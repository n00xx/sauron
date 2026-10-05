"""Jellyfin Quick Connect: authorise a TV's code on behalf of the buyer.

The load-bearing test in this file is
``test_client_supplied_user_id_is_ignored``. Everything else is ordinary
coverage; that one is the difference between a convenience feature and handing
out administrator tokens to anyone who can point a television at the server.

Verified against Jellyfin 10.11.11, where an admin API key may authorise a code
on behalf of another user (``POST /QuickConnect/Authorize?code=&userId=``).
``test_forbidden_status_is_reported_distinctly`` is the canary for a future
Jellyfin tightening that privilege: it fails loudly rather than silently
degrading, because the contingency (minting a short-lived user token at join
time) is a different design.
"""

import datetime
from pathlib import Path

import pytest

from app.extensions import db
from app.models import MediaServer, User
from app.services.expiry import RENEWAL_URL
from app.services.media.jellyfin import JellyfinClient
from app.services.wizard_identity import (
    WIZARD_USER_IDS_KEY,
    current_wizard_user,
    remember_wizard_user,
)


class _Response:
    def __init__(self, status_code=200, text="true"):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        external_url="https://tv.example.net",
        api_key="admin-api-key",
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def provisioned_user(jellyfin_server):
    user = User(
        token="jf-user-abc",  # the Jellyfin user id
        username="buyer",
        code="INVITE1",
        server_id=jellyfin_server.id,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _client_stub(monkeypatch, recorder, response=None, raises=None):
    """Intercept the outbound Jellyfin call and record what we sent."""

    def fake_post(url, params=None, headers=None, timeout=None):
        recorder.append({"url": url, "params": params, "headers": headers})
        if raises is not None:
            raise raises
        return response or _Response()

    monkeypatch.setattr(
        "app.services.media.jellyfin.requests.post", fake_post, raising=True
    )


# ── The security invariant ──────────────────────────────────────────────────


def test_client_supplied_user_id_is_ignored(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """A userId in the request body must never reach Jellyfin.

    If it did, an attacker would submit their own TV's code together with the
    administrator's user id and be handed an admin token.
    """
    sent = []
    _client_stub(monkeypatch, sent)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post(
        "/wizard/quick-connect",
        data={
            "code": "123456",
            "userId": "administrator-guid",
            "user_id": "administrator-guid",
            "jellyfin_user_id": "administrator-guid",
        },
    )

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0]["params"]["userId"] == "jf-user-abc"
    assert "administrator-guid" not in str(sent[0])


def test_missing_session_identity_fails_closed(client, monkeypatch):
    """No recorded account means no authorisation, even with wizard access.

    `restrict_wizard` lets a request through on a client-controlled Referer, so
    reaching this route proves nothing on its own.
    """
    sent = []
    _client_stub(monkeypatch, sent)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"

    response = client.post("/wizard/quick-connect", data={"code": "123456"})

    assert response.status_code == 403
    assert sent == [], "must not call Jellyfin without a server-set identity"


def test_stale_session_user_fails_closed(client, jellyfin_server, monkeypatch):
    """An account deleted after provisioning must not fall back to guessing."""
    sent = []
    _client_stub(monkeypatch, sent)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): 999999}

    response = client.post("/wizard/quick-connect", data={"code": "123456"})

    assert response.status_code == 403
    assert sent == []


# ── Endpoint behaviour ──────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["", "abc123", "12", "12345678901", "12 34"])
def test_malformed_codes_are_rejected_without_calling_jellyfin(
    client, provisioned_user, jellyfin_server, monkeypatch, code
):
    sent = []
    _client_stub(monkeypatch, sent)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": code})

    assert response.status_code == 200
    assert sent == []
    assert "Revisa el c\u00f3digo".encode() in response.data


def test_successful_authorisation_tells_the_buyer_to_look_at_the_device(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    _client_stub(monkeypatch, [], response=_Response(200, "true"))

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert "\u00a1Conectado!".encode() in response.data


def test_unknown_code_is_reported_as_expired(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """Jellyfin answers 404 for a mistyped or timed-out code."""
    _client_stub(monkeypatch, [], response=_Response(404, "Error processing request."))

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "000000"})

    assert response.status_code == 200
    assert "no funcion\u00f3".encode() in response.data


