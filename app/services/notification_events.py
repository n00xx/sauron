"""Single source of truth for notification event types.

Before this module every event type was spelled out by hand in eight places
across six files: the column default in ``app/models.py``, the checkbox parsing
and fallback default in ``app/blueprints/notifications/routes.py`` (twice each,
for create and edit), the badge list in ``settings/notifications.html``, and the
checkbox markup in both the create and edit agent modals. Adding one event meant
eight coordinated edits, and missing any one of them produced an event that
silently reached nobody.

Deliberately free of ``app`` imports so it can be pulled in from models,
blueprints, services and templates without an import cycle.
"""

from dataclasses import dataclass

__all__ = [
    "EVENT_TYPES",
    "EventType",
    "default_subscription",
    "event_by_key",
    "is_operational",
]


@dataclass(frozen=True)
class EventType:
    """One notification event.

    Attributes:
        key: Stable identifier stored in ``Notification.notification_events``
            and passed to ``notify(event_type=...)``. Never rename one of these
            without a migration — existing agent rows store it verbatim.
        label: English UI text. Templates wrap it in ``_()`` for translation.
        badge: Tailwind classes for the badge on the agents list.
        operational: When true the event bypasses per-agent subscription and
            reaches every configured agent. Reserved for "something broke and
            someone has to act" signals: subscription is opt-in, agent rows keep
            whatever was saved when they were created, and a newly added
            operational alert would otherwise be born mute — which is the exact
            failure this module exists to prevent.
    """

    key: str
    label: str
    badge: str
    operational: bool = False


_BADGE_BLUE = "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200"
_BADGE_PURPLE = "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200"
_BADGE_GREEN = "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
_BADGE_AMBER = "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200"
_BADGE_RED = "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"


EVENT_TYPES: tuple[EventType, ...] = (
    EventType("user_joined", "User Joined", _BADGE_BLUE),
    EventType("update_available", "Update Available", _BADGE_PURPLE),
    EventType("user_renewed", "Membership Renewed", _BADGE_GREEN),
    EventType("stripe_refund", "Stripe Refund", _BADGE_AMBER, operational=True),
    EventType(
        "library_scan_failed", "Library Scan Failed", _BADGE_RED, operational=True
    ),
    EventType(
        "libraries_disabled_by_scan",
        "Libraries Disabled by Scan",
        _BADGE_RED,
        operational=True,
    ),
    EventType(
        "stripe_sync_stalled", "Stripe Sync Stalled", _BADGE_RED, operational=True
    ),
)

_BY_KEY = {event.key: event for event in EVENT_TYPES}


def event_by_key(key: str) -> EventType | None:
    """Look up an event by its stored key, or None if it is unknown."""
    return _BY_KEY.get(key)


def is_operational(key: str) -> bool:
    """Whether this event ignores per-agent subscription.

    Unknown keys are not operational: an event type retired in a later release
    must not start broadcasting to everyone just because an old agent row still
    references it.
    """
    event = _BY_KEY.get(key)
    return event.operational if event is not None else False


def default_subscription() -> str:
    """Comma-separated keys a brand-new agent is subscribed to.

    Operational events are excluded on purpose: they reach every agent through
    ``is_operational`` regardless, so listing them here would only make the
    stored value misleading about what the checkboxes control.
    """
    return ",".join(event.key for event in EVENT_TYPES if not event.operational)


def backfill_subscription(stored: str | None) -> str:
    """Add subscribable keys an existing agent row cannot know about yet.

    ``notification_events`` stores opt-INs, and a row keeps whatever was saved
    when it was created. A subscribable event added in a later release is
    therefore absent from every pre-existing row, and `notify` skips it — the
    alert reaches nobody, and the edit modal renders its checkbox unchecked, so
    saving that agent for any unrelated reason silently confirms the off state.

    Called once from the upgrade migration, never at runtime: re-running it
    would undo an admin who deliberately unticked something.
    """
    existing = [key.strip() for key in (stored or "").split(",") if key.strip()]
    missing = [
        event.key
        for event in EVENT_TYPES
        if not event.operational and event.key not in existing
    ]
    return ",".join(existing + missing) if (existing or missing) else ""


SUBSCRIBABLE_EVENT_TYPES: tuple[EventType, ...] = tuple(
    event for event in EVENT_TYPES if not event.operational
)

OPERATIONAL_EVENT_TYPES: tuple[EventType, ...] = tuple(
    event for event in EVENT_TYPES if event.operational
)
