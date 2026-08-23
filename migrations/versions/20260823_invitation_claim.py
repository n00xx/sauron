"""Add provisional claim columns to invitation

Separates the reservation held during provisioning from `used`, which means
"an account was actually created". Reusing `used` as the claim marker made a
single-use invitation reject its own first redemption, because the media-server
clients re-validate the code from inside `_do_join`.

Revision ID: 20260823_invite_claim
Revises: 20260722_expiry_notify
Create Date: 2026-08-23

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260823_invite_claim"
down_revision = "20260722_expiry_notify"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("claim_token", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.drop_column("claim_token")
        batch_op.drop_column("claimed_at")
