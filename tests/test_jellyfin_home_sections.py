"""Sauron must not touch a Jellyfin account's Home screen layout.

It used to. Between 2026.7.9 and 2026.10.4 every account it created had all ten
Home sections set to "none", and that cost the Roku app every row it had to
draw: jellyfin-roku only falls back to its own defaults when ``homesection0`` is
ABSENT (session.bs, SaveUserHomeSections) and skips every section reading "none"
(HomeRows.bs, processUserSections). A member who linked their television got a
Search box, their own name, and nothing else. The web client hid the damage
because its sidebar reaches the libraries whatever the Home says.

The invariant is now the opposite one, and these tests hold both halves of it:
provisioning writes no DisplayPreferences at all, and the repair only rewrites
an account that is blank on every section the TV reads.

Both provisioning paths are covered — invitation redemption (client.join ->
_do_join) and the password-prompt route (/j/<code>/password).
"""

from unittest.mock import patch

from app.extensions import db
from app.models import AdminAccount, Invitation, MediaServer, Settings, User
from app.services.jellyfin_home_repair import (
    REPAIR_SETTING_KEY,
    repair_blank_home_sections,
)
from app.services.media.jellyfin import (
    DEFAULT_HOME_SECTIONS,
    ROKU_HOME_SECTION_COUNT,
    JellyfinClient,
)

BLANK_SECTIONS = {f"homesection{i}": "none" for i in range(10)}


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _jellyfin_client(server_id, *, display_prefs=None):
    """A JellyfinClient with only the HTTP layer faked out.

    The home-section methods are deliberately left real so the endpoint, query
    params and body are exercised.
    """
    client = object.__new__(JellyfinClient)
    client.server_id = server_id
    client.policy_updates = []
    client.prefs_posts = []
    client.prefs_gets = []

    def get(endpoint, params=None, **kwargs):
        if endpoint.startswith("/DisplayPreferences/"):
            client.prefs_gets.append((endpoint, params))
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


# ── Provisioning leaves the Home screen alone ────────────────────────────────


def test_do_join_never_writes_display_preferences(client, session):
    """The regression itself: redeeming an invitation must not blank the Home."""
    server = _jellyfin_server()
    _invitation("JFHOME", server)

    jf = _jellyfin_client(server.id)
    ok, msg = _join(jf, "JFHOME")

    assert ok is True, msg
    assert jf.prefs_posts == []
    assert jf.prefs_gets == []
    # Without this the test would also pass if _do_join had failed before ever
    # reaching the point where the blanking used to happen.
    assert len(jf.policy_updates) == 1


def test_password_prompt_never_writes_display_preferences(client, session):
    """Same guarantee on the other path that provisions a Jellyfin account."""
    _complete_setup()
    server = _jellyfin_server()
    _invitation("JFPWD", server)

    media_client = HomeSectionCapturingClient()
    response = _redeem_via_password_prompt(client, "JFPWD", media_client)

    assert response.status_code == 302
    assert media_client.prefs_posts == []
    # Proves the route actually reached the Jellyfin branch, so the assertion
    # above is about a guard that ran rather than a branch that never executed.
    assert len(media_client.policy_updates) == 1
    assert User.query.filter_by(username="viewer", server_id=server.id).count() == 1


class HomeSectionCapturingClient:
    """Stands in for a media client on the /j/<code>/password route."""

    def __init__(self):
        self.user_id = "jf-user-1"
        self.prefs_posts = []
        self.policy_updates = []

    def create_user(self, username, password):
        return self.user_id

    def get(self, endpoint):
        return _Response({"Policy": {}})

    def set_policy(self, user_id, policy):
        self.policy_updates.append(policy)

    def post(self, endpoint, params=None, json=None, **kwargs):
        self.prefs_posts.append((endpoint, params, json))
        return _Response({})


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


# ── Recognising a blanked Home screen ────────────────────────────────────────


def test_blank_detection_matches_the_signature_sauron_left(client, session):
    jf = _jellyfin_client(1, display_prefs={"CustomPrefs": dict(BLANK_SECTIONS)})
    assert jf.home_screen_is_blank("jf-user-1") is True


def test_untouched_account_does_not_read_as_blank(client, session):
    """Absent keys are the healthy state — that is what a client defaults from.

    Getting this backwards would make the repair write an explicit layout onto
    every account that was already fine, pinning them to today's defaults.
    """
    jf = _jellyfin_client(1, display_prefs={"CustomPrefs": {"skipBackLength": "10000"}})
    assert jf.home_screen_is_blank("jf-user-1") is False


def test_member_who_turned_their_own_sections_off_is_left_alone(client, session):
    """One real section is enough to prove the member chose this layout."""
    sections = dict(BLANK_SECTIONS)
    sections["homesection3"] = "nextup"

    jf = _jellyfin_client(1, display_prefs={"CustomPrefs": sections})
    assert jf.home_screen_is_blank("jf-user-1") is False


def test_sections_beyond_the_roku_range_cannot_rescue_a_blank_home(client, session):
    """jellyfin-roku loops 0..7, so a section at 8 or 9 is invisible to the TV."""
    sections = dict(BLANK_SECTIONS)
    sections[f"homesection{ROKU_HOME_SECTION_COUNT + 1}"] = "latestmedia"

    jf = _jellyfin_client(1, display_prefs={"CustomPrefs": sections})
    assert jf.home_screen_is_blank("jf-user-1") is True


# ── Repairing one account ────────────────────────────────────────────────────


