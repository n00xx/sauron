"""Keep members on the renewal screen from requesting titles in Seerr.

A restricted account can still sign in to Jellyfin, so it can sign in to Seerr
too — and Moonfin reaches Seerr through Moonbase with the member's OWN Seerr
session, so Seerr's per-user permissions are what decide. Measured on Seerr
3.5.0 with a throwaway user: with ``permissions`` at 0, POST /api/v1/request
answers 403 "You do not have permission to make movie requests."

So while a member is on the renewal screen their Seerr permissions are zeroed
and the originals kept in ``User.seerr_saved_permissions``; renewal puts them
back. Seerr imports a member on first sign-in with the default permissions, so
``sync_seerr_access`` re-checks every expiry run and catches anyone who opens
Seerr after being restricted.

Seerr is the "overseerr" companion connection of the member's server (URL and
API key). Without one this module does nothing. Nothing here raises, and
nothing commits except ``sync_seerr_access``: the renewal-screen calls run
inside the expiry sweep's savepoint.
"""

import logging

import requests
from sqlalchemy import or_

from app.extensions import db
from app.models import Connection, User

CONNECTION_TYPE = "overseerr"
NO_PERMISSIONS = 0
# Seerr's ADMIN bit. An admin's permissions are never touched.
ADMIN_PERMISSION = 2
TIMEOUT_SECONDS = 15

log = logging.getLogger(__name__)


def _connection(user: User) -> Connection | None:
    conn = Connection.query.filter_by(
        media_server_id=user.server_id, connection_type=CONNECTION_TYPE
    ).first()
    if conn is None or not conn.url or not conn.api_key:
        return None
    return conn


def _api(conn: Connection, path: str) -> str:
    return f"{conn.url.rstrip('/')}/api/v1{path}"


def _key(jellyfin_id: str | None) -> str:
    return (jellyfin_id or "").replace("-", "").lower()


def _seerr_users(conn: Connection) -> dict[str, dict]:
    """Seerr users keyed by their Jellyfin id. Raises on transport errors."""
    response = requests.get(
        _api(conn, "/user"),
        headers={"X-Api-Key": conn.api_key},
        params={"take": 1000},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return {
        _key(u.get("jellyfinUserId")): u
        for u in response.json().get("results", [])
        if u.get("jellyfinUserId")
    }


def _set_permissions(conn: Connection, seerr_id: int, permissions: int) -> None:
    response = requests.post(
        _api(conn, f"/user/{seerr_id}/settings/permissions"),
        headers={"X-Api-Key": conn.api_key},
        json={"permissions": permissions},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _suspend(conn: Connection, user: User, seerr_users: dict[str, dict]) -> bool:
    seerr_user = seerr_users.get(_key(user.token))
    if seerr_user is None:
        return False  # never signed in to Seerr; the next pass checks again
    permissions = seerr_user.get("permissions") or 0
    if permissions & ADMIN_PERMISSION:
        log.warning("Seerr user %s is an admin; not suspending", seerr_user["id"])
        return False
    # Keep the FIRST value seen: on a later pass Seerr already holds our 0.
    if user.seerr_saved_permissions is None:
        user.seerr_saved_permissions = permissions
    if permissions != NO_PERMISSIONS:
        _set_permissions(conn, seerr_user["id"], NO_PERMISSIONS)
    return True


def _restore(conn: Connection, user: User, seerr_users: dict[str, dict]) -> None:
    seerr_user = seerr_users.get(_key(user.token))
    if seerr_user is not None:
        _set_permissions(conn, seerr_user["id"], user.seerr_saved_permissions)
    # Gone from Seerr: nothing left to give back.
    user.seerr_saved_permissions = None


def suspend_requests(user: User) -> bool:
    """Zero the member's Seerr permissions, saving the originals.

    True when the member is now unable to request. False when there is no
    Seerr connection, the member is not in Seerr yet, or Seerr failed — the
    scheduled pass tries again.
    """
    conn = _connection(user)
    if conn is None:
        return False
    try:
        return _suspend(conn, user, _seerr_users(conn))
    except Exception as exc:
        log.warning("Could not suspend Seerr requests for user %s: %s", user.id, exc)
        return False


def restore_requests(user: User) -> bool:
    """Give the member back the Seerr permissions saved on suspension.

    True when there is nothing left to restore. False keeps the saved value for
    the scheduled pass to retry.
    """
    if user.seerr_saved_permissions is None:
        return True
    conn = _connection(user)
    if conn is None:
        return False
    try:
        _restore(conn, user, _seerr_users(conn))
        return True
    except Exception as exc:
        log.warning("Could not restore Seerr requests for user %s: %s", user.id, exc)
        return False


def sync_seerr_access() -> dict:
    """Suspend everyone on the renewal screen; restore everyone who left it.

    One Seerr listing per connection per run. Requires an app context; commits.
    """
    summary = {"suspended": 0, "restored": 0, "errors": 0}
    users = User.query.filter(
        or_(
            User.restricted_policy.is_not(None),
            User.seerr_saved_permissions.is_not(None),
        )
    ).all()

    listings: dict[int, dict[str, dict] | None] = {}
    for user in users:
        conn = _connection(user)
        if conn is None:
            continue
        try:
            if conn.id not in listings:
                listings[conn.id] = _seerr_users(conn)
            seerr_users = listings[conn.id]
            if user.restricted_policy is not None:
                before = seerr_users.get(_key(user.token), {}).get("permissions")
                if _suspend(conn, user, seerr_users) and before != NO_PERMISSIONS:
                    summary["suspended"] += 1
            else:
                _restore(conn, user, seerr_users)
                summary["restored"] += 1
        except Exception as exc:
            summary["errors"] += 1
            log.warning("Seerr access sync failed for user %s: %s", user.id, exc)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Could not save Seerr access state")
        summary["errors"] += 1
    return summary
