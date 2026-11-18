"""Label what each stored image is, and record what each interviewer utterance was.

Additive. ``snapshot_kind`` says whether the stored image is an interview-room image or a
camera-only still; existing rows stay NULL and are treated as the camera-only stills they are.
``utterance_kind`` and ``question_number`` let the Conversation PDF label questions and follow-ups
only where that was recorded; existing transcript rows stay NULL and are left unlabelled.

Revision ID: 0007_room_image_labels
Revises: 0006_scheduled_preparation
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_room_image_labels"
down_revision = "0006_scheduled_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("interview_record", sa.Column("snapshot_kind", sa.String(12)))
    op.add_column("interview_transcript", sa.Column("utterance_kind", sa.String(24)))
    op.add_column("interview_transcript", sa.Column("question_number", sa.Integer()))


def downgrade() -> None:
    raise RuntimeError("Stored interview records must not be discarded by downgrade")
