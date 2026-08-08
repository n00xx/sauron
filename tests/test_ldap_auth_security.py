"""Regression tests for the LDAP authentication weaknesses (F-13, F-14).

F-13: the LDAP branch called ``login_user()`` directly and never checked
whether the account had passkeys registered, so an admin protected by a
passkey could skip the second factor simply by authenticating through LDAP.

F-14: the admin-group restriction was optional. With ``admin_group_dn`` unset,
any directory user who could bind got an AdminAccount created on the fly and
was logged straight in.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import AdminAccount, LDAPConfiguration, WebAuthnCredential


def _ldap_config(app, *, admin_group_dn):
    from app.services.ldap.encryption import encrypt_credential

    with app.app_context():
        config = LDAPConfiguration(
            enabled=True,
            server_url="ldap://ldap.example.com:389",
            use_tls=True,
            verify_cert=True,
            service_account_dn="cn=wizarr,ou=people,dc=example,dc=com",
            service_account_password_encrypted=encrypt_credential("test_password"),
            user_base_dn="ou=people,dc=example,dc=com",
            user_search_filter="(uid={username})",
            username_attribute="uid",
            email_attribute="mail",
            user_object_class="inetOrgPerson",
            group_base_dn="ou=groups,dc=example,dc=com",
            group_object_class="groupOfUniqueNames",
            group_member_attribute="uniqueMember",
            allow_admin_bind=True,
            admin_group_dn=admin_group_dn,
        )
        db.session.add(config)
        db.session.commit()
        return config.id


def _cleanup(app, config_id, username):
    with app.app_context():
        config = db.session.get(LDAPConfiguration, config_id)
        if config:
            db.session.delete(config)
        acc = AdminAccount.query.filter_by(username=username).first()
        if acc:
            WebAuthnCredential.query.filter_by(admin_account_id=acc.id).delete()
            db.session.delete(acc)
        db.session.commit()


def _mock_ldap(mock_client_class, *, username, in_admin_group=True):
    client = MagicMock()
    client.authenticate_user.return_value = (
        True,
        {
            "dn": f"uid={username},ou=people,dc=example,dc=com",
            "mail": f"{username}@example.com",
        },
    )
    client.get_user_groups.return_value = (
        [{"dn": "cn=admins,ou=groups,dc=example,dc=com"}] if in_admin_group else []
    )
    mock_client_class.return_value = client
    return client


# ── F-14: no admin group means no admin ────────────────────────────────────


@patch("app.blueprints.auth.ldap_auth.LDAPClient")
def test_ldap_without_admin_group_is_rejected(mock_client_class, app):
    """An unset admin_group_dn must deny, not silently grant admin."""
    config_id = _ldap_config(app, admin_group_dn=None)
    _mock_ldap(mock_client_class, username="anyone")

    try:
        from app.blueprints.auth.ldap_auth import handle_ldap_login

        with app.test_request_context():
            success, message, account = handle_ldap_login("anyone", "password123")

        assert success is False, (
            "LDAP granted admin with no admin group configured (F-14)"
        )
        assert account is None

        with app.app_context():
            assert AdminAccount.query.filter_by(username="anyone").first() is None, (
                "An admin account was auto-provisioned without authorisation"
            )
    finally:
        _cleanup(app, config_id, "anyone")


@patch("app.blueprints.auth.ldap_auth.LDAPClient")
def test_ldap_user_outside_admin_group_is_rejected(mock_client_class, app):
    """Group membership is still enforced when the group *is* configured."""
    config_id = _ldap_config(
        app, admin_group_dn="cn=admins,ou=groups,dc=example,dc=com"
    )
    _mock_ldap(mock_client_class, username="outsider", in_admin_group=False)

    try:
        from app.blueprints.auth.ldap_auth import handle_ldap_login

        with app.test_request_context():
            success, _message, account = handle_ldap_login("outsider", "password123")

        assert success is False
        assert account is None
        with app.app_context():
            assert AdminAccount.query.filter_by(username="outsider").first() is None
    finally:
        _cleanup(app, config_id, "outsider")


# ── F-13: LDAP must respect the second factor ──────────────────────────────


@patch("app.blueprints.auth.ldap_auth.LDAPClient")
def test_ldap_login_does_not_establish_session(mock_client_class, app):
    """handle_ldap_login must return the account, not log it in itself.

    Session establishment belongs to the route, which is the only place that
    knows whether a second factor is still outstanding.
    """
    config_id = _ldap_config(
        app, admin_group_dn="cn=admins,ou=groups,dc=example,dc=com"
    )
    _mock_ldap(mock_client_class, username="ldapadmin")

    try:
        from flask_login import current_user

        from app.blueprints.auth.ldap_auth import handle_ldap_login

        with app.test_request_context():
            success, _message, account = handle_ldap_login("ldapadmin", "password123")

            assert success is True
            assert account is not None
            assert account.username == "ldapadmin"
            assert not current_user.is_authenticated, (
                "handle_ldap_login established a session on its own"
            )
    finally:
        _cleanup(app, config_id, "ldapadmin")


@patch("app.blueprints.auth.ldap_auth.LDAPClient")
def test_ldap_login_with_passkey_requires_second_factor(mock_client_class, client, app):
    """F-13: an LDAP admin holding a passkey must still be challenged."""
    config_id = _ldap_config(
        app, admin_group_dn="cn=admins,ou=groups,dc=example,dc=com"
    )
    _mock_ldap(mock_client_class, username="passkey-ldap")

    with app.app_context():
        acc = AdminAccount(username="passkey-ldap")
        db.session.add(acc)
        db.session.commit()
        db.session.add(
            WebAuthnCredential(
                admin_account_id=acc.id,
                credential_id=b"cred-passkey-ldap",
                public_key=b"fake-public-key",
                name="test-key",
            )
        )
        db.session.commit()

    try:
        resp = client.post(
            "/login",
            data={
                "username": "passkey-ldap",
                "password": "password123",
                "auth_method": "ldap",
            },
        )

        # 200 = the 2FA prompt, not a 302 straight into the app.
        assert resp.status_code == 200, (
            "LDAP login bypassed the passkey second factor (F-13)"
        )
        with client.session_transaction() as sess:
            assert sess.get("pending_2fa_user_id") is not None
            assert sess.get("2fa_verified_user_id") is None
    finally:
        _cleanup(app, config_id, "passkey-ldap")


@patch("app.blueprints.auth.ldap_auth.LDAPClient")
def test_ldap_login_without_passkey_still_works(mock_client_class, client, app):
    """The ordinary LDAP path must keep working."""
    config_id = _ldap_config(
        app, admin_group_dn="cn=admins,ou=groups,dc=example,dc=com"
    )
    _mock_ldap(mock_client_class, username="plain-ldap")

    try:
        resp = client.post(
            "/login",
            data={
                "username": "plain-ldap",
                "password": "password123",
                "auth_method": "ldap",
            },
        )
        assert resp.status_code in {302, 303}
        assert resp.headers["Location"].endswith("/")
    finally:
        _cleanup(app, config_id, "plain-ldap")


@pytest.mark.parametrize("password_hash", [None])
def test_ldap_created_account_cannot_login_locally(app, password_hash):
    """An LDAP-provisioned account has no local password and must reject one."""
    with app.app_context():
        acc = AdminAccount(username="nolocal")
        acc.password_hash = password_hash
        db.session.add(acc)
        db.session.commit()

        assert acc.check_password("") is False
        assert acc.check_password("anything") is False

        db.session.delete(acc)
        db.session.commit()
