"""The renewal screen, Jellyfin side: a lapsed account that can still sign in.

Disabling an account kills the token the TV holds, and the member's app then
shows "Connection failed (badResponse): HTTP 403" with no word about renewing
(Moonfin 2.5.1). Restricting it instead keeps the session alive and shows a
single library, "Membresía vencida", whose artwork carries the renewal QR code.

Measured on tv.neexy.net (Jellyfin 12.1) before building this: with only that
library in ``EnabledFolders``, the same token gets 404 on every other item's
page, PlaybackInfo and download, and Continue Watching, search and the library
list leave them out.
"""

import json

import pytest
import requests

from app.extensions import db
from app.models import MediaServer, User
from app.services.media.jellyfin import (
    RENEWAL_LIBRARY_NAME,
    RESTRICTED_POLICY_FIELDS,
    JellyfinClient,
)

FOLDERS = [
    {"Id": "pelis", "Name": "Peliculas", "CollectionType": "movies"},
    {"Id": "series", "Name": "Series", "CollectionType": "tvshows"},
    {"Id": "cols", "Name": "Colecciones", "CollectionType": "boxsets"},
    {"Id": "lists", "Name": "Playlists", "CollectionType": "playlists"},
    {"Id": "renew", "Name": RENEWAL_LIBRARY_NAME, "CollectionType": "movies"},
]
WITHOUT_RENEWAL = [f for f in FOLDERS if f["Id"] != "renew"]

MEMBER_POLICY = {
    "IsAdministrator": False,
    "IsDisabled": False,
    "EnableAllFolders": False,
    "EnabledFolders": ["pelis", "series", "cols"],
    "EnableLiveTvAccess": True,
    "EnableContentDownloading": True,
    "MaxActiveSessions": 2,
}


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeJellyfin:
    """Just enough of the Jellyfin API: one user's policy, folders, sessions."""

    def __init__(self, policy, folders=FOLDERS, sessions=(), fail_posts=False):
        self.policy = json.loads(json.dumps(policy))
        self.folders = folders
        self.sessions = list(sessions)
        self.fail_posts = fail_posts
        self.posts = []

    def get(self, path, **_kwargs):
        if path == "/Library/MediaFolders":
            return _Resp({"Items": self.folders})
        if path == "/Sessions":
            return _Resp(self.sessions)
        if path.startswith("/Users/"):
            return _Resp({"Id": "jf-1", "Policy": json.loads(json.dumps(self.policy))})
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json=None, **_kwargs):
        if self.fail_posts:
            raise requests.HTTPError("refused")
        self.posts.append((path, json))
        if path.endswith("/Policy"):
            self.policy = json
        return _Resp(None, 204)


def _client(fake: _FakeJellyfin) -> JellyfinClient:
    client = object.__new__(JellyfinClient)
    client.server_id = None
    client.get = fake.get
    client.post = fake.post
    client.set_policy = lambda user_id, policy: fake.post(
        f"/Users/{user_id}/Policy", json=policy
    )
    return client


# ── Restricting ─────────────────────────────────────────────────────────────


def test_restricting_leaves_only_the_renewal_library():
    fake = _FakeJellyfin(MEMBER_POLICY)

    _client(fake).restrict_user("jf-1")

    assert fake.policy["EnableAllFolders"] is False
    assert fake.policy["EnabledFolders"] == ["renew"]
    assert fake.policy["EnableLiveTvAccess"] is False
    assert fake.policy["EnableContentDownloading"] is False
    assert fake.policy["IsDisabled"] is False, "it must still be able to sign in"


def test_restricting_returns_exactly_what_it_overrode():
    """Only these fields go back on renewal, so nothing else may be saved."""
    snapshot = _client(_FakeJellyfin(MEMBER_POLICY)).restrict_user("jf-1")

    assert snapshot == {
        field: MEMBER_POLICY[field] for field in RESTRICTED_POLICY_FIELDS
    }


def test_restricting_leaves_the_rest_of_the_policy_alone():
    fake = _FakeJellyfin(MEMBER_POLICY)

    _client(fake).restrict_user("jf-1")

    assert fake.policy["MaxActiveSessions"] == 2


def test_restricting_a_disabled_account_turns_it_back_on():
    """Members disabled before the switch: their libraries are still in the
    policy, only IsDisabled was flipped, so the snapshot is still the real one."""
    fake = _FakeJellyfin({**MEMBER_POLICY, "IsDisabled": True})

    snapshot = _client(fake).restrict_user("jf-1")

    assert fake.policy["IsDisabled"] is False
    assert fake.policy["EnabledFolders"] == ["renew"]
    assert snapshot["EnabledFolders"] == ["pelis", "series", "cols"]


def test_without_a_renewal_library_the_account_sees_nothing():
    """Still cut off. Better an empty home than the catalogue."""
    fake = _FakeJellyfin(MEMBER_POLICY, folders=WITHOUT_RENEWAL)

    _client(fake).restrict_user("jf-1")

    assert fake.policy["EnableAllFolders"] is False
    assert fake.policy["EnabledFolders"] == []


def test_a_refused_restriction_returns_none():
    assert (
        _client(_FakeJellyfin(MEMBER_POLICY, fail_posts=True)).restrict_user("jf-1")
        is None
    )


# ── Restoring ───────────────────────────────────────────────────────────────


def _restricted(**overrides):
    return {
        **MEMBER_POLICY,
        "EnableAllFolders": False,
        "EnabledFolders": ["renew"],
        "EnableLiveTvAccess": False,
        "EnableContentDownloading": False,
        **overrides,
    }


def _snapshot(**overrides):
    return {
        **{field: MEMBER_POLICY[field] for field in RESTRICTED_POLICY_FIELDS},
        **overrides,
    }


