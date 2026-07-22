"""Notify expiring users who are currently streaming.

Cross-references the "expiring this week" user list with the live now-playing
sessions and pushes an on-screen message (via the media server's session
messaging API) to any expiring user who is actively watching something.

Only Jellyfin/Emby expose a session-messaging command, so other server types
are skipped silently. Delivery is idempotent per expiry window: a user is
messaged at most once while inside a given 7-day expiring window, so neither
the manual admin action nor the scheduled job spams an active viewer.
"""

import datetime
import logging

from app.extensions import db
from app.services.expiry import get_expiring_this_week_users
from app.services.media.service import (
    get_client_for_media_server,
    get_now_playing_all_servers,
)

# Server types that support the on-screen session message command.
SUPPORTED_SERVER_TYPES = frozenset({"jellyfin", "emby"})

# The expiring window opens this many days before the expiry timestamp; used to
# scope idempotency to the current window.
EXPIRING_WINDOW_DAYS = 7

# Message shown to the user. Intentionally fixed Spanish (México) copy — this is
# an end-user facing notice for a Spanish-speaking deployment, independent of
# the admin UI locale.
EXPIRY_MESSAGE_HEADER = "Aviso de suscripción"
EXPIRY_MESSAGE_TEXT = (
    "Tu suscripción está por vencer. Por favor, renueva tu acceso "
    "para continuar disfrutando del servicio."
)


def _as_naive_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """Coerce a datetime to naive UTC for safe comparison with DB values."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(datetime.UTC).replace(tzinfo=None)
    return value


def _index_playing_sessions(sessions: list[dict]) -> tuple[dict, dict]:
    """Build lookup maps of *playing* sessions keyed by (server_id, user_id)
    and (server_id, lowercased user_name)."""
    by_user_id: dict[tuple, dict] = {}
    by_user_name: dict[tuple, dict] = {}
    for session in sessions:
        if session.get("state") != "playing":
            continue
        server_id = session.get("server_id")
        user_id = session.get("user_id")
        user_name = session.get("user_name")
        if server_id is None:
            continue
        if user_id:
            by_user_id[(server_id, str(user_id))] = session
        if user_name:
            by_user_name[(server_id, str(user_name).lower())] = session
    return by_user_id, by_user_name


def _find_session_for_user(user, by_user_id: dict, by_user_name: dict) -> dict | None:
    """Match an expiring user to an active playing session on their own server.

    Prefers the media-server user id (``User.token`` == session ``user_id``);
    falls back to a case-insensitive username match on the same server.
    """
    server_id = user.server_id
    if server_id is None:
        return None
    if user.token:
        match = by_user_id.get((server_id, str(user.token)))
        if match:
            return match
    if user.username:
        return by_user_name.get((server_id, str(user.username).lower()))
    return None


def notify_expiring_streaming_users(app=None, force: bool = False) -> dict:
    """Send the expiry notice to expiring users who are currently streaming.

    Args:
        app: Flask app for out-of-context (scheduler) calls. When omitted, the
            current application context is used.
        force: When True, ignore the per-window idempotency guard and re-send.

    Returns:
        dict: Summary counters — notified, already_notified, not_streaming,
        unsupported, errors.
    """
    if app is None:
        from flask import current_app

        try:
            app = current_app._get_current_object()  # type: ignore
        except RuntimeError:
            logging.error(
                "notify_expiring_streaming_users called outside application "
                "context and no app provided"
            )
            return _empty_summary()

    with app.app_context():
        return _run(force=force)


def _empty_summary() -> dict:
    return {
        "notified": 0,
        "already_notified": 0,
        "not_streaming": 0,
        "unsupported": 0,
        "errors": 0,
    }


def _run(force: bool) -> dict:
    summary = _empty_summary()

    expiring = get_expiring_this_week_users()
    if not expiring:
        return summary

    sessions = get_now_playing_all_servers()
    by_user_id, by_user_name = _index_playing_sessions(sessions)

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    changed = False

    for item in expiring:
        user = item["user"]
        server = user.server

        if server is None or server.server_type not in SUPPORTED_SERVER_TYPES:
            summary["unsupported"] += 1
            continue

        session = _find_session_for_user(user, by_user_id, by_user_name)
        if session is None:
            summary["not_streaming"] += 1
            continue

        # Idempotency: skip if we already notified within the current window.
        if not force and _already_notified_this_window(user):
            summary["already_notified"] += 1
            continue

        try:
            client = get_client_for_media_server(server)
            delivered = client.send_message(
                session_id=session.get("session_id", ""),
                text=EXPIRY_MESSAGE_TEXT,
                header=EXPIRY_MESSAGE_HEADER,
            )
        except Exception as exc:
            logging.error(
                "Failed to send expiry notice to user %s on %s: %s",
                user.username,
                server.name,
                exc,
            )
            summary["errors"] += 1
            continue

        if delivered:
            user.expiry_notified_at = now
            changed = True
            summary["notified"] += 1
            logging.info(
                "📩 Sent expiry notice to streaming user %s on %s",
                user.username,
                server.name,
            )
        else:
            summary["errors"] += 1

    if changed:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logging.error("Failed to persist expiry_notified_at updates: %s", exc)

    return summary


def _already_notified_this_window(user) -> bool:
    """True if the user was already notified inside the current expiry window.

    The window opens ``EXPIRING_WINDOW_DAYS`` before ``user.expires``; a notice
    stamped after that point belongs to the current window. If the user renews
    (expiry moves further out), the window opens later and a fresh notice is
    allowed the next time they re-enter the expiring range.
    """
    notified_at = _as_naive_utc(user.expiry_notified_at)
    if notified_at is None:
        return False

    expires = _as_naive_utc(user.expires)
    if expires is None:
        # No expiry to scope against — fall back to "notified at all".
        return True

    window_open = expires - datetime.timedelta(days=EXPIRING_WINDOW_DAYS)
    return notified_at >= window_open
