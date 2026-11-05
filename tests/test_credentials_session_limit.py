"""The renewal ownership check must not count as one of the member's devices.

Incident 2026-11-04 (FamSoria404): a member on the 1-device plan typed the
right password into "Validar cuenta" twice and was refused both times. Jellyfin's
server log for both attempts reads "Authentication request … has succeeded",
then "Current/Max sessions 1/1" and "User is at their maximum number of
sessions" (403) — the television held the only seat, and the credential check
is a real sign-in that needs one.

Measured on tv.neexy.net (Jellyfin 12.4.0) on 2026-11-05 with throwaway users:

- the password is checked BEFORE the session limit: a wrong password with the
  seat taken is 401, a right one is 403;
- the 403 leaves no entry in /System/ActivityLog;
- changing MaxActiveSessions through the Policy endpoint keeps the open
  session alive and lets the check in; putting it back to 1 keeps the TV
  signed in while a third device is refused again.

The fix widens the limit by ONE seat, and only after Jellyfin has answered 403
(the password is checked before the seat), so a caller who does not know the
password can never open the window.

The renewal screen keeps the TV's session alive on purpose, so this hit almost
every lapsed member: 12 of the 13 accounts with a 1-device limit that day.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import MediaServer, User
from app.services.credentials import verify_media_credentials

PASSWORD = "ClaveCorrecta1"


class _SeatedJellyfin:
    """Jellyfin's sign-in rules, in the order the server applies them."""

    def __init__(
        self,
        *,
        max_sessions=1,
        open_sessions=1,
        disabled=False,
        outside_schedule=False,
        limit_writable=True,
        limit_readable=True,
    ):
        self.max_sessions = max_sessions
        self.open_sessions = open_sessions
        self.disabled = disabled
        self.outside_schedule = outside_schedule
        self.limit_writable = limit_writable
        self.limit_readable = limit_readable
        self.limit_writes: list[int] = []
        self.logged_out: list[str] = []

    # -- policy ----------------------------------------------------------
    def get_max_active_sessions(self, user_id):
        return self.max_sessions if self.limit_readable else None

    def set_max_active_sessions(self, user_id, max_sessions):
        self.limit_writes.append(max_sessions)
        if not self.limit_writable:
            return False
        self.max_sessions = max_sessions
        return True

    def is_user_disabled(self, user_id):
        return self.disabled

    def enable_user(self, user_id):
        self.disabled = False
        return True

    def disable_user(self, user_id):
        self.disabled = True
        return True

    # -- sign in ---------------------------------------------------------
    def authenticate_user(self, username, password):
        if self.disabled or self.outside_schedule:
            return False, 403, None
        if password != PASSWORD:
            return False, 401, None
        if self.max_sessions > 0 and self.open_sessions >= self.max_sessions:
            return False, 403, None
        self.open_sessions += 1
        return True, 200, "verify-token"

    def logout_token(self, token):
        self.logged_out.append(token)
        self.open_sessions -= 1


@pytest.fixture
def jellyfin(session):
    server = MediaServer(
        name="Jelly",
        server_type="jellyfin",
        url="http://jellyfin.example.com",
        api_key="k",
        verified=True,
    )
    session.add(server)
    session.commit()
    return server


def _member(session, server, *, days, disabled=False, restricted=False):
    user = User(
        username="FamSoria404",
        email="fam@example.com",
        token="jf-id-1",
        code="X",
        server_id=server.id,
        is_disabled=disabled,
        expires=datetime.now(UTC) + timedelta(days=days),
        restricted_policy='{"EnabledFolders": ["pelis"]}' if restricted else None,
    )
    session.add(user)
    session.commit()
    return user


def _verify(fake, password=PASSWORD):
    with patch(
        "app.services.credentials.get_client_for_media_server", return_value=fake
    ):
        return verify_media_credentials("famsoria404", password)


# ── The incident ────────────────────────────────────────────────────────────


def test_a_lapsed_member_with_the_tv_on_can_prove_ownership(session, jellyfin):
    user = _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)

    assert _verify(fake) == user.id


def test_the_device_limit_is_back_once_the_check_is_done(session, jellyfin):
    _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)

    _verify(fake)

    assert fake.max_sessions == 1, "the plan is 1 device; the check must not widen it"
    assert fake.limit_writes == [2, 1], "one extra seat, then back"


def test_the_tv_keeps_its_seat(session, jellyfin):
    _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)

    _verify(fake)

    assert fake.logged_out == ["verify-token"], "only the check's own session ends"
    assert fake.open_sessions == 1


