"""Add stripe_event table

Mirrors Stripe events into sauron so the Activity > Eventos tab can monitor
payments, refunds, disputes and fraud warnings, and correlate them with the
playback evidence that only this instance holds.

Stripe retains events for 30 days; this table is the durable archive.

Revision ID: 20260824_stripe_events
Revises: 20260823_invite_claim
Create Date: 2026-08-24

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260824_stripe_events"
down_revision = "20260823_invite_claim"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stripe_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stripe_event_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("created_at_stripe", sa.DateTime(), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("api_version", sa.String(), nullable=True),
        sa.Column("object_id", sa.String(), nullable=True),
        sa.Column("payment_intent_id", sa.String(), nullable=True),
        sa.Column("charge_id", sa.String(), nullable=True),
        sa.Column("customer_email", sa.String(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("dispute_reason", sa.String(), nullable=True),
        sa.Column("dispute_due_by", sa.DateTime(), nullable=True),
        sa.Column("network_reason_code", sa.String(), nullable=True),
        sa.Column("wizarr_user_id", sa.Integer(), nullable=True),
        sa.Column("invitation_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["wizarr_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["invitation_id"], ["invitation.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("stripe_event", schema=None) as batch_op:
        # UNIQUE: the idempotency key that makes an overlapping re-sync a no-op.
        batch_op.create_index(
            "ix_stripe_event_stripe_event_id", ["stripe_event_id"], unique=True
        )
        batch_op.create_index("ix_stripe_event_type", ["type"], unique=False)
        batch_op.create_index("ix_stripe_event_category", ["category"], unique=False)
        batch_op.create_index("ix_stripe_event_severity", ["severity"], unique=False)
        batch_op.create_index(
            "ix_stripe_event_created_at_stripe", ["created_at_stripe"], unique=False
        )
        batch_op.create_index("ix_stripe_event_livemode", ["livemode"], unique=False)
        batch_op.create_index("ix_stripe_event_object_id", ["object_id"], unique=False)
        batch_op.create_index(
            "ix_stripe_event_payment_intent_id", ["payment_intent_id"], unique=False
        )
        batch_op.create_index("ix_stripe_event_charge_id", ["charge_id"], unique=False)
        batch_op.create_index(
            "ix_stripe_event_customer_email", ["customer_email"], unique=False
        )
        batch_op.create_index("ix_stripe_event_status", ["status"], unique=False)
        batch_op.create_index(
            "ix_stripe_event_dispute_due_by", ["dispute_due_by"], unique=False
        )
        batch_op.create_index(
            "ix_stripe_event_wizarr_user_id", ["wizarr_user_id"], unique=False
        )
        batch_op.create_index(
            "ix_stripe_event_invitation_id", ["invitation_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("stripe_event", schema=None) as batch_op:
        batch_op.drop_index("ix_stripe_event_invitation_id")
        batch_op.drop_index("ix_stripe_event_wizarr_user_id")
        batch_op.drop_index("ix_stripe_event_dispute_due_by")
        batch_op.drop_index("ix_stripe_event_status")
        batch_op.drop_index("ix_stripe_event_customer_email")
        batch_op.drop_index("ix_stripe_event_charge_id")
        batch_op.drop_index("ix_stripe_event_payment_intent_id")
        batch_op.drop_index("ix_stripe_event_object_id")
        batch_op.drop_index("ix_stripe_event_livemode")
        batch_op.drop_index("ix_stripe_event_created_at_stripe")
        batch_op.drop_index("ix_stripe_event_severity")
        batch_op.drop_index("ix_stripe_event_category")
        batch_op.drop_index("ix_stripe_event_type")
        batch_op.drop_index("ix_stripe_event_stripe_event_id")

    op.drop_table("stripe_event")
