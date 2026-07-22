"""Default the invitation transcode toggles to OFF for new invitations

Flips the server_default of allow_transcode_audio / allow_transcode_video from
true to false so every newly created invitation has Jellyfin transcoding
playback disabled unless the admin explicitly enables it in the Create
Invitation modal. Existing invitation rows are intentionally left unchanged.

Revision ID: 20260721_transcode_off
Revises: 20260721_transcode
Create Date: 2026-07-21

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260721_transcode_off"
down_revision = "20260721_transcode"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.alter_column(
            "allow_transcode_audio",
            existing_type=sa.Boolean(),
            existing_nullable=True,
            server_default=sa.false(),
        )
        batch_op.alter_column(
            "allow_transcode_video",
            existing_type=sa.Boolean(),
            existing_nullable=True,
            server_default=sa.false(),
        )


def downgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.alter_column(
            "allow_transcode_video",
            existing_type=sa.Boolean(),
            existing_nullable=True,
            server_default=sa.true(),
        )
        batch_op.alter_column(
            "allow_transcode_audio",
            existing_type=sa.Boolean(),
            existing_nullable=True,
            server_default=sa.true(),
        )
