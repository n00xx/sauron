"""Add resend_email table

Durable log of every outbound email sauron sends through Resend.

Resend's free tier retains data for 30 days, and a send-only restricted API key
cannot read ``GET /emails/{id}`` at all — so this table is the only lasting
record of what was sent, to whom, and whether it left the building. It is also
what the Activity > Resend tab counts against the free-tier caps (100/day,
3.000/month), since Resend exposes no quota endpoint.

Revision ID: 20260825_resend_email
Revises: 20260824_reset_stripe_wm
Create Date: 2026-08-25

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260825_resend_email"
down_revision = "20260824_reset_stripe_wm"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resend_email",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("to_address", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resend_id", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # SET NULL, not CASCADE: deleting a user must not erase the record that
        # a password reset link was mailed to them.
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("resend_email", schema=None) as batch_op:
        batch_op.create_index("ix_resend_email_to_address", ["to_address"])
        batch_op.create_index("ix_resend_email_kind", ["kind"])
        batch_op.create_index("ix_resend_email_status", ["status"])
        batch_op.create_index("ix_resend_email_resend_id", ["resend_id"])
        # The quota counters ("today", "this month") scan on this column every
        # time the tab loads.
        batch_op.create_index("ix_resend_email_created_at", ["created_at"])


def downgrade():
    with op.batch_alter_table("resend_email", schema=None) as batch_op:
        batch_op.drop_index("ix_resend_email_created_at")
        batch_op.drop_index("ix_resend_email_resend_id")
        batch_op.drop_index("ix_resend_email_status")
        batch_op.drop_index("ix_resend_email_kind")
        batch_op.drop_index("ix_resend_email_to_address")

    op.drop_table("resend_email")
