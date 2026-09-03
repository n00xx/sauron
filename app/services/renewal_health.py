"""Find members who are paid up but locked out of the media server.

The state this hunts for is a paying customer with a valid membership whose
account is switched off — they have no access and sauron thinks everything is
fine. Nobody notices until the customer complains, which is the expensive way
to find out.

`POST /api/users/<id>/extend` no longer produces it: reactivation happens there
before the date moves, and the call fails without extending if that reactivation
fails. What remains are the paths this cannot police:

  * `PUT /api/users/<id>/update-expiry` sets an arbitrary date and deliberately
    does NOT reactivate — it is also how an operator schedules an expiry in the
    past, so reactivating there would be wrong.
  * A caller still using the old two-step sequence, where `/enable` failed.
  * A media server that dropped the enable on the floor and answered anyway.

── Why the query is narrower than it could be ──────────────────────────────
Only members whose expiry is in the FUTURE count. A disabled account with no
expiry date at all is almost always a deliberate suspension — abuse, fraud, a
chargeback — and alerting on those forever is how an alert channel gets muted.
That does mean a deliberately suspended member with a future expiry still trips
this once; deduplication below keeps it to once rather than every tick.
"""

import datetime
import json
import logging

from app.extensions import db
from app.models import Settings, User

# Remembers which members have already been reported so a standing situation
# alerts once instead of on every scheduler tick. Stored as a JSON list of user
# ids in the generic settings table — this needs no schema of its own.
REPORTED_SETTING_KEY = "locked_out_members_reported"


def find_locked_out_members() -> list[User]:
    """Members with a membership still valid whose account is switched off.

    Ordered by id so the reported list is stable between runs.
    """
    now = datetime.datetime.now(datetime.UTC)
    return (
        User.query.filter(
            User.is_disabled.is_(True),
            User.expires.is_not(None),
            User.expires > now,
        )
        .order_by(User.id)
        .all()
    )


def _read_reported() -> set[int]:
    row = Settings.query.filter_by(key=REPORTED_SETTING_KEY).first()
    if not row or not row.value:
        return set()
    try:
        return {int(x) for x in json.loads(row.value)}
    except (ValueError, TypeError):
        # A corrupted marker must not stop the check; the worst case is one
        # repeated alert.
        return set()


def _write_reported(user_ids: set[int]) -> None:
    row = Settings.query.filter_by(key=REPORTED_SETTING_KEY).first()
    value = json.dumps(sorted(user_ids))
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=REPORTED_SETTING_KEY, value=value))
    db.session.commit()


def check_locked_out_members() -> list[User]:
    """Report members who are paid up but cannot get in.

    Returns only the NEWLY locked-out members — the ones this run is alerting
    about. Members already reported stay silent, and an id that has been put
    right is forgotten so a recurrence alerts again.
    """
    locked_out = find_locked_out_members()
    current_ids = {user.id for user in locked_out}
    already_reported = _read_reported()

    new_ids = current_ids - already_reported
    if current_ids != already_reported:
        # Rewrite rather than union: dropping ids that are no longer locked out
        # is what lets the same member alert again if it happens twice.
        _write_reported(current_ids)

    if not new_ids:
        return []

    newly = [user for user in locked_out if user.id in new_ids]
    names = ", ".join(user.username for user in newly)
    logging.warning(
        "%d member(s) have a valid membership but a disabled account: %s",
        len(newly),
        names,
    )

    try:
        from app.services.notifications import notify

        notify(
            "Members locked out",
            f"{len(newly)} member(s) have paid-up memberships but disabled "
            f"accounts and cannot sign in: {names}. Reactivate them from the "
            f"admin, or POST /api/users/<id>/enable.",
            tags="rotating_light",
            event_type="membership_locked_out",
        )
    except Exception as exc:
        # Best effort, exactly like the renewal notification: a dead agent must
        # not turn a diagnostic into a crash in the scheduler.
        logging.warning("Failed to send locked-out notification: %s", exc)

    return newly