def test_transport_failure_does_not_leak_a_traceback(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    _client_stub(monkeypatch, [], raises=OSError("connection refused"))

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert b"No pudimos conectar tu dispositivo" in response.data
    assert b"connection refused" not in response.data


# ── Client mapping ──────────────────────────────────────────────────────────


def _bare_client():
    client = object.__new__(JellyfinClient)
    client.url = "http://jelly.local"
    client.token = "admin-api-key"
    return client


def test_authorize_sends_code_and_user_id_as_query_params(monkeypatch):
    sent = []
    _client_stub(monkeypatch, sent)

    ok, reason = _bare_client().authorize_quick_connect("734108", "jf-user-abc")

    assert (ok, reason) == (True, None)
    assert sent[0]["url"].endswith("/QuickConnect/Authorize")
    assert sent[0]["params"] == {"code": "734108", "userId": "jf-user-abc"}
    # The admin key authenticates the call; the client identification is what
    # makes the session recognisable in Jellyfin's dashboard.
    assert 'Token="admin-api-key"' in sent[0]["headers"]["Authorization"]
    assert "sauron-quick-connect" in sent[0]["headers"]["Authorization"]


def test_authorize_treats_a_false_body_as_expired(monkeypatch):
    """HTTP 200 with `false` means Jellyfin knew the code but refused it."""
    _client_stub(monkeypatch, [], response=_Response(200, "false"))

    assert _bare_client().authorize_quick_connect("734108", "u") == (False, "expired")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, "expired"), (401, "forbidden"), (403, "forbidden"), (500, "error")],
)
def test_forbidden_status_is_reported_distinctly(monkeypatch, status, expected):
    """401/403 must not be muddled with an expired code.

    They are the signal that a Jellyfin upgrade withdrew the admin key's right
    to authorise on behalf of a user — a different problem with a different fix.
    """
    _client_stub(monkeypatch, [], response=_Response(status, ""))

    ok, reason = _bare_client().authorize_quick_connect("734108", "u")

    assert ok is False
    assert reason == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [(200, "true", True), (200, "false", False), (404, "", False), (500, "", False)],
)
def test_quick_connect_enabled_parses_the_bare_boolean(
    monkeypatch, status, body, expected
):
    monkeypatch.setattr(
        "app.services.media.jellyfin.requests.get",
        lambda *a, **k: _Response(status, body),
        raising=True,
    )

    assert _bare_client().quick_connect_enabled() is expected


def test_quick_connect_enabled_is_false_when_the_server_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr("app.services.media.jellyfin.requests.get", boom, raising=True)

    assert _bare_client().quick_connect_enabled() is False


# ── Session identity ────────────────────────────────────────────────────────


def test_remember_and_recall_the_provisioned_account(
    app, provisioned_user, jellyfin_server
):
    with app.test_request_context():
        remember_wizard_user(jellyfin_server.id, provisioned_user.id)
        recalled = current_wizard_user("jellyfin")

    assert recalled is not None
    assert recalled.token == "jf-user-abc"


def test_recall_ignores_accounts_on_other_server_types(
    app, session, provisioned_user, jellyfin_server
):
    plex = MediaServer(
        name="Plex", server_type="plex", url="http://plex.local", api_key="k"
    )
    db.session.add(plex)
    db.session.commit()
    plex_user = User(
        token="plex-1", username="buyer", code="INVITE1", server_id=plex.id
    )
    db.session.add(plex_user)
    db.session.commit()

    with app.test_request_context():
        remember_wizard_user(plex.id, plex_user.id)
        assert current_wizard_user("jellyfin") is None

        remember_wizard_user(jellyfin_server.id, provisioned_user.id)
        recalled = current_wizard_user("jellyfin")
        assert recalled is not None
        assert recalled.token == "jf-user-abc"


def test_remember_is_a_no_op_on_incomplete_input(app):
    with app.test_request_context():
        remember_wizard_user(None, 1)
        remember_wizard_user(1, None)
        assert current_wizard_user("jellyfin") is None


# ── Widget rendering ────────────────────────────────────────────────────────
#
# QuickConnectWidget.render swallows exceptions and degrades to a placeholder,
# which is right at runtime but means a broken template would ship silently.
# These assert the real markup.


