"""Persist interview attempts and ordered transcript entries.

Revision ID: 0003_interview_records
Revises: 0002_candidate_email_index
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_interview_records"
down_revision = "0002_candidate_email_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_record",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("termination_reason", sa.String(48)),
        sa.Column("build_id", sa.String(80), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pdf_status", sa.String(16), nullable=False, server_default="not_ready"),
        sa.Column("pdf_error", sa.String(120)),
        sa.CheckConstraint(
            "status IN ('starting','in_progress','completed','ended_early','interrupted','failed')",
            name="ck_interview_status",
        ),
        sa.CheckConstraint(
            "pdf_status IN ('not_ready','pending','generating','available','failed')",
            name="ck_interview_pdf_status",
        ),
    )
    op.create_index("ix_interview_record_candidate_id", "interview_record", ["candidate_id"])
    op.create_index("ix_interview_record_created_at", "interview_record", ["created_at"])
    op.create_table(
        "interview_transcript",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "interview_id",
            sa.Uuid(),
            sa.ForeignKey("interview_record.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.String(96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("utterance_id", sa.Integer()),
        sa.Column("speaker", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("interrupted", sa.Boolean(), nullable=False),
        sa.Column("output_started_at", sa.DateTime(timezone=True)),
        sa.Column("output_ended_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("interview_id", "source_event_id", name="uq_transcript_event"),
        sa.UniqueConstraint("interview_id", "utterance_id", name="uq_transcript_utterance"),
        sa.UniqueConstraint("interview_id", "sequence", name="uq_transcript_sequence"),
    )
    op.create_index(
        "ix_interview_transcript_interview_id", "interview_transcript", ["interview_id"]
    )


def downgrade() -> None:
    op.drop_table("interview_transcript")
    op.drop_table("interview_record")
