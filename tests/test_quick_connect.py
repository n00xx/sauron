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

import pytest

from app.extensions import db
from app.models import MediaServer, User
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
    assert b"Check the code" in response.data


def test_successful_authorisation_tells_the_buyer_to_look_at_the_tv(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    _client_stub(monkeypatch, [], response=_Response(200, "true"))

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert b"Connected!" in response.data


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
    assert b"did not work" in response.data


def test_transport_failure_does_not_leak_a_traceback(
    client, provisioned_user, jellyfin_server, monkeypatch
):
    _client_stub(monkeypatch, [], raises=OSError("connection refused"))

    with client.session_transaction() as sess:
        sess["wizard_access"] = "INVITE1"
        sess[WIZARD_USER_IDS_KEY] = {str(jellyfin_server.id): provisioned_user.id}

    response = client.post("/wizard/quick-connect", data={"code": "734108"})

    assert response.status_code == 200
    assert b"could not connect your device" in response.data
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


def test_widget_offers_every_device_path(app, jellyfin_server, monkeypatch):
    """Samsung is listed separately: Jellyfin's support table has no Tizen row
    for Quick Connect log-in, so those owners must be routed to credentials."""
    with app.test_request_context():
        html = _render_widget(monkeypatch, jellyfin_server, enabled=True)

    for key in ("'tv'", "'console'", "'samsung'", "'other'"):
        assert f"device = {key}" in html


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
    assert b"membership is not active" in response.data
    assert b"Connected!" not in response.data


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
    assert b"membership is not active" in response.data


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
    assert b"Connected!" in response.data


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
    assert b"Connected!" in response.data
