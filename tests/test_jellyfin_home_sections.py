"""New Jellyfin accounts must land with every Home screen section set to "None".

Jellyfin keeps that layout in DisplayPreferences (id "usersettings", client
"emby"), not in the user Policy, and its update handler clears every stored
section and re-adds only the keys it receives — so the write has to name all of
them or the omitted ones fall back to the client's built-in defaults.

Both paths that provision a Jellyfin account are covered: invitation redemption
(client.join -> _do_join) and the password-prompt route (/j/<code>/password).
The failure-isolation tests matter most — by the time this runs the account
already exists, so a preferences hiccup must never fail the sign-up.
"""

from unittest.mock import patch

from app.extensions import db
from app.models import AdminAccount, Invitation, MediaServer, Settings, User
from app.services.media.jellyfin import (
    HOME_SECTION_COUNT,
    JellyfinClient,
)

EXPECTED_SECTIONS = {f"homesection{i}": "none" for i in range(HOME_SECTION_COUNT)}


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _jellyfin_client(server_id, *, display_prefs=None, prefs_error=None):
    """A JellyfinClient with only the HTTP layer faked out.

    ``reset_home_sections`` is deliberately left real so the endpoint, query
    params and body are exercised. ``prefs_error`` makes the DisplayPreferences
    GET blow up, standing in for an old server or a proxy hiccup.
    """
    client = object.__new__(JellyfinClient)
    client.server_id = server_id
    client.policy_updates = []
    client.prefs_posts = []

    def get(endpoint, params=None, **kwargs):
        if endpoint.startswith("/DisplayPreferences/"):
            if prefs_error is not None:
                raise prefs_error
            return _Response(display_prefs if display_prefs is not None else {})
        # _do_join reads the freshly created user's current policy
        return _Response({"Policy": {}})

    def post(endpoint, params=None, json=None, **kwargs):
        client.prefs_posts.append((endpoint, params, json))
        return _Response({})

    def set_policy(user_id, policy):
        client.policy_updates.append((user_id, policy))

    # Skip the heavy library + identity-linking machinery for these unit tests.
    client.create_user = lambda username, password: "jf-user-1"
    client.get = get
    client.post = post
    client.set_policy = set_policy
    client._set_specific_folders = lambda user_id, sections: None
    client._create_user_with_identity_linking = lambda payload: None
    return client


def _invitation(code, server):
    invitation = Invitation(code=code, used=False, unlimited=True)
    invitation.servers = [server]
    db.session.add_all([server, invitation])
    db.session.commit()
    return invitation


def _jellyfin_server(name="JF"):
    return MediaServer(
        name=name,
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="jf-key",
    )


def _join(jf, code):
    return jf._do_join(
        username="viewer",
        password="password123",
        confirm="password123",
        email="viewer@example.com",
        code=code,
    )


def test_do_join_blanks_every_home_section(client, session):
    server = _jellyfin_server()
    _invitation("JFHOME", server)

    jf = _jellyfin_client(server.id)
    ok, msg = _join(jf, "JFHOME")

    assert ok is True, msg
    assert len(jf.prefs_posts) == 1
    endpoint, params, body = jf.prefs_posts[0]
    assert endpoint == "/DisplayPreferences/usersettings"
    # client=emby is what jellyfin-web itself sends; userId must be explicit
    # because the API key authenticates as admin, not as the new user.
    assert params == {"userId": "jf-user-1", "client": "emby"}
    assert body["CustomPrefs"] == EXPECTED_SECTIONS


def test_do_join_preserves_unrelated_display_prefs(client, session):
    """Only the homesection* keys are ours to overwrite."""
    server = _jellyfin_server()
    _invitation("JFKEEP", server)

    jf = _jellyfin_client(
        server.id,
        display_prefs={
            "Client": "emby",
            "CustomPrefs": {"skipForwardLength": "30000", "homesection0": "resume"},
        },
    )
    ok, msg = _join(jf, "JFKEEP")

    assert ok is True, msg
    _, _, body = jf.prefs_posts[0]
    assert body["Client"] == "emby"
    assert body["CustomPrefs"]["skipForwardLength"] == "30000"
    assert body["CustomPrefs"]["homesection0"] == "none"


def test_do_join_succeeds_when_display_prefs_call_fails(client, session):
    """A preferences failure must not orphan an already-created account."""
    server = _jellyfin_server()
    _invitation("JFFAIL", server)

    jf = _jellyfin_client(server.id, prefs_error=RuntimeError("boom"))
    ok, msg = _join(jf, "JFFAIL")

    assert ok is True, msg
    assert jf.prefs_posts == []
    # The policy write still landed — the failure was isolated to the prefs call.
    assert len(jf.policy_updates) == 1


class HomeSectionCapturingClient:
    """Stands in for a media client on the /j/<code>/password route."""

    def __init__(self):
        self.user_id = "jf-user-1"
        self.home_section_resets = []
        self.policy_updates = []

    def create_user(self, username, password):
        return self.user_id

    def get(self, endpoint):
        return _Response({"Policy": {}})

    def set_policy(self, user_id, policy):
        # Recorded so a test can tell "the guard skipped us" apart from
        # "the route never got this far".
        self.policy_updates.append(policy)

    def reset_home_sections(self, user_id):
        self.home_section_resets.append(user_id)


def _complete_setup():
    admin = AdminAccount(username="admin")
    admin.set_password("password")
    db.session.add(admin)
    db.session.add(Settings(key="admin_username", value="admin"))


def _redeem_via_password_prompt(client, code, media_client):
    with patch(
        "app.services.media.service.get_client_for_media_server",
        return_value=media_client,
    ):
        return client.post(
            f"/j/{code}/password",
            data={
                "username": "viewer",
                "email": "viewer@example.com",
                "password": "password123",
                "confirm": "password123",
            },
        )


def test_password_prompt_blanks_home_sections_for_jellyfin(client, session):
    _complete_setup()
    server = _jellyfin_server()
    _invitation("JFPWD", server)

    media_client = HomeSectionCapturingClient()
    response = _redeem_via_password_prompt(client, "JFPWD", media_client)

    assert response.status_code == 302
    assert media_client.home_section_resets == ["jf-user-1"]


def test_password_prompt_skips_home_sections_for_emby(client, session):
    """EmbyClient inherits the method but Emby stores display prefs differently."""
    _complete_setup()
    server = MediaServer(
        name="Emby",
        server_type="emby",
        url="http://emby.local",
        api_key="emby-key",
    )
    _invitation("EMBYPWD", server)

    media_client = HomeSectionCapturingClient()
    response = _redeem_via_password_prompt(client, "EMBYPWD", media_client)

    assert response.status_code == 302
    # Without this first assertion the test would also pass if the Emby branch
    # never ran at all, which would prove nothing about the guard.
    assert len(media_client.policy_updates) == 1
    assert media_client.home_section_resets == []


def test_password_prompt_survives_display_prefs_failure(client, session):
    """Same isolation guarantee on the password-prompt path."""
    _complete_setup()
    server = _jellyfin_server()
    _invitation("JFPWDFAIL", server)

    media_client = HomeSectionCapturingClient()

    def boom(user_id):
        raise RuntimeError("boom")

    media_client.reset_home_sections = boom

    response = _redeem_via_password_prompt(client, "JFPWDFAIL", media_client)

    assert response.status_code == 302
    # The redirect alone proves little — the route's per-server handler logs and
    # moves on. The account row is what the isolation actually protects: without
    # it, the exception would trip the rollback and the user would be orphaned on
    # Jellyfin with nothing recorded locally.
    assert User.query.filter_by(username="viewer", server_id=server.id).count() == 1
