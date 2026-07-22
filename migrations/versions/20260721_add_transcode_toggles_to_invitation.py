"""Add allow_transcode_audio and allow_transcode_video to invitation table

Adds two Jellyfin playback toggles to the invitation table, mapping to the
Jellyfin user policy keys EnableAudioPlaybackTranscoding /
EnableVideoPlaybackTranscoding. Defaulted ON so every new invitation enables
transcoding playback unless the admin unchecks it in the Create Invitation modal.

Revision ID: 20260721_transcode
Revises: 20260401_repair
Create Date: 2026-07-21

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260721_transcode"
down_revision = "20260401_repair"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allow_transcode_audio",
                sa.Boolean(),
                nullable=True,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "allow_transcode_video",
                sa.Boolean(),
                nullable=True,
                server_default=sa.true(),
            )
        )


def downgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.drop_column("allow_transcode_video")
        batch_op.drop_column("allow_transcode_audio")
