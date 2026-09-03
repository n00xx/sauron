"""One-time repair for Jellyfin accounts whose Home screen was blanked.

Between 2026.7.9 and 2026.10.4 sauron set every Home section to "none" on each
Jellyfin account it created. On the web client that only looked austere — the
libraries stay reachable from the sidebar. On a TV it was the whole app: the
Roku client draws the Home and nothing else, so a member who linked their
television landed on an empty screen with a Search box and their own name.

That state does not heal on its own, and this is the part worth knowing before
touching any of it. Jellyfin's DisplayPreferences endpoint does NOT clear the
sections it is not sent — measured against 10.11.11, a POST omitting every
``homesection`` key returns 204 and leaves the stored values exactly as they
were. So removing the code that wrote the blanks fixes accounts created from
now on, and does nothing at all for the ones already blanked. They need real
section names written over them, which is what this does.

Runs once per install, guarded by a Settings row, and only rewrites an account
whose sections are *all* blank. A member who deliberately turned their own Home
sections off keeps that choice on the next boot: the repair reads before it
writes and skips anything it did not break.
"""

from __future__ import annotations

import structlog

from app.extensions import db
from app.models import MediaServer, Settings, User

logger = structlog.get_logger(__name__)

# Marks the repair as done. Its presence is the whole guard: the work is a
# network round trip per member, and it has nothing left to do on a second boot.
REPAIR_SETTING_KEY = "jellyfin_home_sections_repaired"


def _already_repaired() -> bool:
    return Settings.query.filter_by(key=REPAIR_SETTING_KEY).first() is not None


def _mark_repaired() -> None:
    """Record that the sweep is done, tolerating a second worker doing the same.

    ``GUNICORN_WORKERS`` can be raised above one, and then every worker runs the
    app factory and reaches this. ``Settings.key`` is unique, so the workers that
    lose the race hit an IntegrityError — which means the marker landed, i.e.
    success. Swallowing it is not enough on its own: without the rollback the
    worker carries a failed transaction into the next request and dies there
    with PendingRollbackError, on a query that had nothing to do with this.
    """
    if _already_repaired():
        return

    try:
        db.session.add(Settings(key=REPAIR_SETTING_KEY, value="1"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.debug("jellyfin.home_sections.marker_already_written")


def _repair_server(server: MediaServer) -> tuple[int, int]:
    """Repair every blanked account on one server.

    Returns ``(repaired, inspected)``. Raises nothing that the caller has to
    care about per user — a member whose account has since been deleted on the
    Jellyfin side must not stop the rest of the queue.
    """
    from app.services.media.service import get_client_for_media_server

    client = get_client_for_media_server(server)
    users = User.query.filter_by(server_id=server.id).all()

    repaired = 0
    inspected = 0

    for user in users:
        # `token` holds the Jellyfin user id for this server type.
        user_id = user.token
        if not user_id:
            continue

        try:
            inspected += 1
            if not client.home_screen_is_blank(user_id):
                continue

            client.restore_default_home_sections(user_id)
            repaired += 1
            logger.info(
                "jellyfin.home_sections.repaired",
                server=server.name,
                username=user.username,
            )
        except Exception:
            # A deleted account, a permission change, a server that went away
            # mid-sweep. None of those are worth failing the boot over, and the
            # marker is only written once the whole pass completes.
            logger.warning(
                "jellyfin.home_sections.repair_failed",
                server=server.name,
                username=user.username,
                exc_info=True,
            )

    return repaired, inspected


def repair_blank_home_sections(*, force: bool = False) -> dict[str, int]:
    """Restore a usable Home screen on every account sauron blanked.

    ``force`` ignores the completion marker, which is what a support session
    needs after restoring a database from before the fix.

    Never raises: this runs during startup, and an unreachable media server is
    an ordinary Tuesday, not a reason to keep the app from booting.
    """
    summary = {"repaired": 0, "inspected": 0, "servers": 0}

    try:
        if not force and _already_repaired():
            logger.debug("jellyfin.home_sections.repair_skipped")
            return summary

        servers = MediaServer.query.filter_by(server_type="jellyfin").all()
        if not servers:
            # Nothing to repair, and nothing to come back for either. Emby is
            # excluded on purpose: it inherits the client but stores display
            # preferences differently, and sauron never blanked it.
            _mark_repaired()
            return summary

        for server in servers:
            try:
                repaired, inspected = _repair_server(server)
            except Exception:
                logger.warning(
                    "jellyfin.home_sections.server_unreachable",
                    server=server.name,
                    exc_info=True,
                )
                continue

            summary["servers"] += 1
            summary["repaired"] += repaired
            summary["inspected"] += inspected

        _mark_repaired()

        if summary["repaired"]:
            logger.info("jellyfin.home_sections.repair_complete", **summary)

        return summary

    except Exception:
        logger.warning("jellyfin.home_sections.repair_aborted", exc_info=True)
        return summary
