"""The renewal screen, sauron side: who gets it, how it is undone, what it touches.

Every enable and disable goes through ``_set_user_enabled_state``, so the mode
lives there: the expiry sweep, POST /enable, /disable, /extend and the admin
toggles all follow it without knowing. ``is_disabled`` keeps meaning "access
cut" — it is the sweep's own filter — and ``restricted_policy`` says HOW it was
cut, holding what has to go back on renewal.
"""

import datetime
import json
from unittest.mock import MagicMock

import pytest

from app.extensions import db
from app.forms.general import GeneralSettingsForm
from app.models import ExpiredUser, MediaServer, Settings, User
from app.services import credentials
from app.services.expiry import (
    disable_or_delete_user_if_expired,
    restrict_lapsed_disabled_accounts,
)
from app.services.media.service import (
    disable_user,
    enable_user,
    restrict_for_expiry,
)

SNAPSHOT = {
    "EnableAllFolders": False,
    "EnabledFolders": ["pelis", "series", "cols"],
    "EnableLiveTvAccess": False,
    "EnableContentDownloading": True,
}


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    monkeypatch.setattr(
        "app.services.notifications.notify", lambda *a, **k: None, raising=True
    )


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def jellyfin(monkeypatch):
    """The media client every service call gets. Restricting succeeds by default."""
    client = MagicMock()
    client.restrict_user.return_value = dict(SNAPSHOT)
    client.unrestrict_user.return_value = True
    client.enable_user.return_value = True
    client.disable_user.return_value = True
    client.end_sessions_with_notice.return_value = 1
    monkeypatch.setattr(
        "app.services.media.service.get_client_for_media_server",
        lambda _server: client,
    )
    return client


def _mode(value):
    db.session.add(Settings(key="expiry_action", value=value))
    db.session.commit()


def _swept(user):
    """The ExpiredUser row the sweep writes when IT disables an account."""
    db.session.add(
        ExpiredUser(
            original_user_id=user.id,
            username=user.username,
            email=user.email,
            invitation_code=user.code,
            server_id=user.server_id,
            expired_at=user.expires,
        )
    )
    db.session.commit()
    return user