def _render_widget(monkeypatch, jellyfin_server, *, enabled):
    from app.services.wizard_widgets import process_widget_placeholders

    monkeypatch.setattr(
        "app.services.media.jellyfin.JellyfinClient.quick_connect_enabled",
        lambda self: enabled,
        raising=True,
    )
    return process_widget_placeholders(
        "{{ widget:quick_connect }}",
        "jellyfin",
        context={
            "external_url": "https://tv.example.net",
            "server_url": "http://jelly.local",
            "server_name": "Neexy",
            "server_id": jellyfin_server.id,
        },
    )


def test_widget_renders_the_code_box_when_quick_connect_is_on(
    app, jellyfin_server, monkeypatch
):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    assert "temporarily unavailable" not in html
    assert 'name="code"' in html
    assert "/wizard/quick-connect" in html
    # The external URL is what the buyer types into the TV, not the internal one
    assert "https://tv.example.net" in html
    assert "http://jelly.local" not in html


def test_widget_falls_back_to_credentials_when_quick_connect_is_off(
    app, jellyfin_server, monkeypatch
):
    """A code box that cannot work is worse than no code box."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=False)

    assert "temporarily unavailable" not in html
    assert 'name="code"' not in html
    assert "unavailable right now" in html
    assert "https://tv.example.net" in html


def _paths(html):
    """Split the widget into its Smart TV and phone/computer sections.

    The template renders the TV path first, then the phone path, then the
    browser path, each opening on a ``data-path`` attribute. The phone slice
    stops where the browser one starts, so nothing the browser path says can
    satisfy or break an assertion about the phone path.
    """
    tv_start = html.index('data-path="tv"')
    other_start = html.index('data-path="other"')
    browser_start = html.index('data-path="browser"')
    assert tv_start < other_start < browser_start
    return html[tv_start:other_start], html[other_start:browser_start]


def _browser_path(html):
    return html[html.index('data-path="browser"') :]


def test_widget_offers_three_device_paths(app, jellyfin_server, monkeypatch):
    """Smart TV, phone/tablet/computer, and the browser with no app at all.
    "Apple TV, Xbox" was dropped."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    assert "device = 'tv'" in html
    assert "device = 'other'" in html
    assert "device = 'browser'" in html
    assert "Instant access from your browser" in html
    assert "'console'" not in html
    assert "Apple TV" not in html
    assert "'samsung'" not in html


def test_only_the_tv_path_uses_quick_connect(app, jellyfin_server, monkeypatch):
    """On a phone the buyer reads this page on the device they are setting up,
    so there is no second screen to type a code into."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    tv, other = _paths(html)
    assert 'name="code"' in tv
    assert "Quick Connect" in tv
    assert 'name="code"' not in other
    assert "Quick Connect" not in other
    assert "/wizard/quick-connect" not in other


def test_tv_path_ends_with_the_install_video(app, jellyfin_server, monkeypatch):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    tv, other = _paths(html)
    assert "Install Moonfin from your Smart TV app store." in tv
    assert "Installing the Moonfin app on a Smart TV" in tv
    assert "/static/video/neexy-moonfin-smart-tv.mp4" in tv
    assert "/static/video/neexy-moonfin-smart-tv-poster.jpg" in tv
    assert tv.index('id="qc-result"') < tv.index("<video"), "the video goes last"
    assert "<video" not in other


def test_tv_path_keeps_the_install_steps_when_quick_connect_is_off(
    app, jellyfin_server, monkeypatch
):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=False)

    tv, _ = _paths(html)
    assert "Install Moonfin from your Smart TV app store." in tv
    assert "https://tv.example.net" in tv
    assert "unavailable right now" in tv
    assert "<video" in tv


def test_phone_path_unlocks_the_download_only_after_the_checkbox(
    app, jellyfin_server, monkeypatch
):
    """The buyer leaves the page when they press the button, so it stays dead
    until they tick the box saying they read the steps."""
    from app.services.wizard_widgets import MOONFIN_DOWNLOAD_URL

    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    _, other = _paths(html)
    assert MOONFIN_DOWNLOAD_URL == "https://neexy.net/descargar"
    assert 'type="checkbox"' in other
    assert 'x-model="ready"' in other
    # No static href: the link only exists once `ready` is true.
    assert f":href=\"ready ? '{MOONFIN_DOWNLOAD_URL}' : null\"" in other
    assert f'href="{MOONFIN_DOWNLOAD_URL}"' not in other.replace(":href=", "")


def test_phone_path_lets_the_buyer_copy_the_address(app, jellyfin_server, monkeypatch):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    _, other = _paths(html)
    assert 'data-copy="https://tv.example.net"' in other
    assert "Press Copy now" in other
    assert "Choose Password" in other


def test_phone_path_walks_moonfin_screen_by_screen(app, jellyfin_server, monkeypatch):
    """Six steps, one per screen Moonfin shows on a phone, in the order the GIF
    plays them. The address sits on its own step, where Moonfin asks for it."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    _, other = _paths(html)
    steps = [
        ">Download and install Moonfin from your device's app store.</p>",
        ">Open the Moonfin app.</p>",
        ">Choose “Add server”.</p>",
        ">Server address</p>",
        ">Choose Password</p>",
        ">Enter your username and password and choose Sign in.</p>",
    ]
    positions = [other.index(step) for step in steps]
    assert positions == sorted(positions)

    address = other.index('data-copy="https://tv.example.net"')
    assert positions[3] < address < positions[4], "the address belongs to step 4"

    for removed in (
        "Download Moonfin",
        "Open Moonfin",
        "Enter the username and password you created.",
        ">Install</p>",
        ">Open</p>",
        ">Add server</p>",
    ):
        assert removed not in other, removed


