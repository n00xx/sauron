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

3. The check is a REAL sign-in, so it needs one of the member's device seats.
   Jellyfin checks the password first and the session limit second: with a
   1-device plan and the TV signed in, the right password gets 403 "User is at
   their maximum number of sessions", and nothing reaches /System/ActivityLog.
   The renewal screen keeps the TV's session alive on purpose, so that was
   almost every lapsed member (incident FamSoria404, 2026-11-04). Handled like
   (1), but only after Jellyfin has accepted the password: on that 403 the
   limit is raised by one seat, the check is retried, and the limit goes back.
   Measured on Jellyfin 12.4.0: changing it through the Policy endpoint does not
   end the TV's session, and the restored limit applies to the next sign-in.

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


def account_lock(user_id: int) -> threading.Lock:
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

    with account_lock(user.id):
        # The ORIGINAL state comes from sauron's OWN column, never from a fresh
        # read of the Jellyfin policy. A live read races against the temporary
        # enable performed by a concurrent verification of the same account
        # (see the _user_locks note above) — the column does not.
        #
        # An account on the renewal screen is cut off but NOT disabled: it can
        # sign in as it is. Opening the door would hand it the catalogue for a
        # moment, and closing it again would stop whatever is on its screen.
        original_disabled = bool(user.is_disabled) and user.restricted_policy is None
        widened: tuple[int, int] | None = None
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
            if status == 403:
                # Jellyfin checks the password BEFORE the device limit, so this
                # 403 is either a refused account or a correct password with no
                # free seat. Only then is a seat made — never for a caller who
                # has not got past the password.
                widened = _widen_session_limit(client, user, jf_id)
                if widened is not None:
                    ok, status, token = client.authenticate_user(
                        user.username, password
                    )
            if token:
                client.logout_token(token)
        except Exception:
            # Deliberately no exc_info payload beyond the trace: the password is
            # a local, and nothing here formats it into a message.
            logging.exception("Credential check failed unexpectedly")
        finally:
            # Limit first: _restore_account_state may disable the account, and
            # neither step may be skipped because the other one failed.
            _restore_session_limit(client, user, jf_id, widened)
            _restore_account_state(client, user, jf_id, original_disabled, ok, status)

    if status == 403:
        # Jellyfin logs nothing about this in its ActivityLog, so say it here.
        # Never in the API answer: it would tell an attacker the account exists.
        logging.warning(
            "Credential check for user %s refused by Jellyfin with 403 "
            "(disabled, outside its access schedule, or no free device seat).",
            user.id,
        )
    return user.id if ok else None


def _widen_session_limit(client, user: User, jf_id: str) -> tuple[int, int] | None:
    """Make room for exactly one more device. Returns (original, widened) or None.

    One seat, not "unlimited": while the window is open another of the member's
    devices could take it, and this caps that at a single extra session. None
    means nothing was changed — the account is unlimited (so the 403 was not
    about seats), the limit could not be read (never restore a value we
    guessed), or the write failed. Runs under the per-account lock, so no other
    check interleaves.
    """
    reader = getattr(client, "get_max_active_sessions", None)
    if reader is None:
        return None
    limit = reader(jf_id)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return None
    widened = limit + 1
    if client.set_max_active_sessions(jf_id, widened) is not True:
        logging.warning(
            "Credential check could not make room for user %s; a member whose "
            "devices are all signed in will be refused.",
            user.id,
        )
        return None
    return limit, widened


def _restore_session_limit(
    client, user: User, jf_id: str, widened: tuple[int, int] | None
) -> None:
    """Put the member's device limit back. Never raises.

    Only if it still holds the value we wrote: anything else means someone (an
    admin, a plan change) set it meanwhile, and theirs wins. Left widened, the
    account keeps one unpaid device, so that is logged as CRITICAL.
    """
    if widened is None:
        return
    original, ours = widened
    try:
        live = client.get_max_active_sessions(jf_id)
        if live is not None and live != ours:
            logging.warning(
                "Device limit of user %s changed during a credential check "
                "(now %s); leaving it as it is.",
                user.id,
                live,
            )
            return
        if client.set_max_active_sessions(jf_id, original) is True:
            return
    except Exception:
        logging.exception("Restoring the device limit raised")
    logging.error(
        "CRITICAL: could not restore MaxActiveSessions=%s for user %s after a "
        "credential check; it allows one extra device until it is fixed.",
        original,
        user.id,
    )


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
            # status == 403 → Jellyfin refused the account itself. Enabling here
            #                 would hand an attacker a way to switch on any
            #                 account an admin disabled directly in Jellyfin, so
            #                 leave it alone. If it really is disabled our column
            #                 was stale: correct it, marked as learned so a
            #                 password reset can still tell a lockout from a
            #                 disable sauron made itself.
            #
            #                 A 403 alone is NOT that proof — an access schedule
            #                 or a full device limit answer the same — so only
            #                 the live IsDisabled flag may change our record.
            if (
                status == 403
                and not user.is_disabled
                and client.is_user_disabled(jf_id) is True
            ):
                user.is_disabled = True
                user.disabled_externally = True
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
