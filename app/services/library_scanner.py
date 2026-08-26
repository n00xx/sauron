"""Library scanning service for media servers.

This service handles scanning and synchronizing library metadata from media servers
into the local database during application startup.

This implementation performs an upsert (update existing, insert new) and soft-deletes
(or removes) libraries that no longer exist on the remote server. Libraries that are
referenced by invitations are preserved (disabled) instead of being hard-deleted so
invite<->library associations remain intact.
"""

import logging

logger = logging.getLogger(__name__)

# Timeout for individual server library scans (in seconds)
# This prevents a single unreachable server from blocking startup
LIBRARY_SCAN_TIMEOUT = 15


def _notify_scan_anomaly(event_type: str, title: str, message: str) -> None:
    """Best-effort operational alert about a library scan.

    Isolated behind its own try/except because this runs during startup inside
    the per-server error handler: an exception escaping here (an agent table
    that does not exist yet on a fresh install, a notification endpoint that is
    down) would be recorded as a scan failure, which it is not.
    """
    try:
        from app.services.notifications import notify

        notify(title, message, tags="warning", event_type=event_type)
    except Exception as exc:
        logger.warning(f"Failed to send {event_type} notification: {exc}")


def scan_all_server_libraries(show_logs: bool = True) -> tuple[int, list[str]]:
    """Scan libraries for all configured media servers.

    Args:
        show_logs: Whether to output log messages during scanning

    Returns:
        Tuple of (total_scanned, error_messages)
        - total_scanned: Number of libraries successfully scanned
        - error_messages: List of error messages for failed scans
    """
    from sqlalchemy import inspect

    from app.extensions import db
    from app.models import Library, MediaServer, invite_libraries
    from app.services.media.service import get_client_for_media_server

    # Check if the library table exists (in case migrations haven't run yet)
    inspector = inspect(db.engine)
    if not inspector.has_table("library"):
        if show_logs:
            logger.info("Library table doesn't exist yet - skipping scan")
        raise Exception("Library table not found - run migrations first")

    servers = MediaServer.query.all()
    total_scanned = 0
    errors = []

    for server in servers:
        try:
            client = get_client_for_media_server(server)
            libraries_dict = client.libraries()  # {external_id: name}

            # Normalize to dict if client returned list-like
            if not isinstance(libraries_dict, dict):
                libraries_dict = {
                    str(k): str(v)
                    for k, v in (
                        libraries_dict.items()
                        if hasattr(libraries_dict, "items")
                        else libraries_dict
                    )
                }

            # Load existing libraries for this server keyed by external_id
            existing_libs = {
                lib.external_id: lib
                for lib in Library.query.filter_by(server_id=server.id).all()
            }
            old_count = len(existing_libs)

            incoming_ids = set()
            updated_count = 0
            inserted_count = 0
            disabled_count = 0
            deleted_count = 0

            # Upsert incoming libraries: update existing, insert new
            for external_id, name in libraries_dict.items():
                incoming_ids.add(str(external_id))
                if external_id in existing_libs:
                    lib = existing_libs[external_id]
                    # Update mutable attributes while preserving primary key.
                    # `enabled` is left alone on purpose: it holds the admin's
                    # per-library choice, and this scan runs on every startup, so
                    # resetting it here would undo that choice on every restart.
                    # Trade-off: a library that disappeared from the server (and was
                    # disabled below) stays disabled if it comes back, until the
                    # admin re-checks it. Under-granting is the safer failure.
                    lib.name = name
                    updated_count += 1
                else:
                    lib = Library(
                        external_id=external_id,
                        name=name,
                        server_id=server.id,
                        enabled=True,
                    )
                    db.session.add(lib)
                    inserted_count += 1
                total_scanned += 1

            # A server that answers with nothing while we hold rows for it is
            # far more likely to be broken than genuinely emptied: an
            # unreachable media server, a client that swallows its own error, a
            # half-started container. The pass below is destructive and
            # irreversible in practice (disabled libraries are never
            # re-enabled by design), so refuse to run it on that evidence.
            if not libraries_dict and existing_libs:
                db.session.rollback()
                msg = (
                    f"{server.name} returned zero libraries while {old_count} are "
                    f"on record — skipping the disable/delete pass"
                )
                errors.append(msg)
                if show_logs:
                    logger.warning(msg)
                _notify_scan_anomaly(
                    "library_scan_failed",
                    "Library scan returned nothing",
                    f"{server.name} reported zero libraries but {old_count} are on "
                    f"record. Nothing was disabled or deleted. Check that the media "
                    f"server is reachable.",
                )
                continue

            # Handle libraries that used to exist but weren't returned by the server
            for ext, lib in existing_libs.items():
                if ext not in incoming_ids:
                    # If this library is referenced by any invitation, disable it to preserve associations
                    referenced = db.session.execute(
                        invite_libraries.select().where(
                            invite_libraries.c.library_id == lib.id
                        )
                    ).first()
                    if referenced:
                        if lib.enabled:
                            lib.enabled = False
                            disabled_count += 1
                    else:
                        # Safe to remove since no invites reference it
                        db.session.delete(lib)
                        deleted_count += 1

            db.session.commit()

            if show_logs:
                logger.info(
                    f"Refreshed {len(libraries_dict)} libraries for {server.name} "
                    f"(existing={old_count}, updated={updated_count}, inserted={inserted_count}, "
                    f"disabled={disabled_count}, deleted={deleted_count})"
                )

            # Losing libraries is normal when an admin removes one, but it also
            # silently shrinks what every future invitation grants, so it is
            # worth saying out loud either way.
            if disabled_count or deleted_count:
                _notify_scan_anomaly(
                    "libraries_disabled_by_scan",
                    "Libraries removed by scan",
                    f"{server.name}: {disabled_count} librar"
                    f"{'y' if disabled_count == 1 else 'ies'} disabled and "
                    f"{deleted_count} deleted because the server stopped listing "
                    f"them. Disabled libraries are not re-enabled automatically.",
                )
        except Exception as server_exc:
            # Rollback on error to keep session clean
            db.session.rollback()
            error_msg = f"Failed to scan libraries for {server.name}: {server_exc}"
            errors.append(error_msg)
            if show_logs:
                logger.warning(error_msg)
            _notify_scan_anomaly(
                "library_scan_failed",
                "Library scan failed",
                f"Could not scan libraries for {server.name}: {server_exc}",
            )

    return total_scanned, errors
