"""Add user.expiry_notified_at for streaming expiry-notice idempotency

Adds a nullable timestamp recording the last time an expiring user was sent an
on-screen "subscription expiring" message while streaming. Lets the manual
button and the scheduled job avoid re-notifying the same user repeatedly.

Revision ID: 20260722_expiry_notify
Revises: 20260721_transcode_off
Create Date: 2026-07-22

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260722_expiry_notify"
down_revision = "20260721_transcode_off"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("expiry_notified_at", sa.DateTime(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("expiry_notified_at")
