"""Does this email already have an account on the servers an invitation grants?

Backs the join form's "check as you leave the field" hint for the email, next to
the one for the username (``username_availability``). Same standing: a courtesy,
not the gate — the join re-checks and has the final word.

Only sauron's own ``user`` table can answer. Jellyfin keeps no email for its
accounts, so unlike a username there is no live list to consult.

Compared without case, as the join does.
"""

from __future__ import annotations

from app.extensions import db
from app.forms.validators import normalize_bound_email
from app.models import MediaServer, User
from app.services.username_availability import INVALID, TAKEN


def check_email(email: object, servers: list[MediaServer]) -> str | None:
    """None when free; ``INVALID`` when it is not an address; ``TAKEN`` when it has an account."""
    try:
        normalized = normalize_bound_email(email)
    except ValueError:
        return INVALID

    server_ids = [server.id for server in servers]
    known = User.query.filter(
        User.server_id.in_(server_ids),
        db.func.lower(User.email) == normalized,
    ).first()
    return TAKEN if known else None
