"""Detection for members who are paid up but locked out of the media server.

`/extend` no longer creates this state, but other paths still can — chiefly
`PUT /update-expiry`, which sets an arbitrary date and deliberately does not
reactivate because it is also how an operator schedules an expiry in the past.
Without this check the first report of a locked-out customer is the customer.

An alert needs the member locked out on TWO consecutive runs: the storefront
renews with two HTTP calls, and the gap between the expiry write and the
enable looks exactly like the fault this hunts for.
"""

import datetime

import pytest

from app.extensions import db
from app.models import MediaServer, Settings, User
from app.services.renewal_health import (
    REPORTED_SETTING_KEY,
    check_locked_out_members,
    find_locked_out_members,
)


def _tick():
    """One scheduler run. An alert needs the member locked out on two in a row,
    so most tests need two of these before anything is reported."""
    return check_locked_out_members()


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="k",
    )
    db.session.add(server)
    db.session.commit()
    return server


def _member(jellyfin_server, username, *, disabled, expires_days):
    expires = None
    if expires_days is not None:
        expires = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        ) + datetime.timedelta(days=expires_days)
    user = User(
        token=f"jf-{username}",
        username=username,
        code="INVITE1",
        server_id=jellyfin_server.id,
        is_disabled=disabled,
        expires=expires,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.notifications.notify",
        lambda *a, **k: sent.append((a, k)),
        raising=True,
    )
    return sent


# ── What counts as locked out ───────────────────────────────────────────────


def test_finds_a_member_who_is_paid_up_but_disabled(jellyfin_server):
    victim = _member(jellyfin_server, "victim", disabled=True, expires_days=20)

    found = find_locked_out_members()

    assert [u.id for u in found] == [victim.id]


def test_ignores_a_lapsed_member_who_is_correctly_disabled(jellyfin_server):
    """This is the system working, not a fault."""
    _member(jellyfin_server, "lapsed", disabled=True, expires_days=-5)

    assert find_locked_out_members() == []


def test_ignores_an_active_member(jellyfin_server):
    _member(jellyfin_server, "happy", disabled=False, expires_days=20)

    assert find_locked_out_members() == []


def test_ignores_a_suspension_with_no_expiry_date(jellyfin_server):
    """A disabled account with no expiry is almost always a deliberate
    suspension — abuse, fraud, a chargeback. Alerting on those forever is how
    an alert channel gets muted."""
    _member(jellyfin_server, "suspended", disabled=True, expires_days=None)

    assert find_locked_out_members() == []


# ── Alerting, and not repeating it ──────────────────────────────────────────


def test_the_first_sighting_is_silent(jellyfin_server, _silence_notifications):
    """The neexy race: a healthy renewal writes the expiry and enables a few
    hundred milliseconds later, and in between it looks exactly like a fault."""
    _member(jellyfin_server, "mid_renewal", disabled=True, expires_days=20)

    assert _tick() == [], "a first sighting may be a renewal in flight"
    assert _silence_notifications == []


def test_a_renewal_that_completes_between_ticks_never_alerts(
    jellyfin_server, _silence_notifications
):
    victim = _member(jellyfin_server, "mid_renewal", disabled=True, expires_days=20)

    _tick()  # caught mid-renewal

    victim.is_disabled = False  # the enable call lands
    db.session.commit()

    assert _tick() == []
    assert _silence_notifications == [], "a completed renewal must stay silent"


def test_alerts_once_and_then_stays_quiet(jellyfin_server, _silence_notifications):
    _member(jellyfin_server, "victim", disabled=True, expires_days=20)

    first = _tick()
    second = _tick()
    third = _tick()
    fourth = _tick()

    assert first == [], "still unconfirmed"
    assert [u.username for u in second] == ["victim"]
    assert third == [], "a standing situation must not alert on every tick"
    assert fourth == []
    assert len(_silence_notifications) == 1


def test_the_alert_is_operational_so_it_reaches_every_agent(
    jellyfin_server, _silence_notifications
):
    from app.services.notification_events import is_operational

    _member(jellyfin_server, "victim", disabled=True, expires_days=20)

    _tick()
    _tick()

    _args, kwargs = _silence_notifications[0]
    assert kwargs["event_type"] == "membership_locked_out"
    assert is_operational("membership_locked_out"), (
        "a paying customer locked out must reach agents that never opted in"
    )


def test_a_second_victim_alerts_even_while_the_first_is_outstanding(
    jellyfin_server, _silence_notifications
):
    _member(jellyfin_server, "first", disabled=True, expires_days=20)

    _tick()
    _tick()  # "first" alerts here
    _member(jellyfin_server, "second", disabled=True, expires_days=20)
    _tick()  # "second" seen for the first time — silent
    newly = _tick()

    assert [u.username for u in newly] == ["second"]
    assert len(_silence_notifications) == 2


def test_a_fixed_member_can_alert_again_if_it_recurs(
    jellyfin_server, _silence_notifications
):
    """Forgetting a resolved id is what makes a recurrence visible."""
    victim = _member(jellyfin_server, "victim", disabled=True, expires_days=20)

    _tick()
    _tick()  # alerts

    victim.is_disabled = False  # an operator puts it right
    db.session.commit()
    assert _tick() == []

    victim.is_disabled = True  # and it happens again
    db.session.commit()
    _tick()  # first sighting of the recurrence
    again = _tick()

    assert [u.username for u in again] == ["victim"]
    assert len(_silence_notifications) == 2


def test_a_corrupted_marker_does_not_stop_the_check(
    jellyfin_server, _silence_notifications
):
    db.session.add(Settings(key=REPORTED_SETTING_KEY, value="not json at all"))
    db.session.commit()
    _member(jellyfin_server, "victim", disabled=True, expires_days=20)

    _tick()
    found = _tick()

    assert [u.username for u in found] == ["victim"]


def test_nothing_wrong_means_no_alert(jellyfin_server, _silence_notifications):
    _member(jellyfin_server, "happy", disabled=False, expires_days=20)

    assert _tick() == []
    assert _tick() == []

    assert _silence_notifications == []


def test_reads_the_marker_written_by_2026_10_2(jellyfin_server, _silence_notifications):
    """The upgrade path.

    2026.10.2 stored a bare JSON list of reported ids. It is read as `reported`
    with nothing seen, which costs one extra silent cycle after the upgrade and
    nothing else — in particular it must not re-alert about a member that
    version already reported.
    """
    import json

    victim = _member(jellyfin_server, "victim", disabled=True, expires_days=20)
    db.session.add(Settings(key=REPORTED_SETTING_KEY, value=json.dumps([victim.id])))
    db.session.commit()

    assert _tick() == [], "first run after the upgrade has nothing seen yet"
    assert _tick() == [], "already reported by the previous version"
    assert _silence_notifications == []