def _member(server, *, days, disabled=False, restricted=False, **extra):
    user = User(
        token="jf-1",
        username="member",
        email="member@example.com",
        code="INVITE1",
        server_id=server.id,
        is_disabled=disabled,
        expires=(
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            + datetime.timedelta(days=days)
        ),
        restricted_policy=json.dumps(SNAPSHOT) if restricted else None,
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user


# ── Cutting access ──────────────────────────────────────────────────────────


def test_a_lapsed_member_gets_the_renewal_screen_instead_of_a_disabled_account(
    jellyfin_server, jellyfin
):
    _mode("restrict")
    user = _member(jellyfin_server, days=-1)

    assert restrict_for_expiry(user.id) is True

    jellyfin.restrict_user.assert_called_once_with("jf-1", lift_disable=False)
    jellyfin.disable_user.assert_not_called()
    assert user.is_disabled is True
    assert json.loads(user.restricted_policy) == SNAPSHOT


def test_whoever_is_watching_is_stopped_and_told_why(jellyfin_server, jellyfin):
    _mode("restrict")
    user = _member(jellyfin_server, days=-1)

    restrict_for_expiry(user.id)

    jellyfin.end_sessions_with_notice.assert_called_once()
    assert jellyfin.end_sessions_with_notice.call_args.args[0] == "jf-1"


def test_a_member_still_in_date_is_disabled_as_before(jellyfin_server, jellyfin):
    """A suspension of a member who has NOT lapsed is not a renewal problem."""
    _mode("restrict")
    user = _member(jellyfin_server, days=10)

    restrict_for_expiry(user.id)

    jellyfin.disable_user.assert_called_once_with("jf-1")
    jellyfin.restrict_user.assert_not_called()
    assert user.restricted_policy is None


def test_disable_mode_keeps_disabling(jellyfin_server, jellyfin):
    _mode("disable")
    user = _member(jellyfin_server, days=-1)

    restrict_for_expiry(user.id)

    jellyfin.disable_user.assert_called_once_with("jf-1")
    jellyfin.restrict_user.assert_not_called()


def test_a_deliberate_disable_of_a_lapsed_member_really_disables(
    jellyfin_server, jellyfin
):
    """POST /disable and the admin's toggle are a decision about the account,
    not an expiry: they must not hand it a sign-in back."""
    _mode("restrict")
    user = _member(jellyfin_server, days=-1)

    disable_user(user.id)

    jellyfin.disable_user.assert_called_once_with("jf-1")
    jellyfin.restrict_user.assert_not_called()
    assert user.restricted_policy is None


def test_restricting_twice_keeps_the_first_snapshot(jellyfin_server, jellyfin):
    """A second snapshot would save the RESTRICTED policy as the one to restore."""
    _mode("restrict")
    user = _member(jellyfin_server, days=-1, disabled=True, restricted=True)

    assert restrict_for_expiry(user.id) is True

    jellyfin.restrict_user.assert_not_called()
    assert json.loads(user.restricted_policy) == SNAPSHOT


def test_a_refused_restriction_changes_nothing(jellyfin_server, jellyfin):
    _mode("restrict")
    jellyfin.restrict_user.return_value = None
    user = _member(jellyfin_server, days=-1)

    assert restrict_for_expiry(user.id) is False

    assert user.is_disabled is False
    assert user.restricted_policy is None


# ── Giving access back ──────────────────────────────────────────────────────


def test_renewal_restores_the_saved_libraries(jellyfin_server, jellyfin):
    _mode("restrict")
    user = _member(jellyfin_server, days=-1, disabled=True, restricted=True)

    assert enable_user(user.id) is True

    jellyfin.unrestrict_user.assert_called_once_with("jf-1", SNAPSHOT)
    jellyfin.enable_user.assert_not_called()
    assert user.is_disabled is False
    assert user.restricted_policy is None


def test_a_refused_restore_leaves_the_member_restricted(jellyfin_server, jellyfin):
    _mode("restrict")
    jellyfin.unrestrict_user.return_value = False
    user = _member(jellyfin_server, days=-1, disabled=True, restricted=True)

    assert enable_user(user.id) is False

    assert user.is_disabled is True
    assert json.loads(user.restricted_policy) == SNAPSHOT


def test_a_corrupt_snapshot_still_restores_access(jellyfin_server, jellyfin):
    _mode("restrict")
    user = _member(jellyfin_server, days=-1, disabled=True)
    user.restricted_policy = "{not json"
    db.session.commit()

    assert enable_user(user.id) is True

    jellyfin.unrestrict_user.assert_called_once_with("jf-1", None)


def test_restoring_works_after_switching_back_to_disable_mode(
    jellyfin_server, jellyfin
):
    """Changing the setting must not strand members already on the screen."""
    _mode("disable")
    user = _member(jellyfin_server, days=-1, disabled=True, restricted=True)

    enable_user(user.id)

    jellyfin.unrestrict_user.assert_called_once()
    assert user.restricted_policy is None


def test_enabling_a_plainly_disabled_account_still_just_enables(
    jellyfin_server, jellyfin
):
    _mode("restrict")
    user = _member(jellyfin_server, days=-1, disabled=True)

    enable_user(user.id)

    jellyfin.enable_user.assert_called_once_with("jf-1")
    jellyfin.unrestrict_user.assert_not_called()


# ── The sweep and the members disabled before the switch ────────────────────


def test_the_sweep_puts_lapsed_members_on_the_renewal_screen(
    jellyfin_server, jellyfin, monkeypatch
):
    monkeypatch.setattr("app.services.expiry.time.sleep", lambda _s: None)
    _mode("restrict")
    user = _member(jellyfin_server, days=-1)

    assert disable_or_delete_user_if_expired() == [user.id]

    db.session.refresh(user)
    assert user.is_disabled is True
    assert json.loads(user.restricted_policy) == SNAPSHOT
    jellyfin.disable_user.assert_not_called()


def test_members_disabled_before_the_switch_move_to_the_renewal_screen(
    jellyfin_server, jellyfin
):
    _mode("restrict")
    user = _swept(_member(jellyfin_server, days=-3, disabled=True))

    assert restrict_lapsed_disabled_accounts() == [user.id]

    jellyfin.restrict_user.assert_called_once_with("jf-1", lift_disable=True)
    db.session.refresh(user)
    assert user.is_disabled is True
    assert json.loads(user.restricted_policy) == SNAPSHOT


def test_moving_them_happens_once(jellyfin_server, jellyfin):
    _mode("restrict")
    _swept(_member(jellyfin_server, days=-3, disabled=True))

    restrict_lapsed_disabled_accounts()
    assert restrict_lapsed_disabled_accounts() == []

    assert jellyfin.restrict_user.call_count == 1


@pytest.mark.parametrize(
    ("mode", "days", "extra"),
    [
        ("disable", -3, {}),  # the admin did not ask for the screen
        ("restrict", 5, {}),  # suspended, not lapsed
        ("restrict", -3, {"disabled_externally": True}),  # a lockout, not sauron
    ],
)
def test_who_is_left_alone(jellyfin_server, jellyfin, mode, days, extra):
    _mode(mode)
    _swept(_member(jellyfin_server, days=days, disabled=True, **extra))

    assert restrict_lapsed_disabled_accounts() == []
    jellyfin.restrict_user.assert_not_called()


def test_an_account_disabled_outside_the_sweep_stays_disabled(
    jellyfin_server, jellyfin
):
    """Lapsed and disabled, but not by the expiry sweep: an admin's ban in
    Jellyfin (which the user sync copies into is_disabled) or a lockout. Moving
    it would give a banned account its sign-in back."""
    _mode("restrict")
    _member(jellyfin_server, days=-3, disabled=True)

    assert restrict_lapsed_disabled_accounts() == []
    jellyfin.restrict_user.assert_not_called()


def test_a_failed_move_is_retried_later(jellyfin_server, jellyfin):
    _mode("restrict")
    jellyfin.restrict_user.return_value = None
    user = _swept(_member(jellyfin_server, days=-3, disabled=True))

    assert restrict_lapsed_disabled_accounts() == []

    db.session.refresh(user)
    assert user.is_disabled is True, "still cut off, just the old way"
    assert user.restricted_policy is None


# ── Ownership check for the renewal checkout ────────────────────────────────


def test_a_restricted_member_proves_ownership_without_touching_the_account(
    jellyfin_server, monkeypatch
):
    """The account can sign in already. Toggling it would restore the catalogue
    for a moment and then cut it again, stopping whatever is on screen."""
    client = MagicMock()
    client.authenticate_user.return_value = (True, 200, "tok")
    monkeypatch.setattr(credentials, "get_client_for_media_server", lambda _s: client)
    user = _member(jellyfin_server, days=-1, disabled=True, restricted=True)

    assert credentials.verify_media_credentials("member", "pw") == user.id

    client.enable_user.assert_not_called()
    client.disable_user.assert_not_called()


# ── Renewal endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def api_headers(session):
    import hashlib

    from app.models import AdminAccount, ApiKey

    admin = AdminAccount(username="renewal-screen-admin")
    admin.set_password("irrelevant")
    db.session.add(admin)
    db.session.commit()
    raw = "test-api-key-renewal-screen"
    db.session.add(
        ApiKey(
            name="t",
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_by_id=admin.id,
            is_active=True,
        )
    )
    db.session.commit()
    return {"X-API-Key": raw, "Content-Type": "application/json"}


@pytest.mark.parametrize("disabled", [True, False])
def test_extend_restores_a_restricted_member(
    client, api_headers, jellyfin_server, monkeypatch, disabled
):
    """disabled=False is the drift case: something cleared the flag but the
    member is still on the renewal screen. Paying must still bring them back."""
    calls = []

    def fake_enable(db_id):
        calls.append(db_id)
        user = db.session.get(User, db_id)
        user.is_disabled = False
        user.restricted_policy = None
        db.session.commit()
        return True

    monkeypatch.setattr("app.blueprints.api.api_routes.enable_user", fake_enable)
    user = _member(jellyfin_server, days=-2, disabled=disabled, restricted=True)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    assert calls == [user.id]
    assert response.get_json()["reactivated"] is True


# ── Settings ────────────────────────────────────────────────────────────────


def test_the_settings_offer_the_renewal_screen():
    choices = dict(GeneralSettingsForm.expiry_action.kwargs["choices"])

    assert "restrict" in choices
