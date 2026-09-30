"""Undo Jellyfin's failed-login lockout for an account whose owner has proven themselves.

Jellyfin's lockout has no duration. Once ``InvalidLoginAttemptCount`` reaches the
policy's ``LoginAttemptsBeforeLockout`` it sets ``IsDisabled`` and the account
stays off until someone clears it — the right password is refused like any other.
Only a successful sign-in zeroes the counter, so an account that is re-enabled and
then signed into with its (new) password is fully healed.

The caller decides that ownership has been proven (a used password-reset token).
This module only decides whether the disable is a lockout at all. Jellyfin does not
say why an account is disabled, so it is inferred:

* sauron records every disable it makes in ``user.is_disabled`` — the expiry sweep
  and the admin's disable button both go through it. Those are never undone here:
  an expired account comes back by paying, an admin's disable by the admin.
* the renewal page's credential check also sets ``is_disabled`` when it finds the
  account already disabled, but marks it ``disabled_externally``: sauron learned
  that disable, it did not make it. Those still count as a lockout.
* an account past its ``expires`` that the sweep has not reached yet is expired,
  not locked out, and is left for the sweep.

What remains — Jellyfin disabled, sauron active (or only aware) and unexpired —
is the lockout. The one case it cannot tell apart is an account an admin disabled
by hand straight from Jellyfin's dashboard, bypassing sauron.
"""

from __future__ import annotations

import datetime
import logging

from app.extensions import db
from app.models import User
from app.services.credentials import SUPPORTED_SERVER_TYPES, account_lock
from app.services.media.service import get_client_for_media_server

logger = logging.getLogger("wizarr.lockout")


def _is_expired(user: User) -> bool:
    if user.expires is None:
        return False
    expires = user.expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.UTC)
    return expires <= datetime.datetime.now(datetime.UTC)


def _disabled_by_sauron(user: User) -> bool:
    return bool(user.is_disabled) and not user.disabled_externally


def lift_lockout(user: User) -> bool:
    """Re-enable *user* if Jellyfin locked it out. True only when it did."""
    if _disabled_by_sauron(user) or _is_expired(user):
        return False

    server = user.server
    if server is None or server.server_type not in SUPPORTED_SERVER_TYPES:
        return False

    client = get_client_for_media_server(server)

    # Same lock as the credential check: it briefly enables and re-disables
    # accounts, and reading IsDisabled in the middle of that would misjudge it.
    with account_lock(user.id):
        # None means "could not tell": never enable on a guess.
        if client.is_user_disabled(user.token) is not True:
            return False

        if not client.enable_user(user.token):
            logger.error("Could not lift the failed-login lockout of user %s", user.id)
            return False

        if user.is_disabled or user.disabled_externally:
            # Back to what sauron knew before the credential check corrected
            # it: active, so the expiry sweep handles it when its time comes.
            user.is_disabled = False
            user.disabled_externally = False
            db.session.commit()

    logger.warning("Lifted Jellyfin's failed-login lockout of user %s", user.id)
    return True
