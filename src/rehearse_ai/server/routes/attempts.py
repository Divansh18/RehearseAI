"""The candidate's interview attempt: starting the voice connection and the one room image."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

from rehearse_ai.invitations.access import candidate_session
from rehearse_ai.models import InterviewRecord
from rehearse_ai.records.images import KINDS, LIMITS
from rehearse_ai.records.service import accept_interview_image, fail_attempt_start
from rehearse_ai.server.dependencies import Ctx, Db
from rehearse_ai.voice.pipeline import LiveSession

router = APIRouter()


def parse_offer(body: object) -> SmallWebRTCRequest:
    """The WebRTC offer, accepted only with the acknowledgement of the image disclosure."""
    try:
        if (
            not isinstance(body, dict)
            or body.get("type") != "offer"
            or body.get("snapshot_acknowledged") is not True
        ):
            raise ValueError("Expected a WebRTC offer")
        return SmallWebRTCRequest.from_dict(
            {key: value for key, value in body.items() if key != "snapshot_acknowledged"}
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid WebRTC offer") from exc


@router.post("/api/offer")
async def offer(ctx: Ctx, request: Request, db: Db) -> dict[str, str]:
    assignment = await candidate_session(db, request)
    ctx.accounts.check_csrf(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid WebRTC offer") from exc
    offer_request = parse_offer(body)

    try:
        attempt = await ctx.store.create_assignment_attempt(
            assignment.id, ctx.build_id, ctx.prompt_version
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    record_id = attempt.record_id

    async def connected(connection: object) -> None:
        live = LiveSession(
            connection,
            ctx.settings,
            build_id=ctx.build_id,
            assignment_id=str(assignment.id),
            session_id=str(record_id),
            event_sink=ctx.store.enqueue,
            interview_plan={
                **attempt.plan,
                "candidate_focus_areas": assignment.adjustments.get("focus_areas", []),
                # Compatibility: the plan dict keeps its original key name.
                "blueprint_version_id": str(attempt.version_id),
            },
            self_reported_name=assignment.self_reported_name,
        )
        ctx.live.start(live)

    async def fail(reason: str) -> None:
        await fail_attempt_start(
            ctx.db_sessions,
            ctx.store,
            record_id=record_id,
            assignment_id=assignment.id,
            reason=reason,
        )

    try:
        result = await ctx.webrtc.handle_web_request(offer_request, connected)
    except Exception:
        await fail("signalling_failed")
        raise
    if result is None:
        await fail("signalling_rejected")
        raise HTTPException(status_code=503, detail="WebRTC offer was not accepted")
    return result


@router.post("/api/interviews/{record_id}/snapshot", status_code=201)
async def upload_room_image(record_id: uuid.UUID, ctx: Ctx, request: Request, db: Db):
    """Accept the one interview-room image for the authenticated, active attempt.

    Nothing the browser says about itself is trusted: the attempt comes from the candidate
    session cookie (never from the request), the file is decoded and re-encoded, and its
    dimensions and size are checked against fixed limits. A second request is answered "already
    saved" and never replaces the first, so a retry or a double send is harmless.
    """
    assignment = await candidate_session(db, request)
    ctx.accounts.check_csrf(request)
    kind = request.headers.get("x-capture-kind", "camera")  # older clients send no header
    if kind not in KINDS:
        raise HTTPException(400, "Unknown image kind")
    record = await db.get(InterviewRecord, record_id)
    if record is None or record.assignment_id != assignment.id:
        raise HTTPException(404, "Interview not found")
    if record.snapshot_status == "available":
        return JSONResponse({"status": "already_saved"}, status_code=200)
    if record.status != "in_progress" or record.snapshot_status != "pending":
        raise HTTPException(409, "An image is not accepted for this interview")
    if not ctx.live.is_running(session_id=str(record_id), assignment_id=str(assignment.id)):
        raise HTTPException(409, "Interview connection is no longer active")
    if request.headers.get("content-type", "").split(";", 1)[0] != "image/jpeg":
        raise HTTPException(415, "Expected a JPEG image")
    limit = LIMITS[kind].max_bytes
    size_header = request.headers.get("content-length", "0")
    if not size_header.isdecimal():
        raise HTTPException(400, "Invalid image length")
    if int(size_header) > limit:
        raise HTTPException(413, "Image is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "Image is too large")
    outcome = await accept_interview_image(
        ctx.store,
        record_id=record_id,
        assignment_id=assignment.id,
        kind=kind,
        raw=bytes(raw),
    )
    if outcome == "already_saved":
        return JSONResponse({"status": "already_saved"}, status_code=200)
    return JSONResponse({"status": "saved", "kind": kind}, status_code=201)
