"""Align case-normalized candidate email uniqueness with the ORM model.

Revision ID: 0002_candidate_email_index
Revises: 0001_candidate_accounts
"""

from alembic import op

revision = "0002_candidate_email_index"
down_revision = "0001_candidate_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_candidate_email", "candidate", type_="unique")
    op.drop_index("ix_candidate_email", table_name="candidate")
    op.create_index("ix_candidate_email", "candidate", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_candidate_email", table_name="candidate")
    op.create_unique_constraint("uq_candidate_email", "candidate", ["email"])
    op.create_index("ix_candidate_email", "candidate", ["email"])
