import os

from app.extensions import db
from app.models import Settings


def inject_server_name():
    from sqlalchemy.exc import OperationalError, PendingRollbackError

    try:
        # Use no_autoflush to prevent triggering pending session changes
        with db.session.no_autoflush:
            setting = Settings.query.filter_by(key="server_name").first()
            server_name = setting.value if setting else "Wizarr"
    except (OperationalError, PendingRollbackError) as e:
        if "database is locked" in str(e).lower():
            # Fallback to default if database is locked
            server_name = "Wizarr"
        else:
            raise

    return {"server_name": server_name}


def inject_plus_features():
    """Inject Plus features availability into template context."""
    try:
        import plus

        is_plus_enabled = plus.is_plus_enabled()  # type: ignore
    except (ImportError, AttributeError):
        is_plus_enabled = False

    return {"is_plus_enabled": is_plus_enabled}


def inject_app_version():
    """Inject current app version into template context for cache busting."""
    return {"app_version": os.getenv("APP_VERSION", "dev")}


def inject_turnstile():
    """Inject Cloudflare Turnstile state so any render of login.html can show
    the widget. Only the public site key is exposed to templates."""
    try:
        from app.services.turnstile import get_site_key, is_turnstile_enabled

        enabled = is_turnstile_enabled()
        return {
            "turnstile_enabled": enabled,
            "turnstile_site_key": get_site_key() if enabled else None,
        }
    except Exception:
        return {"turnstile_enabled": False, "turnstile_site_key": None}


def inject_notification_events():
    """Expose the notification event catalogue to every template.

    The agents list and both agent modals render their badges and checkboxes by
    iterating this, so adding an event to app/services/notification_events.py is
    all it takes for the UI to pick it up.
    """
    from app.services.notification_events import (
        EVENT_TYPES,
        OPERATIONAL_EVENT_TYPES,
        SUBSCRIBABLE_EVENT_TYPES,
    )

    return {
        "event_types": EVENT_TYPES,
        "subscribable_event_types": SUBSCRIBABLE_EVENT_TYPES,
        "operational_event_types": OPERATIONAL_EVENT_TYPES,
    }
