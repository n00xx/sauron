"""Subscribe existing notification agents to the newly added events

``notification.notification_events`` stores opt-INs as a comma-separated list,
and a row keeps whatever was saved when it was created. Every agent already in
the database predates ``user_renewed``, so without this backfill ``notify``
would skip it for all of them: the alert would reach nobody, and the edit modal
would render its checkbox unchecked — meaning an admin who opened and saved an
agent for any unrelated reason would silently confirm the off state.

One-time on purpose. Running it again would re-subscribe an admin who
deliberately unticked something, which is why the same logic is never invoked
at runtime.

Operational events are not written here: they bypass the subscription filter
entirely (see app/services/notification_events.py), so listing them would only
make the stored value misleading about what the checkboxes control.

Revision ID: 20260825_notif_backfill
Revises: 20260825_resend_email
Create Date: 2026-08-25

"""

import sqlalchemy as sa
from alembic import op

from app.services.notification_events import backfill_subscription

# revision identifiers, used by Alembic.
revision = "20260825_notif_backfill"
down_revision = "20260825_resend_email"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()

    inspector = sa.inspect(connection)
    if "notification" not in inspector.get_table_names():
        # Fresh install: the column default already covers new rows.
        return

    rows = connection.execute(
        sa.text("SELECT id, notification_events FROM notification")
    ).fetchall()

    for row_id, stored in rows:
        updated = backfill_subscription(stored)
        if updated != (stored or ""):
            connection.execute(
                sa.text(
                    "UPDATE notification SET notification_events = :events "
                    "WHERE id = :id"
                ),
                {"events": updated, "id": row_id},
            )


def downgrade():
    # Nothing to undo: this only widens what an agent listens to, and the keys
    # it adds are indistinguishable from ones an admin ticked by hand. Removing
    # them on downgrade would discard a real choice.
    pass
