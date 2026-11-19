"""Durable preparation and invitation jobs: failures, retries, recovery, stale results, races."""

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from support import (
    JOB_TEXT,
    RESUME_TEXT,
    FakeGenerator,
    generated_plan,
    login,
    make_app,
    make_pdf,
    new_admin_email,
)

from rehearse_ai.accounts.service import MailDeliveryError
from rehearse_ai.jobs import JobRunner
from rehearse_ai.models import (
    BlueprintVersion,
    Candidate,
    InterviewAssignment,
    InterviewJob,
    InterviewPreparation,
    InterviewRecord,
)
from rehearse_ai.plans.preparation import (
    PlanProviderError,
    PlanQualityError,
    question_range,
    validate_generated_plan,
)
from rehearse_ai.scheduling.service import Upload, parse_fields, schedule_interview


@pytest.fixture(scope="module")
def admin_email():
    return new_admin_email()


class Env:
    def __init__(self, app, admin_id, root) -> None:
        self.app = app
        self.runner: JobRunner = app.state.jobs
        self.db = self.runner.db_sessions
        self.admin_id = admin_id
        self.root = root
        self.offset = timedelta(0)
        self.runner.clock = lambda: datetime.now(UTC) + self.offset

    def advance(self, **delta) -> None:
        self.offset += timedelta(**delta)

    async def schedule(self, **overrides) -> uuid.UUID:
        raw = {
            "candidate_name": "Nyla Example",
            "candidate_email": f"candidate-{uuid.uuid4().hex[:8]}@example.com",
            "interviewer_instructions": "PRIVATE-INSTRUCTION-TEXT",
            "job_description_text": JOB_TEXT,
            "scheduled_local": (datetime.now(UTC) - timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "timezone": "Etc/UTC",
            "target_minutes": "30",
            "idempotency_key": uuid.uuid4().hex,
        }
        raw.update(overrides)
        result = await schedule_interview(
            self.db,
            self.root / "documents",
            admin_id=self.admin_id,
            fields=parse_fields(raw),
            resume=Upload("resume.pdf", self.resume_bytes),
            job_file=None,
        )
        return result.assignment_id

    resume_bytes = make_pdf(RESUME_TEXT)

    async def state(self, assignment_id):
        async with self.db() as db:
            assignment = await db.get(InterviewAssignment, assignment_id)
            jobs = {
                job.kind: job
                for job in (
                    await db.scalars(
                        select(InterviewJob)
                        .where(InterviewJob.assignment_id == assignment_id)
                        .order_by(InterviewJob.created_at)
                    )
                ).all()
            }
            versions = (
                await db.scalars(
                    select(BlueprintVersion)
                    .join(
                        InterviewPreparation,
                        InterviewPreparation.blueprint_id == BlueprintVersion.blueprint_id,
                    )
                    .where(InterviewPreparation.assignment_id == assignment_id)
                    .order_by(BlueprintVersion.version)
                )
            ).all()
            return assignment, jobs, list(versions)

    async def update(self, assignment_id, **values) -> None:
        async with self.db.begin() as db:
            await db.execute(
                update(InterviewAssignment)
                .where(InterviewAssignment.id == assignment_id)
                .values(**values)
            )

    async def enqueue_revision(self, assignment_id, request="Add a question about testing"):
        from rehearse_ai.jobs import enqueue

        async with self.db.begin() as db:
            assignment = await db.get(InterviewAssignment, assignment_id)
            await enqueue(
                db,
                assignment_id=assignment_id,
                kind="revise",
                created_by=self.admin_id,
                payload={
                    "request": request,
                    "base_version_id": str(assignment.blueprint_version_id),
                },
            )


@pytest_asyncio.fixture
async def env(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(generated_plan()))
    async with app.state.jobs.db_sessions() as db:
        admin_id = await db.scalar(select(Candidate.id).where(Candidate.email == admin_email))
        await db.execute(
            update(InterviewJob)
            .where(InterviewJob.status.in_(["queued", "running"]))
            .values(status="cancelled")
        )
        await db.commit()
    yield Env(app, admin_id, tmp_path)
    await app.state.jobs.db_sessions.kw["bind"].dispose()


def revised_plan() -> dict:
    plan = copy.deepcopy(generated_plan())
    plan["title"] = "Backend engineer interview (revised)"
    plan["questions"][4]["question"] = (
        "How do you test a queue consumer before it reaches production?"
    )
    plan["questions"][4]["topic"] = "Testing"
    return plan


# --- preparation --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_creates_a_validated_versioned_plan_with_normalised_budgets(env):
    assignment_id = await env.schedule()
    await env.runner.drain()
    assignment, jobs, versions = await env.state(assignment_id)
    assert assignment.preparation_status == "ready" and assignment.preparation_error is None
    assert [v.version for v in versions] == [1]
    assert assignment.blueprint_version_id == versions[0].id
    plan = versions[0].content
    assert plan["target_minutes"] == 30 and len(plan["questions"]) == 5
    assert sum(q["budget_seconds"] for q in plan["questions"]) in range(1525, 1536)
    assert [q["required"] for q in plan["questions"]] == [True, True, True, False, False]
    assert plan["questions"][1]["evidence_quote"] == ""  # job-based: no résumé evidence kept
    assert plan["questions"][0]["evidence_quote"].startswith("Built the PRIVATE-EVIDENCE-SPAN")
    assert jobs["prepare"].status == "succeeded"


@pytest.mark.asyncio
async def test_model_failure_retries_with_backoff_then_succeeds(env):
    env.app.state.plan_generator = FakeGenerator(
        PlanProviderError("RateLimitError"), generated_plan()
    )
    assignment_id = await env.schedule()
    await env.runner.drain()
    assignment, jobs, _ = await env.state(assignment_id)
    assert jobs["prepare"].status == "queued" and jobs["prepare"].attempts == 1
    assert jobs["prepare"].last_error == "model_error"
    assert jobs["prepare"].run_after > env.runner.clock()
    assert assignment.blueprint_version_id is None and assignment.preparation_status == "preparing"
    assert await env.runner.drain() == 0  # not due yet
    env.advance(seconds=31)
    await env.runner.drain()
    assignment, jobs, versions = await env.state(assignment_id)
    assert jobs["prepare"].status == "succeeded" and jobs["prepare"].attempts == 2
    assert assignment.preparation_status == "ready" and len(versions) == 1


@pytest.mark.asyncio
async def test_exhausted_preparation_fails_visibly_and_blocks_begin(env):
    env.app.state.plan_generator = FakeGenerator(PlanProviderError("APIConnectionError"))
    assignment_id = await env.schedule()
    for _ in range(3):
        await env.runner.drain()
        env.advance(minutes=10)
    assignment, jobs, versions = await env.state(assignment_id)
    assert jobs["prepare"].status == "failed" and jobs["prepare"].attempts == 3
    assert jobs["prepare"].last_error == "model_error"
    assert (
        assignment.preparation_status == "failed" and assignment.preparation_error == "model_error"
    )
    assert versions == [] and assignment.blueprint_version_id is None
    await env.update(assignment_id, self_reported_name="Nyla Example")
    with pytest.raises(ValueError, match="unavailable"):
        await env.app.state.interview_store.create_assignment_attempt(assignment_id, "b", "p")
    # No third-party text or document content is kept in the error.
    assert "PRIVATE" not in (jobs["prepare"].last_error or "")


@pytest.mark.asyncio
async def test_ungrounded_plan_is_corrected_once_with_feedback_then_accepted(env):
    fabricated = generated_plan()
    fabricated["questions"][0]["evidence_quote"] = (
        "Founded a startup that was acquired for millions"
    )
    generator = FakeGenerator(fabricated, generated_plan())
    env.app.state.plan_generator = generator
    assignment_id = await env.schedule()
    await env.runner.drain()
    assignment, jobs, _ = await env.state(assignment_id)
    assert assignment.preparation_status == "ready" and jobs["prepare"].attempts == 1
    assert len(generator.calls) == 2
    assert "exact span" in generator.calls[1][1]["previous_validation_error"]
    assert "previous_validation_error" not in generator.calls[0][1]


@pytest.mark.asyncio
async def test_persistently_invalid_plans_never_become_ready(env):
    bad = generated_plan()
    bad["questions"][0]["evidence_quote"] = "An invented achievement the résumé never states"
    generator = FakeGenerator(bad)
    env.app.state.plan_generator = generator
    assignment_id = await env.schedule()
    for _ in range(3):
        await env.runner.drain()
        env.advance(minutes=10)
    assignment, jobs, versions = await env.state(assignment_id)
    assert jobs["prepare"].last_error == "plan_invalid" and jobs["prepare"].status == "failed"
    assert assignment.preparation_status == "failed" and versions == []
    assert len(generator.calls) == 6  # two bounded generations per job attempt


def test_plan_validation_rules():
    resume = RESUME_TEXT

    def check(mutate, minutes=30, match=""):
        plan = generated_plan()
        mutate(plan)
        with pytest.raises(PlanQualityError, match=match):
            validate_generated_plan(plan, resume_text=resume, target_minutes=minutes)

    check(lambda p: p["questions"].__delitem__(slice(2, None)), match="between")
    check(lambda p: p["questions"].extend(copy.deepcopy(p["questions"])), match="between")
    check(lambda p: p["questions"][1].update(topic=p["questions"][0]["topic"]), match="same topic")
    check(
        lambda p: p["questions"][1].update(question=p["questions"][0]["question"] + " please"),
        match="essentially the same",
    )
    check(lambda p: p["questions"][0].update(evidence_quote="too short"), match="exact span")
    check(lambda p: [q.update(priority="could") for q in p["questions"]], match="priority must")
    check(lambda p: p.update(title=None), match="limits")
    check(lambda p: p["questions"][0].update(question="x" * 500), match="limits")
    # Layout must not defeat verbatim grounding: wrapped hyphens, curly quotes and spacing.
    laid_out = (
        RESUME_TEXT + " Built the event-\nsourced ledger; it’s still the team’s “system of record”."
    )
    plan = generated_plan()
    plan["questions"][0]["evidence_quote"] = (
        "event-sourced ledger; it's still the team's \"system of record\""
    )
    validate_generated_plan(plan, resume_text=laid_out, target_minutes=30)
    plan = generated_plan()
    plan["questions"][2]["budget_seconds"] = 0
    plan["questions"][3]["max_probes"] = 9
    plan["target_minutes"] = 120  # an attempt by the model to choose its own duration
    result = validate_generated_plan(plan, resume_text=resume, target_minutes=30)
    assert result.target_minutes == 30  # the requested duration, never the model's choice
    assert result.questions[3].max_probes == 2
    assert all(30 <= q.budget_seconds <= 900 for q in result.questions)
    assert question_range(15) == (3, 3) and question_range(60) == (8, 12)


# --- invitation delivery ------------------------------------------------------------


@pytest.mark.asyncio
async def test_invitation_failures_retry_then_recover_and_are_not_duplicated(env):
    sent = []

    async def flaky(to, token, **details):
        if not sent and len(flaky.calls) == 0:
            flaky.calls.append(1)
            raise MailDeliveryError("down")
        sent.append((to, token))

    flaky.calls = []
    env.app.state.accounts.mailer.send_invitation = flaky
    assignment_id = await env.schedule()
    await env.runner.drain()
    assignment, jobs, _ = await env.state(assignment_id)
    assert assignment.mail_status == "failed" and jobs["invite"].status == "queued"
    assert jobs["invite"].last_error == "smtp_failed"
    # Preparation was not held up by the failed email.
    assert assignment.preparation_status == "ready"
    env.advance(seconds=31)
    await env.runner.drain()
    assignment, jobs, _ = await env.state(assignment_id)
    assert assignment.mail_status == "sent" and jobs["invite"].status == "succeeded"
    assert len(sent) == 1
    # A re-run of a finished delivery must not send (and rotate the link) a second time.
    from rehearse_ai.jobs import ClaimedJob
    from rehearse_ai.scheduling.service import InviteHandler

    handler = InviteHandler(env.db, lambda: env.app.state.accounts.mailer)
    async with env.db.begin() as db:
        row = await db.scalar(select(InterviewJob).where(InterviewJob.id == jobs["invite"].id))
        row.status, row.claim_token = "running", "tok"
    await handler.run(ClaimedJob(row.id, assignment_id, "invite", 1, 3, "tok", {}, env.admin_id))
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_exhausted_invitation_leaves_a_visible_failure_that_resend_can_fix(env):
    async def down(*_args, **_kwargs):
        raise MailDeliveryError("down")

    env.app.state.accounts.mailer.send_invitation = down
    assignment_id = await env.schedule()
    for _ in range(3):
        await env.runner.drain()
        env.advance(minutes=10)
    assignment, jobs, _ = await env.state(assignment_id)
    assert jobs["invite"].status == "failed" and assignment.mail_status == "failed"
    assert assignment.status == "assigned"


# --- restart recovery, leases and stale results -------------------------------------


@pytest.mark.asyncio
async def test_restart_requeues_jobs_a_previous_process_left_running(env):
    assignment_id = await env.schedule()
    first = await env.runner.claim_next()
    second = await env.runner.claim_next()
    assert first and second and {first.kind, second.kind} == {"invite", "prepare"}
    _, jobs, _ = await env.state(assignment_id)
    assert {job.status for job in jobs.values()} == {"running"}
    # The process "dies" here. A fresh runner on the same database recovers and completes.
    restarted = JobRunner(env.db, env.runner.handlers)
    assert await restarted.recover() >= 2
    await restarted.drain()
    assignment, jobs, versions = await env.state(assignment_id)
    assert {job.status for job in jobs.values()} == {"succeeded"}
    assert assignment.preparation_status == "ready" and assignment.mail_status == "sent"
    assert len(versions) == 1  # recovered work was not applied twice


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_the_old_claim_is_stale(env):
    assignment_id = await env.schedule()
    stale = await env.runner.claim_next()
    assert stale is not None
    env.advance(seconds=env.runner.lease_secs + 5)
    reclaimed = await env.runner.claim_next()
    assert reclaimed.id == stale.id and reclaimed.claim_token != stale.claim_token
    assert reclaimed.attempts == 2
    # The stale worker's report is ignored; only the current claim can finish the job.
    assert await env.runner._finish(stale, "succeeded", None) is False
    assert await env.runner._finish(reclaimed, "succeeded", None) is True
    _, jobs, _ = await env.state(assignment_id)
    assert jobs[stale.kind].status == "succeeded"


@pytest.mark.asyncio
async def test_a_final_attempt_that_dies_without_reporting_fails_the_job(env):
    assignment_id = await env.schedule()
    for _ in range(3):
        job = await env.runner.claim_next()
        while job is not None and job.kind != "prepare":
            await env.runner._finish(job, "succeeded", None)
            job = await env.runner.claim_next()
        assert job is not None
        env.advance(seconds=env.runner.lease_secs + 5)
    # The exhausted job is failed rather than re-run; only unrelated due work remains.
    leftover = await env.runner.claim_next()
    assert leftover is None or leftover.kind == "invite"
    assignment, jobs, _ = await env.state(assignment_id)
    assert jobs["prepare"].status == "failed" and jobs["prepare"].last_error == "lease_expired"
    assert assignment.preparation_status == "failed"


@pytest.mark.asyncio
async def test_stale_preparation_result_is_discarded_not_applied(env):
    holder = {}

    class ReclaimDuringGeneration(FakeGenerator):
        async def generate(self, prompt_name, payload):
            # While worker A is "thinking", its lease lapses and worker B takes over.
            env.advance(seconds=env.runner.lease_secs + 5)
            holder["b"] = await env.runner.claim_next()
            return await super().generate(prompt_name, payload)

    env.app.state.plan_generator = ReclaimDuringGeneration(generated_plan())
    assignment_id = await env.schedule()
    # Only the prepare job is of interest; finish the invitation first.
    job_a = None
    while True:
        job = await env.runner.claim_next()
        if job.kind == "invite":
            await env.runner._finish(job, "succeeded", None)
        else:
            job_a = job
            break
    await env.runner.execute(job_a)  # A completes its model call, then must be refused
    assignment, jobs, versions = await env.state(assignment_id)
    assert versions == [] and assignment.blueprint_version_id is None
    assert holder["b"].id == job_a.id and jobs["prepare"].status == "running"
    env.app.state.plan_generator = FakeGenerator(generated_plan())
    await env.runner.execute(holder["b"])
    assignment, jobs, versions = await env.state(assignment_id)
    assert len(versions) == 1 and assignment.preparation_status == "ready"


# --- races with starting and cancelling ---------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_during_preparation_discards_the_generated_plan(env):
    assignment_ref = {}

    class CancelDuringGeneration(FakeGenerator):
        async def generate(self, prompt_name, payload):
            await env.update(assignment_ref["id"], status="cancelled")
            return await super().generate(prompt_name, payload)

    env.app.state.plan_generator = CancelDuringGeneration(generated_plan())
    assignment_ref["id"] = await env.schedule()
    await env.runner.drain()
    assignment, jobs, versions = await env.state(assignment_ref["id"])
    assert assignment.status == "cancelled" and versions == []
    assert assignment.blueprint_version_id is None
    assert jobs["prepare"].status == "cancelled"
    assert jobs["prepare"].last_error == "assignment_started_or_cancelled"


@pytest.mark.asyncio
async def test_starting_during_a_revision_rejects_the_late_result(env):
    assignment_id = await env.schedule()
    await env.runner.drain()
    _, _, versions = await env.state(assignment_id)
    assert len(versions) == 1

    class StartDuringRevision(FakeGenerator):
        async def generate(self, prompt_name, payload):
            await env.update(assignment_id, status="starting")
            return await super().generate(prompt_name, payload)

    env.app.state.plan_generator = StartDuringRevision(revised_plan())
    await env.enqueue_revision(assignment_id)
    await env.runner.drain()
    assignment, jobs, versions = await env.state(assignment_id)
    assert [v.version for v in versions] == [1]  # the running interview keeps what it started with
    assert assignment.blueprint_version_id == versions[0].id
    assert jobs["revise"].status == "cancelled" and jobs["revise"].last_error == "stale_revision"


@pytest.mark.asyncio
async def test_revision_creates_a_new_version_and_a_failed_one_leaves_the_plan_intact(env):
    assignment_id = await env.schedule()
    await env.runner.drain()
    await env.update(assignment_id, self_reported_name="Nyla Example")
    # A revision that fails every attempt must not disturb the ready plan.
    env.app.state.plan_generator = FakeGenerator(PlanProviderError("Timeout"))
    await env.enqueue_revision(assignment_id)
    for _ in range(3):
        await env.runner.drain()
        env.advance(minutes=10)
    assignment, jobs, versions = await env.state(assignment_id)
    assert jobs["revise"].status == "failed" and jobs["revise"].last_error == "model_error"
    assert assignment.preparation_status == "ready" and len(versions) == 1
    assert assignment.blueprint_version_id == versions[0].id
    # A successful revision becomes version 2 without editing version 1.
    generator = FakeGenerator(revised_plan())
    env.app.state.plan_generator = generator
    original = copy.deepcopy(versions[0].content)
    await env.enqueue_revision(assignment_id, "Replace the last question with one about testing")
    await env.runner.drain()
    assignment, jobs, versions = await env.state(assignment_id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].content == original
    assert assignment.blueprint_version_id == versions[1].id
    prompt, payload = generator.calls[0]
    assert prompt == "plan_revise" and payload["current_plan"]["title"] == original["title"]
    assert payload["revision_request"].startswith("Replace the last question")
    # Starting freezes exactly the selected version on the record, atomically.
    await env.update(
        assignment_id,
        scheduled_start_at=datetime.now(UTC) - timedelta(minutes=2),
        latest_start_at=datetime.now(UTC) + timedelta(minutes=20),
    )
    started = await env.app.state.interview_store.create_assignment_attempt(
        assignment_id, "build", "prompt"
    )
    assert started.version_id == versions[1].id
    assert started.plan["title"].endswith("(revised)")
    async with env.db() as db:
        record = await db.get(InterviewRecord, started.record_id)
        assert record.blueprint_version_id == versions[1].id
    # After the interview has started, a late revision is rejected outright.
    await env.enqueue_revision(assignment_id)
    await env.runner.drain()
    _, jobs, versions = await env.state(assignment_id)
    assert len(versions) == 2 and jobs["revise"].status == "cancelled"


@pytest.mark.asyncio
async def test_concurrent_duplicate_submissions_create_one_interview(env):
    same = {"idempotency_key": uuid.uuid4().hex, "candidate_email": "same@example.com"}
    results = await asyncio.gather(*(env.schedule(**same) for _ in range(3)))
    assert len(set(results)) == 1
    async with env.db() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(InterviewJob)
                .where(InterviewJob.assignment_id == results[0])
            )
            == 2
        )
    documents = list((env.root / "documents").iterdir())
    assert len(documents) == 1  # losing submissions removed their uploaded copies


