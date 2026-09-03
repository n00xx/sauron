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

── Why an alert needs TWO consecutive sightings ────────────────────────────
Because the storefront's renewal is two HTTP calls, and the gap between them
looks exactly like the fault this hunts for. neexy sets the expiry first and
enables second — deliberately, since the reverse order would leave an account
enabled but still expired for the sweep to disable again — so for the few
hundred milliseconds between those calls, a perfectly healthy renewal reads as
"valid membership, account switched off".

A run that sees a member for the first time therefore records it and says
nothing. Only a member still locked out on the NEXT run is real. The cost is
one cycle of delay on a genuine alert; the alternative is a false positive on
an operational channel that reaches every agent, which is how a channel stops
being read.
"""

import datetime
import json
import logging

from app.extensions import db
from app.models import Settings, User

# Holds {"seen": [...], "reported": [...]} as JSON in the generic settings
# table — no schema of its own. `seen` is what the previous run observed, which
# is what makes the two-sighting rule possible; `reported` is what has already
# been alerted, so a standing situation stays quiet.
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


def _read_state() -> tuple[set[int], set[int]]:
    """Return ``(seen, reported)`` from the settings row.

    Tolerates the shape 2026.10.2 wrote — a bare JSON list of reported ids —
    by reading it as `reported` with nothing seen. That costs one extra cycle
    before the first alert after the upgrade and nothing else.

    A corrupted marker must not stop the check; the worst case is one repeated
    alert, which beats a diagnostic that silently stops running.
    """
    row = Settings.query.filter_by(key=REPORTED_SETTING_KEY).first()
    if not row or not row.value:
        return set(), set()

    try:
        parsed = json.loads(row.value)
        if isinstance(parsed, list):  # 2026.10.2 format
            return set(), {int(x) for x in parsed}
        return (
            {int(x) for x in parsed.get("seen", [])},
            {int(x) for x in parsed.get("reported", [])},
        )
    except (ValueError, TypeError, AttributeError):
        return set(), set()


def _write_state(seen: set[int], reported: set[int]) -> None:
    value = json.dumps({"seen": sorted(seen), "reported": sorted(reported)})
    row = Settings.query.filter_by(key=REPORTED_SETTING_KEY).first()
    if row:
        row.value = value
    else:
        db.session.add(Settings(key=REPORTED_SETTING_KEY, value=value))
    db.session.commit()


def check_locked_out_members() -> list[User]:
    """Report members who are paid up but cannot get in.

    Returns only the members this run is alerting about: locked out now, locked
    out on the previous run too, and not already reported. A member seen for the
    first time is recorded silently — see the two-sightings note at the top of
    this module.
    """
    locked_out = find_locked_out_members()
    current_ids = {user.id for user in locked_out}
    previously_seen, already_reported = _read_state()

    # Two sightings: anything that appeared only now may be the millisecond gap
    # between a storefront's expiry write and its enable.
    confirmed = current_ids & previously_seen
    new_ids = confirmed - already_reported

    # `reported` is intersected with what is still locked out so a member who
    # was put right is forgotten — that is what lets a recurrence alert again.
    _write_state(current_ids, (already_reported | new_ids) & current_ids)

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
