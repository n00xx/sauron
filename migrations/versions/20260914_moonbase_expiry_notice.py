"""Add user.moonbase_notice_id / moonbase_notice_expires for the Moonbase expiry notice

Records the Moonbase in-app message currently telling a member their membership
ends within a day, and the expiry it was about. The id is what lets a renewal
delete the message; the expiry is how a renewal is recognised.

Revision ID: 20260914_moonbase_notice
Revises: 20260825_notif_backfill
Create Date: 2026-09-14

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260914_moonbase_notice"
down_revision = "20260825_notif_backfill"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("moonbase_notice_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("moonbase_notice_expires", sa.DateTime(), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("moonbase_notice_expires")
        batch_op.drop_column("moonbase_notice_id")
