"""Candidate accounts, revocable sessions, action links, and rate limits.

Revision ID: 0001_candidate_accounts
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_candidate_accounts"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("email", name="uq_candidate_email"),
    )
    op.create_index("ix_candidate_email", "candidate", ["email"])
    op.create_table(
        "candidate_session",
        sa.Column("token", sa.String(43), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("candidate.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_candidate_session_user_id", "candidate_session", ["user_id"])
    op.create_index("ix_candidate_session_created_at", "candidate_session", ["created_at"])
    op.create_table(
        "account_action_token",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("candidate.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("purpose IN ('verify', 'reset')", name="ck_action_purpose"),
    )
    op.create_index(
        "ix_account_action_token_candidate_id", "account_action_token", ["candidate_id"]
    )
    op.create_table(
        "auth_rate_bucket",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count > 0", name="ck_rate_count"),
    )


def downgrade() -> None:
    op.drop_table("auth_rate_bucket")
    op.drop_table("account_action_token")
    op.drop_table("candidate_session")
    op.drop_table("candidate")
