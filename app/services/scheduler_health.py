"""Watchdog for the scheduled Stripe sync.

Written after the sync stopped running for two days without a single signal.
The Eventos tab showed sync enabled, a valid key and a 15 minute interval; the
last sync was 48 hours old; the app served every other request normally. Three
separate silences made that possible:

  * ``init_extensions`` registered the job inside a ``try/except`` that logged
    at *debug* and blamed missing migrations, so any real failure disappeared.
  * a scheduler that failed to start only produced a *warning*.
  * the settings screen gave up silently whenever the scheduler was not
    running — saving a valid key answered "Settings saved" and changed nothing.

None of those tell anyone the money path went blind. The dispute queue lives in
that tab, and an unanswered dispute is a lost dispute.

This module is the single place that knows how the job should be registered and
what "stalled" means, so the boot path, the settings screen and the health probe
cannot drift apart again.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

STRIPE_SYNC_JOB_ID = "sync_stripe_events"

# How many intervals may pass before a quiet sync counts as stalled. One missed
# tick is a slow run or a restart; three in a row is a fault.
STALE_INTERVAL_MULTIPLIER = 3
# Floor for the staleness window, so a 1 minute interval cannot alert on a
# 3 minute container restart.
MIN_STALE_MINUTES = 30
# The container healthcheck hits the probe every 30s. Alert at most this often.
ALERT_COOLDOWN_MINUTES = 60
# ...and do not even touch the database more often than this.
CHECK_THROTTLE_SECONDS = 300

_ALERT_SETTING = "stripe_sync_stall_alert_at"

# Process-local: a throttle, not a lock. Two workers each doing one check every
# five minutes is fine; the alert cooldown is what stops duplicate messages, and
# that one is stored in the database precisely because it must be shared.
_last_check_monotonic: float | None = None


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ensure_stripe_sync_job(app, *, start_if_stopped: bool = True) -> bool:
    """Register, refresh or drop the sync job to match the saved settings.

    Returns whether the job is registered afterwards. Safe to call repeatedly,
    from boot, from the settings screen and from the watchdog.

    Starts the scheduler when it is not running instead of returning early:
    a stopped scheduler was the state that made every later repair a no-op.
    ``start_if_stopped=False`` is for the boot path, which starts the scheduler
    itself a few lines later and does not want the order shuffled.

    Requires an app context.
    """
    from app.extensions import scheduler, scheduler_disabled_by_config
    from app.services.stripe_events import get_setting, get_sync_interval_minutes

    try:
        if scheduler_disabled_by_config():
            # The scheduler was never initialised on this deployment. Starting
            # it here would override a deliberate operator choice, and add_job
            # on an un-initialised scheduler fails anyway.
            return False

        if not get_setting("stripe_api_key"):
            # No key: the job must not exist. Removing it is what makes
            # "clear the key" take effect without a restart.
            if scheduler.get_job(STRIPE_SYNC_JOB_ID):
                scheduler.remove_job(STRIPE_SYNC_JOB_ID)
            return False

        if not scheduler.running and start_if_stopped:
            scheduler.start()
            logger.warning(
                "Scheduler was not running; started it to register the Stripe sync"
            )

        from app.tasks.stripe_sync import sync_stripe_events_task

        interval = get_sync_interval_minutes()
        scheduler.add_job(
            id=STRIPE_SYNC_JOB_ID,
            func=lambda: sync_stripe_events_task(app),
            trigger="interval",
            minutes=interval,
            replace_existing=True,
            # A tick missed while the process was busy or restarting still runs
            # if it is only a little late, and several missed ticks collapse
            # into one: the sync reads a moving window, so catching up tick by
            # tick would just re-read the same events.
            coalesce=True,
            misfire_grace_time=int(interval * 60),
            max_instances=1,
        )
        return True
    except Exception as exc:
        # Loud on purpose. The debug-level version of this line is why the
        # fault survived for two days.
        logger.error(
            "Could not register the scheduled Stripe sync",
            error=str(exc),
            exc_info=True,
        )
        return False


def check_stripe_sync_health() -> dict[str, Any]:
    """Describe whether the scheduled sync is actually going to run.

    Never raises. Requires an app context.

    ``stalled`` answers one question: is this install expecting automated syncs
    and not getting them? Sync switched off, or no key, is not a fault — however
    old the last sync is.
    """
    from app.extensions import scheduler, scheduler_disabled_by_config
    from app.services.stripe_events import (
        get_setting,
        get_sync_interval_minutes,
        is_sync_enabled,
    )

    health: dict[str, Any] = {
        "configured": False,
        "enabled": False,
        "disabled_by_config": False,
        "scheduler_running": False,
        "job_registered": False,
        "next_run_at": None,
        "last_sync_at": None,
        "minutes_since_sync": None,
        "interval_minutes": None,
        "stale_after_minutes": None,
        "stalled": False,
        "reason": None,
    }

    try:
        health["disabled_by_config"] = scheduler_disabled_by_config()
        health["configured"] = bool(get_setting("stripe_api_key"))
        health["enabled"] = is_sync_enabled()
        interval = get_sync_interval_minutes()
        health["interval_minutes"] = interval
        stale_after = max(MIN_STALE_MINUTES, interval * STALE_INTERVAL_MULTIPLIER)
        health["stale_after_minutes"] = stale_after

        health["scheduler_running"] = bool(getattr(scheduler, "running", False))
        job = (
            scheduler.get_job(STRIPE_SYNC_JOB_ID)
            if health["scheduler_running"]
            else None
        )
        health["job_registered"] = job is not None
        next_run = getattr(job, "next_run_time", None) if job else None
        health["next_run_at"] = next_run.isoformat() if next_run else None

        last_sync = _parse_timestamp(get_setting("stripe_last_sync_at"))
        health["last_sync_at"] = last_sync.isoformat() if last_sync else None
        if last_sync:
            health["minutes_since_sync"] = (
                datetime.now(UTC) - last_sync
            ).total_seconds() / 60

        if not health["enabled"] or health["disabled_by_config"]:
            # Nothing is supposed to run here. Not a fault, whatever the saved
            # Stripe settings say.
            return health

        if not health["scheduler_running"]:
            health["stalled"] = True
            health["reason"] = "scheduler_not_running"
        elif not health["job_registered"]:
            health["stalled"] = True
            health["reason"] = "job_missing"
        elif (
            health["minutes_since_sync"] is None
            or health["minutes_since_sync"] > stale_after
        ):
            # "Never synced" counts as stalled once sync is on: a fresh install
            # that cannot reach Stripe looks exactly like one that stopped.
            health["stalled"] = True
            health["reason"] = "no_recent_sync"
    except Exception as exc:
        # A health check that throws would take the probe or the tab with it.
        logger.warning("Stripe sync health check failed", error=str(exc))

    return health


_REASON_TEXT = {
    "scheduler_not_running": (
        "The background scheduler is not running, so no scheduled job is firing "
        "— expiry checks included."
    ),
    "job_missing": "The Stripe sync job is not registered with the scheduler.",
    "no_recent_sync": "The Stripe sync has not completed for longer than expected.",
}


def _alert_stalled(health: dict[str, Any], repaired: bool) -> None:
    """Send one operational alert, at most once per cooldown.

    Best effort by design: an unreachable notification endpoint must not turn a
    warning about a broken sync into a broken health probe.
    """
    from app.extensions import db
    from app.services.stripe_events import get_setting, set_setting

    try:
        last_alert = _parse_timestamp(get_setting(_ALERT_SETTING))
        now = datetime.now(UTC)
        if last_alert:
            minutes_since = (now - last_alert).total_seconds() / 60
            if minutes_since < ALERT_COOLDOWN_MINUTES:
                return

        set_setting(_ALERT_SETTING, now.isoformat())
        db.session.commit()
    except Exception as exc:
        logger.warning("Could not record the stall alert timestamp", error=str(exc))
        return

    minutes = health.get("minutes_since_sync")
    last_seen = (
        f"Last sync {minutes / 60:.1f} hours ago."
        if minutes is not None
        else "No sync has ever completed."
    )
    repair_line = (
        " The job was re-registered automatically; check the tab in a few minutes."
        if repaired
        else " Automatic repair did not work — this one needs a look."
    )
    message = (
        f"{_REASON_TEXT.get(health.get('reason'), 'The Stripe sync is not running.')} "
        f"{last_seen}{repair_line} Disputes are answered from this data, and an "
        "unanswered dispute is a lost dispute."
    )

    try:
        from app.services.notifications import notify

        notify(
            "Stripe sync stalled",
            message,
            tags="warning",
            event_type="stripe_sync_stalled",
        )
    except Exception as exc:
        logger.warning("Could not send the stalled-sync alert", error=str(exc))


def watchdog_tick(app, *, force: bool = False) -> dict[str, Any] | None:
    """Check the sync, repair what can be repaired, and alert once.

    Returns the health dict when something was wrong, ``None`` when everything
    is fine or the check was throttled away. Callers treat it as fire-and-forget.

    Wired into ``/health`` rather than the scheduler itself, deliberately: a
    watchdog that runs *on* the thing it watches cannot report that the thing
    stopped. ``/health`` is hit by the container healthcheck every 30 seconds
    whether or not anybody has the tab open — and nobody had it open for two
    days.
    """
    global _last_check_monotonic

    now = time.monotonic()
    if not force:
        if (
            _last_check_monotonic is not None
            and now - _last_check_monotonic < CHECK_THROTTLE_SECONDS
        ):
            return None
        _last_check_monotonic = now

    try:
        with app.app_context():
            health = check_stripe_sync_health()
            if not health["stalled"]:
                return None

            logger.error(
                "Scheduled Stripe sync is stalled",
                reason=health["reason"],
                minutes_since_sync=health["minutes_since_sync"],
                scheduler_running=health["scheduler_running"],
                job_registered=health["job_registered"],
            )

            repaired = ensure_stripe_sync_job(app)
            health["repaired"] = repaired

            _alert_stalled(health, repaired)
            return health
    except Exception as exc:
        logger.warning("Scheduler watchdog failed", error=str(exc))
        return None
