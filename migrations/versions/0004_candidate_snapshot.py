"""Track one private candidate camera still per interview.

Revision ID: 0004_candidate_snapshot
Revises: 0003_interview_records
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_candidate_snapshot"
down_revision = "0003_interview_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "interview_record",
        sa.Column("snapshot_status", sa.String(16), nullable=False, server_default="not_started"),
    )
    op.add_column(
        "interview_record",
        sa.Column("snapshot_captured_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_interview_snapshot_status",
        "interview_record",
        "snapshot_status IN ('not_started','pending','available','unavailable')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_interview_snapshot_status", "interview_record", type_="check")
    op.drop_column("interview_record", "snapshot_captured_at")
    op.drop_column("interview_record", "snapshot_status")
