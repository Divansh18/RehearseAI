"""Versioned blueprints, assignments and restricted candidate sessions.

Revision ID: 0005_assigned_interviews
Revises: 0004_candidate_snapshot
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_assigned_interviews"
down_revision = "0004_candidate_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_blueprint",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "interview_blueprint_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "blueprint_id",
            sa.Uuid(),
            sa.ForeignKey("interview_blueprint.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("blueprint_id", "version", name="uq_blueprint_version"),
    )
    op.create_index(
        "ix_blueprint_version_blueprint", "interview_blueprint_version", ["blueprint_id"]
    )
    op.create_table(
        "interview_assignment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "blueprint_version_id",
            sa.Uuid(),
            sa.ForeignKey("interview_blueprint_version.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("candidate_email", sa.String(320), nullable=False),
        sa.Column("candidate_name", sa.String(120)),
        sa.Column("self_reported_name", sa.String(120)),
        sa.Column("adjustments", sa.JSON(), nullable=False),
        sa.Column("candidate_instructions", sa.Text(), nullable=False),
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("invitation_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("invitation_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mail_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latest_start_at >= scheduled_start_at", name="ck_assignment_window"),
    )
    op.create_index("ix_assignment_email", "interview_assignment", ["candidate_email"])
    op.create_table(
        "candidate_interview_session",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("interview_assignment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_candidate_interview_session_assignment",
        "candidate_interview_session",
        ["assignment_id"],
    )
    op.create_table(
        "interview_assignment_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("interview_assignment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_assignment_audit_assignment", "interview_assignment_audit", ["assignment_id"]
    )
    op.alter_column("interview_record", "candidate_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "interview_record",
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("interview_assignment.id", ondelete="RESTRICT"),
        ),
    )
    op.create_unique_constraint(
        "uq_interview_assignment_attempt", "interview_record", ["assignment_id"]
    )


def downgrade() -> None:
    raise RuntimeError("Assigned interview records must not be discarded by downgrade")
