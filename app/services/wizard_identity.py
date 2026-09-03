"""Which media accounts belong to the person currently walking the wizard.

The wizard session only ever carried ``wizard_access`` — the invitation code —
which is enough to decide *what* to show but not *who* is looking. Quick Connect
needs the who: it authorises a television against one specific Jellyfin account
using the admin API key, so getting the identity wrong hands somebody else's
library, or an administrator's, to whoever is holding the remote.

Resolving the code back to a user is NOT an acceptable substitute.
``Invitation.used_by`` is assigned as ``used_by or new_user``, so on a multi-use
invitation it pins to whoever redeemed it first and every later redeemer would
be handed that first account. The identity is recorded here at creation time
instead, while we still know it for certain.
"""

from flask import session

from app.extensions import db
from app.models import User

# Maps ``str(media_server_id) -> local User.id``. String keys because the
# session is serialised as JSON, which has no integer keys.
WIZARD_USER_IDS_KEY = "wizard_user_ids"


def remember_wizard_user(server_id: int | None, user_id: int | None) -> None:
    """Record that *user_id* was just provisioned on *server_id*.

    Called from every account-creation path that then sends the buyer into the
    wizard. Silently ignores incomplete input: a missing id is a caller that had
    nothing to record, and failing account creation over a bookkeeping entry
    would be a poor trade.

    Rebinds the whole mapping rather than mutating it in place — Flask only
    marks the session dirty on top-level assignment, so an in-place update of
    the nested dict would be dropped.
    """
    if server_id is None or user_id is None:
        return

    known = dict(session.get(WIZARD_USER_IDS_KEY) or {})
    known[str(server_id)] = user_id
    session[WIZARD_USER_IDS_KEY] = known


def forget_wizard_users() -> None:
    """Drop the recorded accounts, e.g. when the wizard session is torn down."""
    session.pop(WIZARD_USER_IDS_KEY, None)


def current_wizard_user(server_type: str) -> User | None:
    """The account this session provisioned on a *server_type* server, if any.

    Reads exclusively from server-set session state. Nothing in the request
    influences which account comes back — that is the whole security property
    the Quick Connect endpoint rests on.

    Returns None when the session predates this bookkeeping (a wizard opened
    before the feature shipped) so callers can degrade instead of guessing.
    """
    known = session.get(WIZARD_USER_IDS_KEY) or {}
    if not isinstance(known, dict):
        return None

    for user_id in known.values():
        user = db_get_user(user_id)
        if user is None:
            continue
        server = getattr(user, "server", None)
        if server is not None and server.server_type == server_type:
            return user
    return None


def db_get_user(user_id) -> User | None:
    """Load a ``User`` by primary key, tolerating a stale session entry.

    An account deleted between provisioning and the wizard step is expected
    (an admin cleaning up a test purchase), not exceptional.
    """
    try:
        return db.session.get(User, user_id)
    except Exception:
        return None
