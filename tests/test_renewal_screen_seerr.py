"""A member on the renewal screen must not request titles in Seerr.

The restricted account can still sign in to Jellyfin, so it can sign in to
Seerr too, and Moonfin reaches Seerr through Moonbase with the member's own
Seerr session. Measured on Seerr 3.5.0 with a throwaway user: with
``permissions`` at 0, POST /api/v1/request answers 403 "You do not have
permission to make movie requests."; with the default (REQUEST + CREATE_ISSUES)
it goes through. So sauron saves the member's Seerr permissions, sets them to 0
while restricted, and puts them back on renewal.

Seerr is reached through the "overseerr" companion connection (URL + API key).
"""

import json

import pytest
import requests

from app.extensions import db
from app.models import Connection, MediaServer, Settings, User
from app.services import seerr_access
from app.services.expiry import RENEWAL_URL

DEFAULT = 4194336  # REQUEST + CREATE_ISSUES, Seerr's default on tv.neexy.net
ADMIN_BIT = 2


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _FakeSeerr:
    def __init__(self, users, down=False):
        self.users = {u["id"]: dict(u) for u in users}
        self.down = down
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, headers))
        if self.down:
            raise requests.ConnectionError("down")
        assert url.endswith("/api/v1/user")
        everyone = list(self.users.values())
        skip, take = params.get("skip", 0), params["take"]
        return _Resp({"results": everyone[skip : skip + take]})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        if self.down:
            raise requests.ConnectionError("down")
        seerr_id = int(url.rstrip("/").split("/")[-3])
        self.users[seerr_id]["permissions"] = json["permissions"]
        return _Resp({"permissions": json["permissions"]})


@pytest.fixture
def server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def connection(server):
    conn = Connection(
        connection_type="overseerr",
        name="Seerr",
        url="http://seerr.local:5055/",
        api_key="seerr-key",
        media_server_id=server.id,
    )
    db.session.add(conn)
    db.session.commit()
    return conn


def _seerr(monkeypatch, users, **kwargs):
    fake = _FakeSeerr(users, **kwargs)
    monkeypatch.setattr(seerr_access.requests, "get", fake.get)
    monkeypatch.setattr(seerr_access.requests, "post", fake.post)
    return fake


def _member(server, *, token="ab12-cd34", restricted=True, saved=None):  # noqa: S107 — a Jellyfin user id
    user = User(
        token=token,
        username="member",
        code="INVITE1",
        server_id=server.id,
        is_disabled=restricted,
        restricted_policy=json.dumps({"EnabledFolders": ["x"]}) if restricted else None,
        seerr_saved_permissions=saved,
    )
    db.session.add(user)
    db.session.commit()
    return user


# ── Suspending ──────────────────────────────────────────────────────────────


def test_suspending_saves_the_permissions_and_clears_them(
    server, connection, monkeypatch
):
    fake = _seerr(
        monkeypatch, [{"id": 26, "jellyfinUserId": "AB12CD34", "permissions": DEFAULT}]
    )
    user = _member(server)

    assert seerr_access.suspend_requests(user) is True

    assert user.seerr_saved_permissions == DEFAULT
    assert fake.users[26]["permissions"] == 0
    post = next(c for c in fake.calls if c[0] == "POST")
    assert post[1] == "http://seerr.local:5055/api/v1/user/26/settings/permissions"
    get = next(c for c in fake.calls if c[0] == "GET")
    assert get[2] == {"X-Api-Key": "seerr-key"}


def test_suspending_twice_keeps_the_original(server, connection, monkeypatch):
    """A second pass must not save the 0 it wrote as the thing to restore."""
    _seerr(monkeypatch, [{"id": 26, "jellyfinUserId": "ab12cd34", "permissions": 0}])
    user = _member(server, saved=DEFAULT)

    seerr_access.suspend_requests(user)

    assert user.seerr_saved_permissions == DEFAULT


def test_without_a_seerr_connection_nothing_happens(server, monkeypatch):
    fake = _seerr(monkeypatch, [])
    user = _member(server)

    assert seerr_access.suspend_requests(user) is False

    assert fake.calls == []
    assert user.seerr_saved_permissions is None


def test_a_member_not_in_seerr_yet_is_left_for_the_next_pass(
    server, connection, monkeypatch
):
    _seerr(
        monkeypatch, [{"id": 9, "jellyfinUserId": "someone-else", "permissions": 32}]
    )
    user = _member(server)

    assert seerr_access.suspend_requests(user) is False

    assert user.seerr_saved_permissions is None


def test_a_seerr_admin_is_never_touched(server, connection, monkeypatch):
    fake = _seerr(
        monkeypatch, [{"id": 1, "jellyfinUserId": "ab12cd34", "permissions": ADMIN_BIT}]
    )
    user = _member(server)

    seerr_access.suspend_requests(user)

    assert fake.users[1]["permissions"] == ADMIN_BIT
    assert user.seerr_saved_permissions is None


def test_seerr_down_never_raises(server, connection, monkeypatch):
    _seerr(monkeypatch, [], down=True)
    user = _member(server)

    assert seerr_access.suspend_requests(user) is False


