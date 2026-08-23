"""An expired CSRF token must not dead-end a paid signup.

Flask-WTF defaults `WTF_CSRF_TIME_LIMIT` to one hour and nothing overrode it,
and no `CSRFError` handler existed -- so a user who opened the join page, went
to check their email and came back got Werkzeug's raw 400 ("The CSRF token has
expired."): unbranded, unexplained, and with the invitation code they had just
paid for lost inside the discarded form.

The recovery hands the form back with a fresh token and the code intact.
"""

import datetime

import pytest

from app.extensions import db
from app.models import Invitation, MediaServer

CODE = "CSRFJF001"


def _make_invite():
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="jf-key",
    )
    invitation = Invitation(
        code=CODE,
        used=False,
        unlimited=False,
        created=datetime.datetime.now(datetime.UTC),
    )
    invitation.servers = [server]
    db.session.add_all([server, invitation])
    db.session.commit()
    return server, invitation


def _post_with_bad_token(client):
    return client.post(
        "/invitation/process",
        data={
            "code": CODE,
            "username": "us1",
            "email": "isdf@hotmail.com",
            "password": "Passw0rdok",
            "confirm_password": "Passw0rdok",
            "csrf_token": "expired-or-forged",
        },
        follow_redirects=False,
    )


def test_expired_csrf_returns_the_form_not_a_bare_400(app, client, session):
    """The screenshot: a dead 400 page with no way forward."""
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        response = _post_with_bad_token(client)
        body = response.get_data(as_text=True)

        assert "Bad Request" not in body, "Werkzeug's raw error page reached the user"
        assert response.status_code == 400
        assert "csrf_token" in body, "no fresh token was issued with the form"
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_expired_csrf_preserves_the_invitation_code(app, client, session):
    """The code is what the user paid for; losing it is the real damage."""
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        body = _post_with_bad_token(client).get_data(as_text=True)
        assert CODE in body, "the invitation code was dropped from the reissued form"
        assert "us1" in body and "isdf@hotmail.com" in body, (
            "the user has to retype fields that were never the problem"
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_expired_csrf_never_echoes_the_password(app, client, session):
    """Repopulating a password would write it back into the DOM unvalidated."""
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        body = _post_with_bad_token(client).get_data(as_text=True)
        assert "Passw0rdok" not in body
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_expired_csrf_provisions_nothing(app, client, session):
    """A request that failed CSRF is re-rendered, never acted on."""
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        _post_with_bad_token(client)

        invitation = Invitation.query.filter_by(code=CODE).first()
        assert invitation.used is False
        assert invitation.claimed_at is None, (
            "a CSRF-rejected request still reserved the invitation"
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


@pytest.mark.parametrize("probe", ["%", "_", "%%", "CSRFJF00_", "%1"])
def test_recovery_lookup_is_not_a_code_oracle(app, client, session, probe):
    """The recovery runs before CSRF is established, so `code` is hostile input.

    A LIKE lookup here would make an unauthenticated POST echo back a real,
    paid invitation code -- "%" matches the first row and the reissued form
    renders it. The match must be exact.
    """
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        response = client.post(
            "/invitation/process",
            data={"code": probe, "csrf_token": "garbage"},
        )
        assert CODE not in response.get_data(as_text=True), (
            f"probe {probe!r} leaked a real invitation code to an anonymous caller"
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_recovery_still_matches_a_differently_cased_code(app, client, session):
    """Exactness must not break the case-insensitivity the rest of the flow has."""
    app.config["WTF_CSRF_ENABLED"] = True
    _make_invite()
    try:
        response = client.post(
            "/invitation/process",
            data={"code": CODE.lower(), "csrf_token": "garbage"},
        )
        assert CODE in response.get_data(as_text=True)
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_csrf_token_does_not_expire_on_a_timer(app):
    """One hour is far too short for a flow that waits on an email."""
    assert app.config.get("WTF_CSRF_TIME_LIMIT") is None