def test_restore_writes_the_default_layout(client, session):
    jf = _jellyfin_client(1, display_prefs={"CustomPrefs": dict(BLANK_SECTIONS)})
    jf.restore_default_home_sections("jf-user-1")

    endpoint, params, body = jf.prefs_posts[0]
    assert endpoint == "/DisplayPreferences/usersettings"
    # client=emby is what jellyfin-web and jellyfin-roku both send; userId must
    # be explicit because the API key authenticates as admin, not as the member.
    assert params == {"userId": "jf-user-1", "client": "emby"}

    written = [body["CustomPrefs"][f"homesection{i}"] for i in range(10)]
    assert written == list(DEFAULT_HOME_SECTIONS)
    assert written[0] == "smalllibrarytiles", "the TV needs its library tiles"


def test_restore_keeps_unrelated_preferences(client, session):
    """The POST replaces the whole document, so the rest has to be carried over."""
    jf = _jellyfin_client(
        1,
        display_prefs={
            "Client": "emby",
            "CustomPrefs": {**BLANK_SECTIONS, "skipForwardLength": "30000"},
        },
    )
    jf.restore_default_home_sections("jf-user-1")

    _, _, body = jf.prefs_posts[0]
    assert body["Client"] == "emby"
    assert body["CustomPrefs"]["skipForwardLength"] == "30000"


# ── The one-time sweep ───────────────────────────────────────────────────────


class _RepairClient:
    """A media client whose accounts have known Home-section states."""

    def __init__(self, blank_ids, *, failing_ids=()):
        self.blank_ids = set(blank_ids)
        self.failing_ids = set(failing_ids)
        self.restored = []

    def home_screen_is_blank(self, user_id):
        if user_id in self.failing_ids:
            raise RuntimeError("account vanished mid-sweep")
        return user_id in self.blank_ids

    def restore_default_home_sections(self, user_id):
        self.restored.append(user_id)


def _server_with_users(*usernames):
    server = _jellyfin_server()
    db.session.add(server)
    db.session.commit()

    for name in usernames:
        db.session.add(
            User(
                username=name,
                token=f"jf-{name}",
                email=None,
                code="REPAIR",
                server_id=server.id,
            )
        )
    db.session.commit()
    return server


def _run_repair(media_client, **kwargs):
    with patch(
        "app.services.media.service.get_client_for_media_server",
        return_value=media_client,
    ):
        return repair_blank_home_sections(**kwargs)


def test_repair_fixes_only_the_blanked_accounts(client, session):
    _server_with_users("blanked", "healthy")
    media_client = _RepairClient(blank_ids={"jf-blanked"})

    summary = _run_repair(media_client)

    assert media_client.restored == ["jf-blanked"]
    assert summary["repaired"] == 1
    assert summary["inspected"] == 2


def test_repair_runs_once(client, session):
    _server_with_users("blanked")
    first = _RepairClient(blank_ids={"jf-blanked"})
    _run_repair(first)

    # A second boot must not re-flatten a member who has since rearranged their
    # own Home screen back to something sauron would call "blank".
    second = _RepairClient(blank_ids={"jf-blanked"})
    summary = _run_repair(second)

    assert second.restored == []
    assert summary["repaired"] == 0
    assert Settings.query.filter_by(key=REPAIR_SETTING_KEY).count() == 1


def test_force_overrides_the_marker(client, session):
    """What a support session needs after restoring an older database."""
    _server_with_users("blanked")
    _run_repair(_RepairClient(blank_ids={"jf-blanked"}))

    again = _RepairClient(blank_ids={"jf-blanked"})
    _run_repair(again, force=True)

    assert again.restored == ["jf-blanked"]


def test_one_broken_account_does_not_stop_the_queue(client, session):
    _server_with_users("broken", "blanked")
    media_client = _RepairClient(blank_ids={"jf-blanked"}, failing_ids={"jf-broken"})

    summary = _run_repair(media_client)

    assert media_client.restored == ["jf-blanked"]
    assert summary["repaired"] == 1


def test_unreachable_server_never_fails_the_boot(client, session):
    """This runs during startup; an offline media server is an ordinary Tuesday."""
    _server_with_users("blanked")

    with patch(
        "app.services.media.service.get_client_for_media_server",
        side_effect=RuntimeError("connection refused"),
    ):
        summary = repair_blank_home_sections()

    assert summary["repaired"] == 0


def test_marker_collision_leaves_the_session_usable(client, session):
    """Two Gunicorn workers can reach the marker write at once.

    The loser hits the unique constraint on Settings.key. That is success — the
    marker landed — but without a rollback the worker would carry a failed
    transaction into its next request and die there on an unrelated query.
    """
    _server_with_users("blanked")
    db.session.add(Settings(key=REPAIR_SETTING_KEY, value="1"))
    db.session.commit()

    # Force the write despite the marker, which is exactly the losing worker's
    # view of the world: it read "not repaired" before the winner committed.
    summary = _run_repair(_RepairClient(blank_ids={"jf-blanked"}), force=True)

    assert summary["repaired"] == 1
    # The session survived the constraint violation.
    assert Settings.query.filter_by(key=REPAIR_SETTING_KEY).count() == 1
    assert User.query.filter_by(username="blanked").count() == 1


def test_emby_is_left_out(client, session):
    """EmbyClient inherits the methods, but sauron never blanked Emby."""
    server = MediaServer(
        name="Emby",
        server_type="emby",
        url="http://emby.local",
        api_key="emby-key",
    )
    db.session.add(server)
    db.session.commit()
    db.session.add(
        User(
            username="viewer",
            token="emby-1",
            email=None,
            code="REPAIR",
            server_id=server.id,
        )
    )
    db.session.commit()

    media_client = _RepairClient(blank_ids={"emby-1"})
    summary = _run_repair(media_client)

    assert media_client.restored == []
    assert summary["inspected"] == 0
