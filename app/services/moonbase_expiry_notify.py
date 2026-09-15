"""Expiry notice delivered through Moonbase's server messages.

A member whose membership ends within the next 24 hours gets a message in the
Moonfin apps addressed to them alone, shown right away and kept for 7 days.
When they renew, the message is deleted again — a week of "vence dentro de 1
día" after paying reads as a billing error.

Moonbase is the Moonfin companion plugin for Jellyfin. Its messages are stored
on the server, so a member who was offline when one was sent still sees it the
next time they open the app. That is what separates this from
``expiry_notify``, which only reaches whoever is streaming at that moment. The
two are independent and keep separate bookkeeping on purpose: sharing
``expiry_notified_at`` would let either one suppress the other.

Two columns on ``User`` carry the state:

* ``moonbase_notice_id`` — the Moonbase message on show, if any.
* ``moonbase_notice_expires`` — the expiry that message was about. Once
  ``expires`` moves past it, or is cleared, the member renewed.
"""

import datetime
import logging

from app.extensions import db
from app.models import User
from app.services.media.service import get_client_for_media_server

# Only the Jellyfin build of Moonbase has been verified against a live server.
SUPPORTED_SERVER_TYPES = frozenset({"jellyfin"})

NOTICE_WINDOW = datetime.timedelta(hours=24)
NOTICE_DURATION = datetime.timedelta(days=7)

# End-user copy, fixed Spanish (México) like expiry_notify's. Markdown: Moonbase
# renders CommonMark.
NOTICE_TITLE = "🔔 AVISO DE VENCIMIENTO"
NOTICE_BODY = (
    "Hola 👋\n"
    "\n"
    "Tu membresía **vence dentro de 1 día**. ⏳\n"
    "\n"
    "Para evitar interrupciones en el servicio, puedes realizar tu "
    "renovación desde el siguiente enlace:\n"
    "\n"
    "👉 **Renovar mi membresía**\n"
    "\n"
    "🎬 ¡Gracias por seguir disfrutando de nuestro servicio! 🍿"
)
# A button under the message; on a TV the app shows the link as a QR code.
NOTICE_ACTION_LABEL = "Renovar mi membresía"
NOTICE_ACTION_URL = "https://neexy.net/pay?renovar=1"
# "inbox": the message does not open by itself, and the Messages button carries
# an unread badge until the member reads it. "popup" was tried first and lost on
# the TV: Moonfin 2.5.1 marks a popup read as soon as it tries to open it, and at
# app start the startup-to-home navigation can close that window straight away,
# leaving neither the window nor the badge. Inbox sends no phone push.
NOTICE_DELIVERY = "inbox"
NOTICE_COLOR = "white"


def _as_naive_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """Coerce a datetime to naive UTC for safe comparison with DB values."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(datetime.UTC).replace(tzinfo=None)
    return value


def _empty_summary() -> dict:
    return {
        "notified": 0,
        "already_notified": 0,
        "retracted": 0,
        "skipped": 0,
        "errors": 0,
    }


def sync_moonbase_expiry_notices() -> dict:
    """Take notices down for members who renewed, then notify those in their last day.

    Requires an application context. Removal runs first so that a short renewal
    still landing inside the last day gets a fresh notice in the same pass.

    Returns:
        dict: Counters — notified, already_notified, retracted, skipped, errors.
    """
    summary = _empty_summary()

    noticed = User.query.filter(User.moonbase_notice_id.is_not(None)).all()
    for user in noticed:
        if not _renewed(user):
            continue
        if _retract(user):
            summary["retracted"] += 1
        else:
            summary["errors"] += 1

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    last_day = (
        User.query.options(db.joinedload(User.server))
        .filter(
            User.expires.is_not(None),
            User.expires > now,
            User.expires <= now + NOTICE_WINDOW,
        )
        .order_by(User.expires.asc())
        .all()
    )
    for user in last_day:
        summary[_notify(user)] += 1

    return summary


def retract_notice_if_renewed(user: User) -> None:
    """Take a renewed member's notice down now rather than on the next sync.

    For the renewal endpoints, after they commit the new expiry. Never raises:
    the renewal has already happened, and the scheduled sync retries a removal
    that fails here.
    """
    try:
        if user.moonbase_notice_id and _renewed(user):
            _retract(user)
    except Exception:
        # The endpoints call this inside their own broad except, where anything
        # escaping would turn a renewal the buyer paid for into a 500.
        logging.exception("Moonbase notice check after renewal failed")


def _renewed(user: User) -> bool:
    """True when ``expires`` moved past the expiry the notice was about.

    Moving it earlier is not a renewal, and the notice still holds then.
    """
    notified_for = _as_naive_utc(user.moonbase_notice_expires)
    expires = _as_naive_utc(user.expires)
    if expires is None or notified_for is None:
        return True
    return expires > notified_for


def _notify(user: User) -> str:
    """Post the notice for one member in their last day; return the counter to bump."""
    server = user.server
    if server is None or server.server_type not in SUPPORTED_SERVER_TYPES:
        return "skipped"
    if user.moonbase_notice_id:
        return "already_notified"
    # A disabled account cannot sign in to read it, and a message addressed to
    # no Jellyfin user id is one Moonbase shows to nobody.
    if user.is_disabled or not user.token:
        return "skipped"

    try:
        client = get_client_for_media_server(server)  # type: ignore
        message_id = client.create_moonbase_message(
            title=NOTICE_TITLE,
            body=NOTICE_BODY,
            target_user_id=user.token,
            end_utc=datetime.datetime.now(datetime.UTC) + NOTICE_DURATION,
            delivery=NOTICE_DELIVERY,
            color=NOTICE_COLOR,
            action_label=NOTICE_ACTION_LABEL,
            action_url=NOTICE_ACTION_URL,
        )
    except Exception:
        logging.exception(
            "Moonbase expiry notice for %s on %s failed", user.username, server.name
        )
        return "errors"

    user.moonbase_notice_id = message_id
    user.moonbase_notice_expires = user.expires
    if not _commit():
        # The message is up but unrecorded, so the next run posts a second one.
        logging.error(
            "Moonbase message %s for %s was sent but not recorded",
            message_id,
            user.username,
        )
        return "errors"

    logging.info("📩 Moonbase expiry notice sent to %s", user.username)
    return "notified"


def _retract(user: User) -> bool:
    """Delete the member's notice from Moonbase and forget it. False on failure."""
    message_id = user.moonbase_notice_id
    try:
        server = user.server
        # The server row cascades to its users, so this only guards a broken row.
        if server is not None:
            client = get_client_for_media_server(server)  # type: ignore
            client.delete_moonbase_message(message_id)
    except Exception:
        logging.exception(
            "Removing Moonbase message %s for %s failed", message_id, user.username
        )
        return False

    user.moonbase_notice_id = None
    user.moonbase_notice_expires = None
    if not _commit():
        return False

    logging.info("🗑️ Moonbase expiry notice removed for %s", user.username)
    return True


def _commit() -> bool:
    try:
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        logging.exception("Failed to persist Moonbase notice state")
        return False
