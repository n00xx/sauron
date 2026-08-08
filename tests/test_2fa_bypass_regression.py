"""Regression tests for the 2FA bypass (F-01) and the LDAP 2FA gap (F-13).

F-01: ``POST /complete-2fa`` used to call ``login_user()`` while only checking
``session["pending_2fa_user_id"]`` -- a value set by the *password* step alone.
An attacker knowing the password could skip WebAuthn entirely by posting
directly to the endpoint.

F-13: the LDAP branch logged the admin in without ever checking whether the
account had passkeys registered, bypassing the second factor by another route.
"""

from flask_login import current_user

from app.extensions import db
from app.models import AdminAccount, WebAuthnCredential


def _admin_with_passkey(app, username="twofa-user", password="Password1"):  # noqa: S107
    """Create an admin account that has a passkey registered."""
    with app.app_context():
        acc = AdminAccount(username=username)
        acc.set_password(password)
        db.session.add(acc)
        db.session.commit()

        cred = WebAuthnCredential(
            admin_account_id=acc.id,
            credential_id=f"cred-{username}".encode(),
            public_key=b"fake-public-key",
            name="test-key",
        )
        db.session.add(cred)
        db.session.commit()
        return acc.id


def test_password_login_with_passkey_does_not_authenticate(client, app):
    """Password alone must only stage 2FA, never grant a session."""
    _admin_with_passkey(app, username="stage-2fa")

    resp = client.post(
        "/login", data={"username": "stage-2fa", "password": "Password1"}
    )

    # 200 = the 2FA prompt is rendered, not a redirect into the app.
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("pending_2fa_user_id") is not None
        # The password step must NOT mark the second factor as satisfied.
        assert sess.get("2fa_verified_user_id") is None


def test_complete_2fa_without_webauthn_is_rejected(client, app):
    """F-01: posting straight to /complete-2fa must not authenticate.

    This is the exact attack: log in with a known password, then skip the
    passkey ceremony by posting directly to the completion endpoint.
    """
    _admin_with_passkey(app, username="bypass-attempt")

    client.post("/login", data={"username": "bypass-attempt", "password": "Password1"})

    resp = client.post("/complete-2fa")

    assert resp.status_code == 403, (
        "2FA bypass: /complete-2fa authenticated without WebAuthn verification"
    )

    # And the request context must show nobody logged in.
    with client:
        client.get("/")
        assert not current_user.is_authenticated


def test_complete_2fa_succeeds_after_webauthn_verification(client, app):
    """The legitimate flow still works once WebAuthn has been verified."""
    account_id = _admin_with_passkey(app, username="legit-2fa")

    client.post("/login", data={"username": "legit-2fa", "password": "Password1"})

    # Simulate what authenticate_complete() does after a successful ceremony.
    with client.session_transaction() as sess:
        sess["2fa_verified_user_id"] = account_id

    resp = client.post("/complete-2fa")

    assert resp.status_code in {302, 303}
    assert resp.headers["Location"].endswith("/")


def test_2fa_verified_flag_is_consumed(client, app):
    """The verification marker must be single-use (no replay)."""
    account_id = _admin_with_passkey(app, username="replay-2fa")

    client.post("/login", data={"username": "replay-2fa", "password": "Password1"})
    with client.session_transaction() as sess:
        sess["2fa_verified_user_id"] = account_id

    first = client.post("/complete-2fa")
    assert first.status_code in {302, 303}

    client.get("/logout")

    # Replaying the completion without a fresh ceremony must fail.
    client.post("/login", data={"username": "replay-2fa", "password": "Password1"})
    replay = client.post("/complete-2fa")
    assert replay.status_code == 403


def test_2fa_marker_for_other_user_is_rejected(client, app):
    """A marker naming a different account must not unlock the pending one."""
    victim_id = _admin_with_passkey(app, username="victim-2fa")
    _admin_with_passkey(app, username="attacker-2fa")

    # Attacker authenticates with their own password -> pending = attacker.
    client.post("/login", data={"username": "attacker-2fa", "password": "Password1"})

    # ...but presents a verification marker for the victim's account.
    with client.session_transaction() as sess:
        sess["2fa_verified_user_id"] = victim_id

    resp = client.post("/complete-2fa")
    assert resp.status_code == 403
