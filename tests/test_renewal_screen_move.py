"""POST /api/users/<id>/renewal-screen: the operator moves one lapsed account.

The automatic pass only moves accounts it can PROVE the expiry sweep disabled
for their current expiry (an ExpiredUser row with that date), so a ban made in
Jellyfin never gets its sign-in back. An account disabled any other way — by
hand, before an expiry date was edited, or recorded as a lockout — stays
disabled and keeps answering 403 until someone decides. This is that decision,
and its answer says why the automatic pass left the account alone.
"""

import datetime
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from app.extensions import db
from app.models import AdminAccount, ApiKey, ExpiredUser, MediaServer, Settings, User


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    monkeypatch.setattr(
        "app.services.notifications.notify", lambda *a, **k: None, raising=True
    )


@pytest.fixture
def api_headers(session):
    admin = AdminAccount(username="move-admin")
    admin.set_password("irrelevant")
    db.session.add(admin)
    db.session.commit()
    raw = "test-api-key-move"
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


@pytest.fixture
def server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def jellyfin(monkeypatch):
    client = MagicMock()
    client.restrict_user.return_value = {"EnabledFolders": ["pelis"]}
    monkeypatch.setattr(
        "app.services.media.service.get_client_for_media_server", lambda _s: client
    )
    monkeypatch.setattr(
        "app.services.seerr_access.suspend_requests", lambda _u: False, raising=True
    )
    return client


def _mode(value):
    db.session.add(Settings(key="expiry_action", value=value))
    db.session.commit()


def _member(server, *, days=-20, disabled=True, **extra):
    user = User(
        token="jf-1",
        username="qwertyu",
        code="INVITE1",
        server_id=server.id,
        is_disabled=disabled,
        expires=(
            datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
            + datetime.timedelta(days=days)
        ),
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _move(client, headers, user_id):
    return client.post(f"/api/users/{user_id}/renewal-screen", headers=headers)


def test_moves_an_account_the_automatic_pass_could_not_prove(
    client, api_headers, server, jellyfin
):
    _mode("restrict")
    user = _member(server, disabled_externally=True)

    response = _move(client, api_headers, user.id)

    assert response.status_code == 200
    body = response.get_json()
    assert body["moved"] is True
    assert body["diagnosis"] == {
        "is_disabled": True,
        "disabled_externally": True,
        "swept_for_this_expiry": False,
    }
    jellyfin.restrict_user.assert_called_once_with("jf-1", lift_disable=True)
    db.session.refresh(user)
    assert user.restricted_policy is not None
    assert user.is_disabled is True
    assert user.disabled_externally is False


def test_the_diagnosis_tells_an_old_expiry_record_apart(
    client, api_headers, server, jellyfin
):
    _mode("restrict")
    user = _member(server)
    db.session.add(
        ExpiredUser(
            original_user_id=user.id,
            username=user.username,
            expired_at=user.expires - datetime.timedelta(days=30),
        )
    )
    db.session.commit()

    body = _move(client, api_headers, user.id).get_json()

    assert body["diagnosis"]["swept_for_this_expiry"] is False
    assert body["diagnosis"]["disabled_externally"] is False


def test_an_account_already_there_is_left_alone(client, api_headers, server, jellyfin):
    _mode("restrict")
    user = _member(server, restricted_policy=json.dumps({"EnabledFolders": ["x"]}))

    response = _move(client, api_headers, user.id)

    assert response.status_code == 200
    assert response.get_json()["moved"] is False
    jellyfin.restrict_user.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "days", "server_type"),
    [
        ("disable", -20, "jellyfin"),  # the admin did not choose the screen
        ("restrict", 10, "jellyfin"),  # not lapsed: a suspension, not a renewal
        ("restrict", -20, "emby"),  # Jellyfin only
    ],
)
def test_refuses_what_is_not_a_lapsed_jellyfin_membership(
    client, api_headers, server, jellyfin, mode, days, server_type
):
    _mode(mode)
    server.server_type = server_type
    db.session.commit()
    user = _member(server, days=days)

    response = _move(client, api_headers, user.id)

    assert response.status_code == 409
    assert response.get_json()["error"]
    jellyfin.restrict_user.assert_not_called()


def test_a_refused_move_is_a_502(client, api_headers, server, jellyfin):
    _mode("restrict")
    jellyfin.restrict_user.return_value = None
    user = _member(server)

    response = _move(client, api_headers, user.id)

    assert response.status_code == 502
    db.session.refresh(user)
    assert user.restricted_policy is None


def test_unknown_user_is_404(client, api_headers, server, jellyfin):
    assert _move(client, api_headers, 9999).status_code == 404


def test_needs_the_api_key(client, server, jellyfin):
    _mode("restrict")
    user = _member(server)

    assert _move(client, {}, user.id).status_code == 401
    jellyfin.restrict_user.assert_not_called()
