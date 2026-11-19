"""Scheduled interviews with automatic preparation, a durable job queue and version freezing.

Additive and compatible with existing rows: existing assignments keep their frozen plan and
become ``preparation_status='ready'``; historical records are untouched.

Revision ID: 0006_scheduled_preparation
Revises: 0005_assigned_interviews
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_scheduled_preparation"
down_revision = "0005_assigned_interviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An assignment can now exist before its plan has been generated.
    op.alter_column(
        "interview_assignment", "blueprint_version_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "interview_assignment",
        sa.Column("preparation_status", sa.String(16), nullable=False, server_default="ready"),
    )
    op.add_column("interview_assignment", sa.Column("preparation_error", sa.String(64)))
    op.add_column("interview_assignment", sa.Column("target_minutes", sa.Integer()))
    op.create_check_constraint(
        "ck_assignment_preparation_status",
        "interview_assignment",
        "preparation_status in ('pending','preparing','ready','failed')",
    )

    # The version an attempt actually ran is frozen on the record when it starts.
    op.add_column(
        "interview_record",
        sa.Column(
            "blueprint_version_id",
            sa.Uuid(),
            sa.ForeignKey("interview_blueprint_version.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column("interview_record", sa.Column("coverage_summary", sa.JSON()))
    op.execute(
        "update interview_record r set blueprint_version_id = a.blueprint_version_id "
        "from interview_assignment a where r.assignment_id = a.id"
    )

    op.create_table(
        "interview_preparation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("interview_assignment.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "blueprint_id",
            sa.Uuid(),
            sa.ForeignKey("interview_blueprint.id", ondelete="RESTRICT"),
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("resume_filename", sa.String(200), nullable=False),
        sa.Column("resume_path", sa.String(96), nullable=False),
        sa.Column("resume_sha256", sa.String(64), nullable=False),
        sa.Column("resume_pages", sa.Integer(), nullable=False),
        sa.Column("resume_text", sa.Text(), nullable=False),
        sa.Column("jd_source", sa.String(8), nullable=False),
        sa.Column("jd_filename", sa.String(200)),
        sa.Column("jd_path", sa.String(96)),
        sa.Column("jd_sha256", sa.String(64)),
        sa.Column("job_description_text", sa.Text(), nullable=False),
        sa.Column("interviewer_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_minutes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("created_by", "idempotency_key", name="uq_preparation_idempotency"),
    )

    op.create_table(
        "interview_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("interview_assignment.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(32)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(64)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="ck_job_status",
        ),
    )
    op.create_index("ix_job_claim", "interview_job", ["status", "run_after"])
    op.create_index("ix_job_assignment", "interview_job", ["assignment_id"])
    # At most one live job per assignment and kind: duplicates cannot pile up.
    op.create_index(
        "uq_job_active",
        "interview_job",
        ["assignment_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status in ('queued','running')"),
    )


def downgrade() -> None:
    raise RuntimeError("Scheduled interviews and their records must not be discarded by downgrade")
