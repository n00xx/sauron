"""Verify a media user's OWN password — the ownership proof for self-service renewals.

Everything else in sauron authenticates ADMINS. This module answers a different
question: "is the person filling in this public form really the owner of media
account X?" It exists so a checkout flow can take money for renewing an account
without letting anyone renew (or probe) somebody else's.

Two properties of Jellyfin make that harder than it sounds, and both are the
reason this file is not a three-line wrapper:

1. A DISABLED account cannot authenticate at all. Jellyfin throws SecurityException
   before it ever checks the password (Jellyfin.Server.Implementations/Users/
   UserManager.cs). Since sauron disables accounts when they expire, the accounts
   most likely to be renewed are exactly the ones that cannot prove ownership.
   Handled by enabling for the duration of the check and restoring afterwards.

2. Failed logins LOCK THE ACCOUNT OUT. Jellyfin increments InvalidLoginAttemptCount
   and flips IsDisabled once it reaches LoginAttemptsBeforeLockout. A public form
   in front of that is a remote account-disabling weapon. Undoing the flag is the
   second line of defense here; the FIRST is the attempt cap on the route, because
   InvalidLoginAttemptCount itself has no reset API — it only zeroes on a
   successful login.

Callers get a user id or None. They must never surface anything finer: the
distinction between "no such user", "wrong password" and "account disabled" is a
user-enumeration oracle.
"""

import logging
import threading

from sqlalchemy import func

from app.extensions import db
from app.models import User
from app.services.media.service import get_client_for_media_server

# Only these back-ends can check a user's own password. Plex identities live at
# plex.tv rather than on the server, and the rest expose no comparable endpoint.
SUPPORTED_SERVER_TYPES = frozenset({"jellyfin", "emby"})

# Serialises the enable → authenticate → restore sequence for one account.
#
# Load-bearing. sauron runs ONE gunicorn process with 8 threads, so two
# verifications of the same disabled account genuinely run in parallel. Without
# this lock they interleave: A enables, B observes "enabled", A restores to
# disabled, B restores to *enabled* — leaving a lapsed account live and unpaid.
#
# Keyed by User.id, never by the submitted username: the id is bounded by real
# accounts, so an attacker feeding random names cannot grow this dict.
_user_locks: dict[int, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _lock_for(user_id: int) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


def find_user_by_username(username: str) -> User | None:
    """Case-insensitive exact lookup of a media user.

    Case-insensitive because the buyer is typing their username from memory into
    a checkout form, and Jellyfin itself treats usernames case-insensitively at
    login — an exact-match miss would read as "wrong password" to someone whose
    credentials are perfectly correct.

    Fails closed on ambiguity: with several media servers configured the same
    username can exist more than once, and we would have no way to know which
    account the buyer means. Returns None rather than guessing.
    """
    candidate = (username or "").strip()
    if not candidate:
        return None

    matches = (
        User.query.filter(func.lower(User.username) == candidate.lower()).limit(2).all()
    )
    if len(matches) != 1:
        if matches:
            logging.warning(
                "Credential check: %d accounts share a username; refusing to guess.",
                len(matches),
            )
        return None
    return matches[0]


def verify_media_credentials(username: str, password: str) -> int | None:
    """Return the sauron User.id when the password is correct, else None.

    Never raises and never distinguishes failure modes to the caller.
    """
    if not password:
        return None

    user = find_user_by_username(username)
    if user is None:
        return None

    server = user.server
    if server is None or server.server_type not in SUPPORTED_SERVER_TYPES:
        logging.info(
            "Credential check unsupported for server type %s",
            server.server_type if server else "unknown",
        )
        return None

    try:
        client = get_client_for_media_server(server)
    except Exception:
        logging.exception("Credential check: could not build media client")
        return None

    # Jellyfin's own user id, as stored by sauron.
    jf_id = user.token
    if not jf_id:
        return None

    with _lock_for(user.id):
        # The ORIGINAL state comes from sauron's OWN column, never from a fresh
        # read of the Jellyfin policy. A live read races against the temporary
        # enable performed by a concurrent verification of the same account
        # (see the _user_locks note above) — the column does not.
        original_disabled = bool(user.is_disabled)
        ok = False
        status: int | None = None

        try:
            if original_disabled:
                # Expired/disabled accounts cannot authenticate at all, so open
                # the door just long enough to check the password. The window is
                # one HTTP round-trip, held under the per-account lock, and the
                # finally-block below closes it on every path.
                client.enable_user(jf_id)

            ok, status, token = client.authenticate_user(user.username, password)
            if token:
                client.logout_token(token)
        except Exception:
            # Deliberately no exc_info payload beyond the trace: the password is
            # a local, and nothing here formats it into a message.
            logging.exception("Credential check failed unexpectedly")
        finally:
            _restore_account_state(client, user, jf_id, original_disabled, ok, status)

    return user.id if ok else None


def _restore_account_state(
    client,
    user: User,
    jf_id: str,
    original_disabled: bool,
    ok: bool,
    status: int | None,
) -> None:
    """Put the account back exactly as we found it. Never raises.

    Runs on every path out of the verification, including exceptions — an
    account left enabled by a crashed check is unpaid access.
    """
    try:
        if original_disabled:
            # Always re-disable, success or not. Passing the password proves
            # ownership; it does not pay for anything. Access is granted later,
            # by fulfillment, only once Stripe confirms the charge.
            client.disable_user(jf_id)
            return

        if ok or status == 403:
            # ok            → Jellyfin also reset InvalidLoginAttemptCount for us.
            # status == 403 → the account was ALREADY disabled before we knocked,
            #                 so our column was stale and this attempt never
            #                 reached the password check. Enabling here would
            #                 hand an attacker a way to switch on any account
            #                 that an admin disabled directly in Jellyfin. Leave
            #                 it alone and correct our own record instead.
            if status == 403 and not user.is_disabled:
                user.is_disabled = True
                db.session.commit()
            return

        # status == 401 (or unknown): the credentials were rejected while the
        # account was enabled. THIS attempt may have been the one that tripped
        # Jellyfin's lockout, which would disable a paying customer's account
        # from a public form. Reading inside the per-account lock is safe — no
        # other verification can be touching this user.
        if client.is_user_disabled(jf_id) is True:
            logging.warning(
                "Credential check tripped Jellyfin's lockout for user %s; re-enabling.",
                user.id,
            )
            client.enable_user(jf_id)
    except Exception:
        # An account stuck in the wrong state needs a human, so make it loud.
        logging.exception(
            "CRITICAL: could not restore account state for user %s after a "
            "credential check (originally %s)",
            user.id,
            "disabled" if original_disabled else "enabled",
        )
