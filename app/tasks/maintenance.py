import logging
import os

from app.services.expiry import (
    disable_or_delete_user_if_expired,
)


def checkpoint_wal_database(app=None):
    """Checkpoint the SQLite WAL file to prevent unbounded growth.

    WAL mode accumulates changes in the -wal file. Periodic checkpointing
    ensures the WAL doesn't grow too large and improves backup reliability.

    This should be called periodically (e.g., daily) via the scheduler.

    Args:
        app: Flask application instance. If None, will try to get from current context.
    """
    if app is None:
        from flask import current_app

        try:
            app = current_app._get_current_object()  # type: ignore
        except RuntimeError:
            logging.error(
                "checkpoint_wal_database called outside application context and no app provided"
            )
            return

    with app.app_context():
        from app.extensions import db

        # Only checkpoint SQLite databases in WAL mode
        if "sqlite" not in str(db.engine.url):
            return

        try:
            # Check if we're actually in WAL mode first
            with db.engine.connect() as conn:
                result = conn.exec_driver_sql("PRAGMA journal_mode").fetchone()
                journal_mode = result[0] if result else "unknown"

                if journal_mode.lower() != "wal":
                    # Not in WAL mode, nothing to checkpoint
                    logging.debug(
                        f"Skipping WAL checkpoint (journal_mode={journal_mode})"
                    )
                    return

                # Execute WAL checkpoint - this merges WAL back into main database
                # PASSIVE mode doesn't block readers/writers
                conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)")
                logging.info("✅ SQLite WAL checkpoint completed")
        except Exception as e:
            logging.warning(f"Failed to checkpoint WAL: {e}")


def _get_expiry_check_interval():
    """Get the interval for expiry checks based on environment."""
    # Use 1 minute for development, 15 minutes for production
    if os.getenv("WIZARR_ENABLE_SCHEDULER") == "true":
        return 1  # Development mode: every 1 minute
    return 15  # Production mode: every 15 minutes


def check_expiring(app=None):
    """Check for and process expired users based on expiry action setting.

    Args:
        app: Flask application instance. If None, will try to get from current context.
    """
    if app is None:
        from flask import current_app

        try:
            app = current_app._get_current_object()  # type: ignore
        except RuntimeError:
            # If we're outside application context, we need the app to be passed
            logging.error(
                "check_expiring called outside application context and no app provided"
            )
            return

    with app.app_context():
        processed = disable_or_delete_user_if_expired()
        if len(processed) > 0:
            logging.info(
                "🧹 Expiry cleanup: Processed %s expired users.", len(processed)
            )
        # Only log in development mode to avoid spam in production logs
        elif os.getenv("WIZARR_ENABLE_SCHEDULER") == "true":
            logging.info("🕒 Expiry cleanup: No expired users found.")


def notify_streaming_expirers(app=None):
    """Message expiring users who are currently streaming.

    Scheduled counterpart to the manual admin action: on each cycle it looks
    for users inside the expiring-this-week window who have an active playback
    session and sends them an on-screen renewal notice (idempotent per window).

    Args:
        app: Flask application instance. If None, the notifier resolves the
            current application context itself.
    """
    from app.services.expiry_notify import notify_expiring_streaming_users

    summary = notify_expiring_streaming_users(app)
    if summary.get("notified"):
        logging.info(
            "📩 Expiry notices: sent to %s streaming user(s).", summary["notified"]
        )
    elif os.getenv("WIZARR_ENABLE_SCHEDULER") == "true":
        logging.info("📩 Expiry notices: no streaming expirers to notify.")


def check_locked_out(app=None):
    """Alert on members who are paid up but whose account is switched off.

    Runs on the same cadence as the expiry sweep, and for the mirror-image
    reason: that one takes access away when a membership lapses, this one
    notices when access was never given back.

    Args:
        app: Flask application instance. If None, will try to get from current context.
    """
    if app is None:
        from flask import current_app

        try:
            app = current_app._get_current_object()  # type: ignore
        except RuntimeError:
            logging.error(
                "check_locked_out called outside application context and no app provided"
            )
            return

    with app.app_context():
        try:
            from app.services.renewal_health import check_locked_out_members

            check_locked_out_members()
        except Exception:
            # A diagnostic must never take the scheduler down with it, but a
            # silent one is worthless — hence exception(), not debug().
            logging.exception("Locked-out member check failed")
