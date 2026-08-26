"""Scheduled pull of Stripe events into the local archive.

Registered from ``app.extensions`` alongside the other interval jobs. Only runs
when an API key is configured and sync is enabled, so a deployment that never
touches Stripe pays nothing for this.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def sync_stripe_events_task(app) -> dict[str, Any]:
    """Sync events, then correlate the new rows with sauron's own data.

    Never raises: this runs on the scheduler, and an exception escaping here
    would take the job down permanently rather than skipping one tick.
    """
    with app.app_context():
        from app.extensions import db
        from app.services.stripe_events import is_sync_enabled, sync_and_correlate

        try:
            if not is_sync_enabled():
                return {"skipped": True, "reason": "disabled"}

            # Pull, correlate, alert — in that order, defined once in the
            # service. Wiring the steps here is what let this path and the
            # "Sync now" button drift: whichever ran first stored the dispute,
            # and the other never alerted because the row was no longer new.
            return sync_and_correlate()
        except Exception as exc:
            db.session.rollback()
            logger.error("Stripe sync task failed", error=str(exc), exc_info=True)
            return {"error": str(exc)}
