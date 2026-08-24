"""Clear the Stripe sync watermark once, so the next sync backfills in full

Until this release, changing the Stripe API key left ``stripe_last_sync_at``
untouched. The watermark records a position in ONE account's event stream, so
pointing the key at a different account meant sauron only ever asked the new
account for events since the old account's last tick — its 30 days of history
were never read. The failure was silent: every sync reported success and stored
nothing.

The code fix resets the watermark whenever the key changes, but that only helps
the *next* key change. An instance that already carries a poisoned watermark
would still resume from it after upgrading, so the tab would stay empty and the
fix would look like it had not worked.

Clearing it once here is safe: the worst case is a single re-read of Stripe's
30-day retention window, and the unique index on ``stripe_event_id`` makes that
a no-op for anything already stored.

Revision ID: 20260824_reset_stripe_wm
Revises: 20260824_stripe_events
Create Date: 2026-08-24

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260824_reset_stripe_wm"
down_revision = "20260824_stripe_events"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM settings WHERE key = 'stripe_last_sync_at'")


def downgrade():
    # Nothing to restore: the watermark is a cache of sync progress, not data.
    # Re-deriving it costs one overlapping read, which is already idempotent.
    pass