def test_an_active_member_renewing_early_with_the_tv_on(session, jellyfin):
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=2, open_sessions=2)

    assert _verify(fake) == user.id
    assert fake.max_sessions == 2


# ── What must stay as it was ────────────────────────────────────────────────


def test_a_wrong_password_is_still_refused_and_the_limit_restored(session, jellyfin):
    _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)

    assert _verify(fake, password="otra-cosa") is None
    assert fake.max_sessions == 1
    assert fake.limit_writes == [], "a wrong password must never widen the limit"


def test_the_limit_is_restored_even_when_the_sign_in_blows_up(session, jellyfin):
    _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)

    first_try = fake.authenticate_user
    calls = []

    def flaky(username, password):
        calls.append(1)
        if len(calls) == 1:
            return first_try(username, password)  # 403: the TV holds the seat
        raise RuntimeError("connection reset")  # dies after the limit widened

    fake.authenticate_user = flaky

    assert _verify(fake) is None
    assert fake.max_sessions == 1
    assert fake.limit_writes == [2, 1]


def test_an_unlimited_account_is_not_touched(session, jellyfin):
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=0, open_sessions=3)

    assert _verify(fake) == user.id
    assert fake.limit_writes == []


def test_a_limit_it_cannot_read_is_left_alone(session, jellyfin):
    """Without the original value there is nothing safe to restore, so the
    check runs as before rather than guessing at the member's plan."""
    _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1, limit_readable=False)

    assert _verify(fake) is None
    assert fake.limit_writes == []


def test_a_free_seat_means_the_limit_is_never_touched(session, jellyfin):
    user = _member(session, jellyfin, days=-2, disabled=True, restricted=True)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=0)

    assert _verify(fake) == user.id
    assert fake.limit_writes == []


def test_a_limit_changed_during_the_check_is_not_overwritten(session, jellyfin):
    """An admin or a plan change wrote the limit while the seat was open:
    theirs wins over the value we read before."""
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1)
    first_try = fake.authenticate_user
    calls = []

    def upgraded_meanwhile(username, password):
        calls.append(1)
        if len(calls) == 2:
            fake.max_sessions = 4  # e.g. the store applying a 4-device plan
        return first_try(username, password)

    fake.authenticate_user = upgraded_meanwhile

    assert _verify(fake) == user.id
    assert fake.max_sessions == 4
    assert fake.limit_writes == [2]


# ── A 403 is not proof of a disabled account ────────────────────────────────


def test_a_seat_403_never_marks_an_active_member_disabled(session, jellyfin):
    """If the limit cannot be lifted, Jellyfin still answers 403 for the seat.
    Before the fix sauron read every 403 as "already disabled" and wrote
    is_disabled + disabled_externally onto a paying member."""
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=1, limit_writable=False)

    assert _verify(fake) is None

    db.session.refresh(user)
    assert user.is_disabled is False
    assert user.disabled_externally is False


def test_an_access_schedule_403_does_not_mark_the_member_disabled(session, jellyfin):
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=0, outside_schedule=True)

    assert _verify(fake) is None

    db.session.refresh(user)
    assert user.is_disabled is False


def test_a_genuinely_disabled_account_is_still_learned(session, jellyfin):
    user = _member(session, jellyfin, days=5)
    fake = _SeatedJellyfin(max_sessions=1, open_sessions=0, disabled=True)

    assert _verify(fake) is None

    db.session.refresh(user)
    assert user.is_disabled is True
    assert user.disabled_externally is True


# ── JellyfinClient.get_max_active_sessions ──────────────────────────────────


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _client_returning(payload=None, error=None):
    from app.services.media.jellyfin import JellyfinClient

    client = object.__new__(JellyfinClient)

    def get(path, **_kwargs):
        if error:
            raise error
        return _Resp(payload)

    client.get = get
    return client


@pytest.mark.parametrize(
    ("policy", "expected"),
    [({"MaxActiveSessions": 1}, 1), ({"MaxActiveSessions": 0}, 0), ({}, 0)],
)
def test_the_client_reads_the_live_limit(policy, expected):
    assert (
        _client_returning({"Policy": policy}).get_max_active_sessions("jf") == expected
    )


@pytest.mark.parametrize(
    "client",
    [
        _client_returning(None),
        _client_returning({"Policy": {"MaxActiveSessions": "1"}}),
        _client_returning(error=RuntimeError("down")),
    ],
)
def test_the_client_says_it_does_not_know(client):
    assert client.get_max_active_sessions("jf") is None