def test_phone_path_tips_a_screenshot_right_after_the_steps(
    app, jellyfin_server, monkeypatch
):
    """Right under step 6, so on a phone the steps and the address are still
    on screen when the buyer reads it and takes the screenshot."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    tv, other = _paths(html)
    tip = "Take a screenshot of this information before you continue."
    assert tip in other
    assert tip not in tv
    assert 'role="note"' in other
    step_6 = other.index("Enter your username and password and choose Sign in.")
    assert step_6 < other.index(tip) < other.index("What it looks like in Moonfin")


def test_phone_path_shows_the_setup_gif_above_the_checkbox(
    app, jellyfin_server, monkeypatch
):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    tv, other = _paths(html)
    gif = "/static/img/neexy-moonfin-phone-setup.gif"
    assert gif in other
    assert gif not in tv
    assert other.index(gif) < other.index('type="checkbox"')
    # Versioned: sw.js would otherwise keep serving a replaced GIF once more.
    assert f"{gif}?v=" in other
    # Titled like the TV path's video, so it does not float in on its own.
    assert other.index("What it looks like in Moonfin") < other.index(gif)
    # Hidden until the buyer picks this path, so a TV buyer never downloads it.
    assert 'loading="lazy"' in other
    # promo/phone-setup/build.py output: 700x650 frames under a 104px step strip.
    assert 'width="700"' in other and 'height="754"' in other

    # Rebuilding the GIF at another size must update the template too, or the
    # reserved box no longer matches and the checkbox jumps as it loads.
    # Bytes 6-9 of a GIF are its logical screen width and height.
    gif_file = Path(app.static_folder, "img/neexy-moonfin-phone-setup.gif")
    header = gif_file.read_bytes()[:10]
    assert header[:6] == b"GIF89a"
    assert int.from_bytes(header[6:8], "little") == 700
    assert int.from_bytes(header[8:10], "little") == 754


def test_browser_path_needs_no_app(app, jellyfin_server, monkeypatch):
    """No install, said up front, along with where it works and who it is for.
    Moonfin is still recommended, for playback quality."""
    from app.services.wizard_widgets import MOONFIN_DOWNLOAD_URL

    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    browser = _browser_path(html)
    in_order = [
        "No need to install any app.",
        "This mode is only available on:",
        "Phones",
        "Tablets",
        "Laptops and computers",
        "Ideal if you just want to get in, see what is available and start "
        "enjoying without installing anything.",
        "Even so, we recommend Moonfin for the best experience of the service: "
        "it plays more titles in their original quality, with better picture "
        "and sound and fewer pauses than the browser.",
    ]
    positions = [browser.index(text) for text in in_order]
    assert positions == sorted(positions)

    # Nothing to install, nothing to type a code into.
    for absent in (
        'name="code"',
        "Quick Connect",
        "<video",
        'type="checkbox"',
        MOONFIN_DOWNLOAD_URL,
        "neexy-moonfin-phone-setup.gif",
    ):
        assert absent not in browser, absent


def test_browser_path_only_asks_for_the_address_and_credentials(
    app, jellyfin_server, monkeypatch
):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    browser = _browser_path(html)
    open_step = browser.index("Open this address in your browser:")
    address = browser.index('data-copy="https://tv.example.net"')
    sign_in = browser.index(
        "That is all: enter your username and password and press Sign In."
    )
    assert open_step < address < sign_in


def test_browser_path_shows_the_sign_in_screen(app, jellyfin_server, monkeypatch):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    tv, other = _paths(html)
    browser = _browser_path(html)
    image = "/static/img/neexy-browser-sign-in.png"
    assert image not in tv and image not in other
    assert f"{image}?v=" in browser
    assert 'loading="lazy"' in browser
    sign_in = browser.index("enter your username and password and press Sign In.")
    title = browser.index("What it looks like in your browser")
    assert sign_in < title < browser.index(image)
    assert 'width="768"' in browser and 'height="352"' in browser

    # Same contract as the GIF: the reserved box must match the file.
    # A PNG stores its width and height big-endian at bytes 16-23 (IHDR).
    header = Path(app.static_folder, "img/neexy-browser-sign-in.png").read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(header[16:20], "big") == 768
    assert int.from_bytes(header[20:24], "big") == 352


def test_browser_path_ends_with_popcorn_and_a_button_to_the_server(
    app, jellyfin_server, monkeypatch
):
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    browser = _browser_path(html)
    image = browser.index("neexy-browser-sign-in.png")
    popcorn = browser.index("Get your popcorn ready and that is it!")
    button = browser.index('href="https://tv.example.net"')
    assert image < popcorn < button
    assert "Go to Neexy" in browser[button:]
    assert 'target="_blank"' in browser[button:]
    assert 'rel="noopener noreferrer"' in browser[button:]


@pytest.mark.parametrize(
    ("external_url", "href"),
    [
        (None, "https://tv.neexy.net"),
        ("tv.neexy.net", "https://tv.neexy.net"),
        ("https://tv.example.net", "https://tv.example.net"),
        ("http://tv.example.net", "http://tv.example.net"),
    ],
)
def test_browser_button_always_links_to_a_full_url(
    app, monkeypatch, external_url, href
):
    """The address is shown as the admin wrote it, but a bare hostname as an
    href would resolve relative to this page."""
    from app.services.wizard_widgets import QuickConnectWidget

    monkeypatch.setattr(
        QuickConnectWidget, "_quick_connect_available", lambda *a, **k: True
    )
    context = {"server_name": "Neexy"}
    if external_url:
        context["external_url"] = external_url

    with app.test_request_context():
        html = QuickConnectWidget().render("jellyfin", _context=context)

    browser = _browser_path(html)
    assert f'href="{href}"' in browser
    assert 'href="tv.' not in browser


def test_widget_opts_out_of_prose_typography(app, jellyfin_server, monkeypatch):
    """`not-prose` is not decoration — dropping it puts two bugs back on screen.

    Wizard content renders inside `prose prose-slate` (wizard/_content.html), and
    @tailwindcss/typography then applies:

      :where(code)::before/::after { content: "`" }  → literal backticks around
        the server address, which read as characters you are meant to type
      :where(ol) { list-style-type: decimal }        → a second column of numbers
        beside the numbered badges the list already draws

    Neither is visible in this template's own markup, so a future edit that drops
    the class would look harmless in review and only show up in a screenshot.
    """
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    assert "not-prose" in html


def test_widget_shows_the_public_address_not_the_lan_one(
    app, jellyfin_server, monkeypatch
):
    """A buyer on mobile data cannot reach 192.168.x.x.

    When an admin has not filled in External URL the widget used to print
    MediaServer.url — the address the container dials. It now falls back to the
    public hostname the onboarding video teaches instead.
    """
    from app.services.wizard_widgets import PUBLIC_SERVER_ADDRESS, QuickConnectWidget

    monkeypatch.setattr(
        QuickConnectWidget, "_quick_connect_available", lambda *a, **k: True
    )

    with app.test_request_context():
        html = QuickConnectWidget().render(
            "jellyfin",
            _context={"server_url": "http://192.168.8.207:30013", "server_id": 1},
        )

    assert PUBLIC_SERVER_ADDRESS in html
    assert "192.168.8.207" not in html


# ── Backfill onto existing installs ─────────────────────────────────────────
#
# import_default_wizard_steps only seeds server types that are missing
# entirely, so without this backfill the feature would ship as code that no
# existing Jellyfin install can reach.


def _jellyfin_step(position, markdown="Nothing special here", category="post_invite"):
    from app.models import WizardStep

    return WizardStep(
        server_type="jellyfin",
        category=category,
        position=position,
        title=f"Step {position}",
        markdown=markdown,
        requires=[],
    )


def _jellyfin_steps():
    from app.models import WizardStep

    return (
        db.session.query(WizardStep)
        .filter(WizardStep.server_type == "jellyfin")
        .order_by(WizardStep.position)
        .all()
    )


def test_backfill_appends_the_step_to_an_existing_install(app, session):
    from app.services.wizard_seed import (
        QUICK_CONNECT_MARKER,
        ensure_quick_connect_step,
    )

    db.session.add_all([_jellyfin_step(0), _jellyfin_step(1)])
    db.session.commit()

    with app.app_context():
        ensure_quick_connect_step()

    steps = _jellyfin_steps()
    assert len(steps) == 3
    assert QUICK_CONNECT_MARKER in steps[-1].markdown
    assert steps[-1].position == 2, "must append, never reorder existing steps"


def test_backfill_is_idempotent(app, session):
    from app.services.wizard_seed import ensure_quick_connect_step

    db.session.add_all([_jellyfin_step(0)])
    db.session.commit()

    with app.app_context():
        ensure_quick_connect_step()
        ensure_quick_connect_step()
        ensure_quick_connect_step()

    assert len(_jellyfin_steps()) == 2


def test_backfill_skips_a_fresh_install(app, session):
    """No Jellyfin steps at all means import_default_wizard_steps handles it."""
    from app.services.wizard_seed import ensure_quick_connect_step

    with app.app_context():
        ensure_quick_connect_step()

    assert _jellyfin_steps() == []


def test_backfill_respects_a_step_an_admin_already_moved(app, session):
    """The marker is matched wherever it lives, not by title or position."""
    from app.services.wizard_seed import (
        QUICK_CONNECT_MARKER,
        ensure_quick_connect_step,
    )

    db.session.add_all(
        [
            _jellyfin_step(0),
            _jellyfin_step(
                0,
                markdown=f"Renamed by the admin {{{{ {QUICK_CONNECT_MARKER} }}}}",
                category="pre_invite",
            ),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_quick_connect_step()

    assert len(_jellyfin_steps()) == 2, "must not add a second copy"


def test_backfill_numbers_from_post_invite_only(app, session):
    """Position is unique per (server_type, category), so a high pre_invite
    position must not push the new post_invite step out of sequence."""
    from app.services.wizard_seed import ensure_quick_connect_step

    db.session.add_all(
        [
            _jellyfin_step(0),
            _jellyfin_step(7, category="pre_invite"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_quick_connect_step()

    added = [s for s in _jellyfin_steps() if "quick_connect" in (s.markdown or "")]
    assert len(added) == 1
    assert added[0].position == 1


# ── End to end through the wizard page ──────────────────────────────────────
#
# Every test above calls the widget or the endpoint directly. This one walks
# the wizard the way a buyer does, which is the only thing that proves the
# step actually reaches a browser.


def test_the_step_reaches_the_buyer_through_the_post_wizard_page(
    client, session, jellyfin_server, monkeypatch
):
    from app.models import Invitation
    from app.services.wizard_seed import ensure_quick_connect_step

    monkeypatch.setattr(
        "app.services.media.jellyfin.JellyfinClient.quick_connect_enabled",
        lambda self: True,
        raising=True,
    )

    invitation = Invitation(code="TEST123", unlimited=True)
    invitation.servers = [jellyfin_server]
    db.session.add_all([invitation, _jellyfin_step(0, markdown="# Welcome")])
    db.session.commit()

    ensure_quick_connect_step()

    with client.session_transaction() as sess:
        sess["wizard_access"] = "TEST123"

    # The device step is appended after the existing ones.
    response = client.get("/wizard/post-wizard/1")

    assert response.status_code == 200
    body = response.data.decode()
    assert 'name="code"' in body, "the Quick Connect code box never rendered"
    assert "/wizard/quick-connect" in body
    assert "https://tv.example.net" in body


def test_the_availability_probe_uses_a_short_timeout(monkeypatch):
    """The probe runs while rendering the page a buyer sees right after paying.

    A slow Jellyfin must not hold that page blank for the authorise budget;
    timing out simply degrades the step to username and password.
    """
    from app.services.media import jellyfin as jf

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["timeout"] = timeout
        return _Response(200, "true")

    monkeypatch.setattr(jf.requests, "get", fake_get, raising=True)
    _bare_client().quick_connect_enabled()

    assert seen["timeout"] == jf.QC_PROBE_TIMEOUT_SECONDS
    assert seen["timeout"] < jf.QC_TIMEOUT_SECONDS


# ── Membership lifecycle ────────────────────────────────────────────────────
#
# Facts established against Jellyfin 10.11.11, because the answers are not
# obvious and two of them are load-bearing:
#
#   * Disabling an account kills the token a TV already holds — the very next
#     request comes back 401. Expiry therefore cuts off Quick Connect devices
#     immediately; there is no leak.
#   * Re-enabling does NOT revive that token. After a renewal the device signs
#     in again. This is not specific to Quick Connect: a password login stores
#     a token too, and it dies the same way.
#   * Jellyfin does NOT refuse Quick Connect for a disabled account. Both
#     /QuickConnect/Authorize and /Users/AuthenticateWithQuickConnect return
#     200 with an AccessToken; only later requests 401. AuthenticateByName, by
#     contrast, refuses outright with 403. Sauron has to close that gap itself,
#     which is what these tests pin down.


def _expire(user, *, disabled=False, days_ago=1):
    user.is_disabled = disabled
    user.expires = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) - datetime.timedelta(days=days_ago)
    db.session.commit()


def test_an_expired_membership_cannot_connect_a_new_device(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """Jellyfin would hand a lapsed account a token that 401s on every request,
    so the wizard must refuse rather than report a success that is a lie."""
    sent = []
    _client_stub(monkeypatch, sent)
    _expire(provisioned_user)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 403
    assert sent == [], "must not spend the admin key on a lapsed membership"
    assert "Tu membres\u00eda no est\u00e1 activa".encode() in response.data
    assert "\u00a1Conectado!".encode() not in response.data


def test_a_lapsed_membership_is_shown_where_to_renew(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """A bare "not active" leaves the buyer stuck on their couch. The refusal
    carries the renewal button, in Spanish, opening outside the wizard."""
    _client_stub(monkeypatch, [])
    _expire(provisioned_user, disabled=True)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    html = client.post("/wizard/quick-connect", data={"code": "734108"}).data.decode()

    assert f'href="{RENEWAL_URL}"' in html
    assert 'target="_blank"' in html
    assert "Renovar mi membresía" in html
    assert "Renew my membership" not in html


def test_the_renewal_link_is_the_storefront_renewal_page():
    assert RENEWAL_URL == "https://neexy.net/pay?renovar=1"


def test_a_disabled_account_cannot_connect_a_new_device(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """Disabled without an expiry date — an admin suspension, say."""
    sent = []
    _client_stub(monkeypatch, sent)
    provisioned_user.is_disabled = True
    db.session.commit()

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 403
    assert sent == []
    assert "Tu membres\u00eda no est\u00e1 activa".encode() in response.data


def test_a_renewed_membership_can_connect_again(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """The renewal path: expiry pushed into the future and the account enabled
    again. The device has to redo Quick Connect because its old token is dead,
    and this is what makes that possible."""
    _client_stub(monkeypatch, [], response=_Response(200, "true"))
    _expire(provisioned_user, disabled=True)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    assert (
        client.post("/wizard/quick-connect", data={"code": "734108"}).status_code == 403
    )

    # Renewal: extend the expiry AND re-enable. Both are required — extending
    # the date alone leaves the Jellyfin account disabled and nothing works.
    provisioned_user.is_disabled = False
    provisioned_user.expires = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) + datetime.timedelta(days=30)
    db.session.commit()

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert "\u00a1Conectado!".encode() in response.data


def test_a_membership_without_an_expiry_date_still_connects(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    """`expires = None` means "never expires", not "expired"."""
    _client_stub(monkeypatch, [], response=_Response(200, "true"))
    provisioned_user.expires = None
    db.session.commit()

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert "\u00a1Conectado!".encode() in response.data
