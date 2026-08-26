"""Notification event catalogue and subscription filtering.

The catalogue exists because every event type used to be spelled out in eight
places across six files, and missing one of them produced an event that
silently reached nobody. These tests pin the properties that made that failure
mode possible.
"""

from unittest.mock import patch

from app.extensions import db
from app.models import Notification
from app.services.notification_events import (
    EVENT_TYPES,
    OPERATIONAL_EVENT_TYPES,
    SUBSCRIBABLE_EVENT_TYPES,
    backfill_subscription,
    default_subscription,
    event_by_key,
    is_operational,
)
from app.services.notifications import notify


def test_event_keys_are_unique():
    keys = [event.key for event in EVENT_TYPES]
    assert len(keys) == len(set(keys))


def test_catalogue_splits_cleanly_into_subscribable_and_operational():
    assert set(SUBSCRIBABLE_EVENT_TYPES) | set(OPERATIONAL_EVENT_TYPES) == set(
        EVENT_TYPES
    )
    assert not set(SUBSCRIBABLE_EVENT_TYPES) & set(OPERATIONAL_EVENT_TYPES)


def test_default_subscription_lists_only_subscribable_events():
    """Operational keys in the stored value would misrepresent the checkboxes.

    They are delivered through is_operational regardless of what is stored.
    """
    default = default_subscription().split(",")
    assert default == [event.key for event in SUBSCRIBABLE_EVENT_TYPES]
    assert all(not is_operational(key) for key in default)


def test_unknown_event_is_not_operational():
    """A retired event key must not start broadcasting to every agent."""
    assert event_by_key("retired_event_from_2019") is None
    assert is_operational("retired_event_from_2019") is False


def _agent(app, session, *, events: str) -> None:
    with app.app_context():
        Notification.query.delete()
        db.session.add(
            Notification(
                name="Test agent",
                type="ntfy",
                url="https://ntfy.example/test",
                notification_events=events,
            )
        )
        db.session.commit()


def test_unsubscribed_event_is_not_delivered(app, session):
    _agent(app, session, events="user_joined")

    with app.app_context(), patch("app.services.notifications._ntfy") as mock_ntfy:
        notify("Title", "Body", tags="tada", event_type="update_available")

    assert not mock_ntfy.called


def test_subscribed_event_is_delivered(app, session):
    _agent(app, session, events="user_joined,update_available")

    with app.app_context(), patch("app.services.notifications._ntfy") as mock_ntfy:
        notify("Title", "Body", tags="tada", event_type="update_available")

    assert mock_ntfy.called


def test_operational_event_reaches_an_agent_that_never_subscribed(app, session):
    """The point of the operational class: an alert cannot be born mute.

    Agent rows keep whatever was saved when they were created, so a newly added
    operational event would otherwise reach nobody until someone remembered to
    tick a box — which is the failure this whole change exists to prevent.
    """
    _agent(app, session, events="user_joined")

    with app.app_context(), patch("app.services.notifications._ntfy") as mock_ntfy:
        notify("Title", "Body", tags="warning", event_type="library_scan_failed")

    assert mock_ntfy.called


def test_backfill_subscribes_a_pre_existing_agent_to_new_events():
    """An agent row written before an event existed must be caught up.

    notification_events stores opt-INs, so a subscribable event added in a
    later release is absent from every pre-existing row and notify() skips it.
    The upgrade migration runs this over each row; without it, user_renewed
    would reach nobody in any database that predates it.
    """
    stored = "user_joined,update_available"

    assert backfill_subscription(stored) == "user_joined,update_available,user_renewed"


def test_backfill_never_adds_operational_events():
    """They bypass subscription anyway; storing them would misrepresent the UI."""
    result = backfill_subscription("user_joined").split(",")

    assert all(not is_operational(key) for key in result)


def test_backfill_preserves_order_and_is_idempotent():
    """Re-running must not duplicate keys or reorder an admin's stored value."""
    once = backfill_subscription("update_available")

    assert once.split(",")[0] == "update_available"
    assert backfill_subscription(once) == once


def test_backfill_of_an_empty_value_matches_a_new_agent():
    assert backfill_subscription("") == default_subscription()
    assert backfill_subscription(None) == default_subscription()


def test_backfilled_agent_receives_the_new_event(app, session):
    """End to end: after the backfill the agent actually gets the alert."""
    _agent(app, session, events=backfill_subscription("user_joined,update_available"))

    with app.app_context(), patch("app.services.notifications._ntfy") as mock_ntfy:
        notify("Title", "Body", tags="tada", event_type="user_renewed")

    assert mock_ntfy.called