# --- HTTP actions -------------------------------------------------------------------


def test_retry_preparation_and_resend_recover_failed_work_over_http(tmp_path, admin_email):
    app = make_app(tmp_path, FakeGenerator(PlanProviderError("Timeout")))
    with TestClient(app, client=(f"test-{uuid.uuid4().hex}", 50300)) as admin:
        headers = login(admin, admin_email)
        runner: JobRunner = app.state.jobs
        offset = {"delta": timedelta(0)}
        runner.clock = lambda: datetime.now(UTC) + offset["delta"]
        admin.portal.call(
            runner.recover
        )  # park stale work from other runs is handled by the next line
        data = {
            "candidate_name": "Nyla Example",
            "candidate_email": f"candidate-{uuid.uuid4().hex[:8]}@example.com",
            "job_description_text": JOB_TEXT,
            "scheduled_local": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "timezone": "Etc/UTC",
            "target_minutes": "30",
            "idempotency_key": uuid.uuid4().hex,
        }
        files = [("resume", ("resume.pdf", make_pdf(RESUME_TEXT), "application/pdf"))]
        created = admin.post("/api/admin/schedule", data=data, files=files, headers=headers)
        assert created.status_code == 201
        assignment_id = created.json()["assignment_id"]

        async def only_mine():
            async with runner.db_sessions.begin() as db:
                await db.execute(
                    update(InterviewJob)
                    .where(InterviewJob.assignment_id != uuid.UUID(assignment_id))
                    .where(InterviewJob.status == "queued")
                    .values(status="cancelled")
                )

        admin.portal.call(only_mine)
        for _ in range(3):
            admin.portal.call(runner.drain)
            offset["delta"] += timedelta(minutes=10)
        detail = admin.get(f"/api/admin/assignments/{assignment_id}").json()
        assert (
            detail["preparation_status"] == "failed"
            and detail["preparation_error"] == "model_error"
        )
        assert detail["mail_status"] == "sent"
        statuses = {job["kind"]: job["status"] for job in detail["jobs"]}
        assert statuses == {"prepare": "failed", "invite": "succeeded"}
        app.state.plan_generator = FakeGenerator(generated_plan())
        retried = admin.post(
            f"/api/admin/assignments/{assignment_id}/retry-preparation", headers=headers
        )
        assert retried.status_code == 202
        assert (
            admin.post(
                f"/api/admin/assignments/{assignment_id}/retry-preparation", headers=headers
            ).status_code
            == 409
        )
        admin.portal.call(runner.drain)
        detail = admin.get(f"/api/admin/assignments/{assignment_id}").json()
        assert detail["preparation_status"] == "ready" and len(detail["plan_versions"]) == 1
        resent = admin.post(f"/api/admin/assignments/{assignment_id}/resend", headers=headers)
        assert resent.status_code == 200 and len(app.state.sent_mail) == 2