# ── Restoring ───────────────────────────────────────────────────────────────


def test_restoring_puts_the_permissions_back(server, connection, monkeypatch):
    fake = _seerr(
        monkeypatch, [{"id": 26, "jellyfinUserId": "ab12cd34", "permissions": 0}]
    )
    user = _member(server, restricted=False, saved=DEFAULT)

    assert seerr_access.restore_requests(user) is True

    assert fake.users[26]["permissions"] == DEFAULT
    assert user.seerr_saved_permissions is None


def test_a_failed_restore_keeps_what_to_restore(server, connection, monkeypatch):
    _seerr(monkeypatch, [], down=True)
    user = _member(server, restricted=False, saved=DEFAULT)

    assert seerr_access.restore_requests(user) is False

    assert user.seerr_saved_permissions == DEFAULT


def test_a_member_gone_from_seerr_has_nothing_left_to_restore(
    server, connection, monkeypatch
):
    _seerr(monkeypatch, [])
    user = _member(server, restricted=False, saved=DEFAULT)

    assert seerr_access.restore_requests(user) is True

    assert user.seerr_saved_permissions is None


def test_nothing_saved_means_nothing_to_do(server, connection, monkeypatch):
    fake = _seerr(monkeypatch, [])
    user = _member(server, restricted=False)

    assert seerr_access.restore_requests(user) is True
    assert fake.calls == []


# ── The scheduled pass ──────────────────────────────────────────────────────


def test_the_pass_catches_a_first_seerr_sign_in_and_a_renewal(
    server, connection, monkeypatch
):
    """Seerr imports a member on first sign-in WITH the default permissions, so
    one who opens Seerr after being restricted must be caught on the next pass."""
    fake = _seerr(
        monkeypatch,
        [
            {"id": 26, "jellyfinUserId": "lapsed", "permissions": DEFAULT},
            {"id": 27, "jellyfinUserId": "renewed", "permissions": 0},
        ],
    )
    lapsed = _member(server, token="lapsed")
    renewed = _member(server, token="renewed", restricted=False, saved=DEFAULT)

    summary = seerr_access.sync_seerr_access()

    assert summary == {"suspended": 1, "restored": 1, "errors": 0, "unconfigured": 0}
    assert fake.users[26]["permissions"] == 0
    assert fake.users[27]["permissions"] == DEFAULT
    db.session.refresh(lapsed)
    db.session.refresh(renewed)
    assert lapsed.seerr_saved_permissions == DEFAULT
    assert renewed.seerr_saved_permissions is None
    assert sum(1 for c in fake.calls if c[0] == "GET") == 1, "one listing per pass"


# ── Wired into the renewal screen ───────────────────────────────────────────


@pytest.fixture
def jellyfin(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.restrict_user.return_value = {"EnabledFolders": ["x"]}
    client.unrestrict_user.return_value = True
    monkeypatch.setattr(
        "app.services.media.service.get_client_for_media_server", lambda _s: client
    )
    return client


def test_restricting_suspends_seerr_and_renewing_restores_it(
    server, jellyfin, monkeypatch
):
    import datetime

    from app.services.media.service import enable_user, restrict_for_expiry

    calls = []
    monkeypatch.setattr(seerr_access, "suspend_requests", lambda u: calls.append("off"))
    monkeypatch.setattr(seerr_access, "restore_requests", lambda u: calls.append("on"))
    db.session.add(Settings(key="expiry_action", value="restrict"))
    user = _member(server, restricted=False)
    user.expires = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) - datetime.timedelta(days=1)
    db.session.commit()

    restrict_for_expiry(user.id)
    enable_user(user.id)

    assert calls == ["off", "on"]


def test_the_on_screen_notice_shows_the_full_renewal_link(server, jellyfin):
    import datetime

    from app.services.media.service import restrict_for_expiry

    db.session.add(Settings(key="expiry_action", value="restrict"))
    user = _member(server, restricted=False)
    user.expires = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) - datetime.timedelta(days=1)
    db.session.commit()

    restrict_for_expiry(user.id)

    text = jellyfin.end_sessions_with_notice.call_args.args[2]
    assert RENEWAL_URL in text


# ── Not failing open ────────────────────────────────────────────────────────


def test_a_member_past_the_first_page_of_seerr_users_is_found(
    server, connection, monkeypatch
):
    monkeypatch.setattr(seerr_access, "PAGE_SIZE", 2)
    others = [
        {"id": n, "jellyfinUserId": f"other{n}", "permissions": DEFAULT}
        for n in range(1, 6)
    ]
    fake = _seerr(
        monkeypatch,
        [*others, {"id": 26, "jellyfinUserId": "ab12cd34", "permissions": DEFAULT}],
    )
    user = _member(server)

    assert seerr_access.suspend_requests(user) is True

    assert fake.users[26]["permissions"] == 0


def test_restricted_members_without_a_seerr_connection_are_reported(
    server, monkeypatch
):
    """Configuring Seerr is a manual step; forgetting it must not be silent."""
    _seerr(monkeypatch, [])
    _member(server)

    summary = seerr_access.sync_seerr_access()

    assert summary["unconfigured"] == 1
