"""Is this username free on the servers an invitation grants?

Backs the join form's "check as you leave the field" hint. It is a courtesy,
not the gate: the join itself re-checks (and Jellyfin has the final word), so
every failure here degrades to "not known to be taken" rather than to an error.

Two sources, because neither alone is complete:

* sauron's own ``user`` table — instant, and it already holds every account
  created through an invitation;
* the media server's live user list — the only place that knows accounts made
  straight in Jellyfin (the admin, say), which a join would still collide with.
  Cached briefly per server: someone tabbing through the form must not cost a
  Jellyfin round trip per keystroke, and accounts created through sauron in the
  meantime are in the table anyway.

Both comparisons ignore case, like Jellyfin does: with "juan" taken, Jellyfin
refuses "Juan" too.
"""

from __future__ import annotations

import logging
import re
import time

from app.extensions import db
from app.forms.validators import (
    JOIN_USERNAME_MAX_LENGTH,
    JOIN_USERNAME_MIN_LENGTH,
    JOIN_USERNAME_PATTERN,
)
from app.models import MediaServer, User
from app.services.media.service import get_client_for_media_server

logger = logging.getLogger(__name__)

TAKEN = "taken"
INVALID = "invalid"

# Server types whose client exposes GET /Users with a Name per account.
_LIVE_LOOKUP_TYPES = frozenset({"jellyfin"})
_LIVE_TTL_SECONDS = 60.0
_LIVE_CACHE: dict[int, tuple[float, frozenset[str]]] = {}


def _live_usernames(server: MediaServer) -> frozenset[str]:
    """Lowercased names on the media server, or empty when it cannot say."""
    if server.server_type not in _LIVE_LOOKUP_TYPES:
        return frozenset()
    now = time.monotonic()
    cached = _LIVE_CACHE.get(server.id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        users = get_client_for_media_server(server).get("/Users").json()
        names = frozenset(
            str(u.get("Name", "")).lower() for u in users if isinstance(u, dict)
        )
    except Exception as exc:  # degrade, never block the form
        logger.warning(
            "username check: live lookup failed for server %s: %s", server.id, exc
        )
        return frozenset()
    _LIVE_CACHE[server.id] = (now + _LIVE_TTL_SECONDS, names)
    return names


def check_username(username: str, servers: list[MediaServer]) -> str | None:
    """None when free; ``INVALID`` when it breaks the join rules; ``TAKEN`` when it exists."""
    name = (username or "").strip()
    if not (
        JOIN_USERNAME_MIN_LENGTH <= len(name) <= JOIN_USERNAME_MAX_LENGTH
        and re.fullmatch(JOIN_USERNAME_PATTERN, name)
    ):
        return INVALID

    lowered = name.lower()
    for server in servers:
        known = User.query.filter(
            User.server_id == server.id,
            db.func.lower(User.username) == lowered,
        ).first()
        if known or lowered in _live_usernames(server):
            return TAKEN
    return None