def test_restoring_puts_the_saved_fields_back():
    fake = _FakeJellyfin(_restricted())

    assert _client(fake).unrestrict_user("jf-1", _snapshot()) is True

    for field in RESTRICTED_POLICY_FIELDS:
        assert fake.policy[field] == MEMBER_POLICY[field]
    assert fake.policy["IsDisabled"] is False


def test_restoring_does_not_undo_a_plan_change_made_while_restricted():
    """A renewal into a bigger plan writes MaxActiveSessions while the account is
    still restricted; putting back a whole saved policy would erase it."""
    fake = _FakeJellyfin(_restricted(MaxActiveSessions=4))

    _client(fake).unrestrict_user("jf-1", _snapshot())

    assert fake.policy["MaxActiveSessions"] == 4


def test_restoring_never_hands_back_the_renewal_library():
    fake = _FakeJellyfin(_restricted())

    _client(fake).unrestrict_user("jf-1", _snapshot(EnabledFolders=["pelis", "renew"]))

    assert fake.policy["EnabledFolders"] == ["pelis"]


def test_restoring_without_a_snapshot_grants_every_real_library():
    fake = _FakeJellyfin(_restricted())

    _client(fake).unrestrict_user("jf-1", None)

    assert fake.policy["EnableAllFolders"] is False
    assert fake.policy["EnabledFolders"] == ["pelis", "series", "cols"]


def test_a_refused_restore_returns_false():
    assert (
        _client(_FakeJellyfin(_restricted(), fail_posts=True)).unrestrict_user(
            "jf-1", _snapshot()
        )
        is False
    )


# ── Sessions ────────────────────────────────────────────────────────────────


def test_ending_sessions_stops_playback_and_tells_the_member():
    fake = _FakeJellyfin(
        MEMBER_POLICY,
        sessions=[
            {"Id": "tv", "UserId": "jf-1", "NowPlayingItem": {"Id": "movie"}},
            {"Id": "phone", "UserId": "jf-1"},
            {"Id": "someone-else", "UserId": "jf-2", "NowPlayingItem": {"Id": "x"}},
        ],
    )

    touched = _client(fake).end_sessions_with_notice("jf-1", "Header", "Text")

    paths = [path for path, _ in fake.posts]
    assert "/Sessions/tv/Playing/Stop" in paths
    assert "/Sessions/phone/Playing/Stop" not in paths
    assert "/Sessions/tv/Message" in paths
    assert "/Sessions/phone/Message" in paths
    assert not any("someone-else" in path for path in paths)
    assert touched == 2


def test_ending_sessions_never_raises():
    fake = _FakeJellyfin(
        MEMBER_POLICY,
        sessions=[{"Id": "tv", "UserId": "jf-1", "NowPlayingItem": {"Id": "m"}}],
        fail_posts=True,
    )

    assert _client(fake).end_sessions_with_notice("jf-1", "H", "T") == 0


# ── Keeping the renewal library out of everyone else's reach ────────────────


def test_the_renewal_library_is_not_offered_to_invitations():
    client = _client(_FakeJellyfin(MEMBER_POLICY))

    assert "renew" not in client.libraries()
    assert RENEWAL_LIBRARY_NAME not in client.scan_libraries()


def test_an_all_libraries_invite_does_not_include_the_renewal_screen():
    """EnableAllFolders would show "Membresía vencida" to a paying member."""
    fake = _FakeJellyfin(MEMBER_POLICY)

    _client(fake)._set_specific_folders("jf-1", [])

    assert fake.policy["EnableAllFolders"] is False
    assert fake.policy["EnabledFolders"] == ["pelis", "series", "cols"]


def test_without_a_renewal_library_an_all_libraries_invite_is_unchanged():
    fake = _FakeJellyfin(MEMBER_POLICY, folders=WITHOUT_RENEWAL)

    _client(fake)._set_specific_folders("jf-1", [])

    assert fake.policy["EnableAllFolders"] is True


def test_granting_all_libraries_later_skips_the_renewal_screen_too(app):
    fake = _FakeJellyfin(MEMBER_POLICY)

    with app.app_context():
        assert _client(fake).update_user_libraries("jf-1", None) is True

    assert fake.policy["EnableAllFolders"] is False
    assert fake.policy["EnabledFolders"] == ["pelis", "series", "cols"]


# ── The user sync must not undo the cut ─────────────────────────────────────


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


def test_the_user_sync_keeps_a_restricted_account_marked_as_cut_off(jellyfin_server):
    """Jellyfin reports a restricted account as enabled. Copying that into
    is_disabled would make /extend skip the restore: a paid renewal that leaves
    the member looking at "Membresía vencida"."""
    user = User(
        token="jf-1",
        username="member",
        code="X",
        server_id=jellyfin_server.id,
        is_disabled=True,
        restricted_policy=json.dumps(_snapshot()),
    )
    db.session.add(user)
    db.session.commit()
    client = _client(_FakeJellyfin(_restricted()))
    client.server_id = jellyfin_server.id

    client._sync_user_permissions(user, {"Name": "member", "Policy": _restricted()})

    assert user.is_disabled is True


def test_the_user_sync_still_mirrors_a_plain_account(jellyfin_server):
    user = User(
        token="jf-1",
        username="member",
        code="X",
        server_id=jellyfin_server.id,
        is_disabled=True,
    )
    db.session.add(user)
    db.session.commit()
    client = _client(_FakeJellyfin(MEMBER_POLICY))
    client.server_id = jellyfin_server.id

    client._sync_user_permissions(user, {"Name": "member", "Policy": MEMBER_POLICY})

    assert user.is_disabled is False
