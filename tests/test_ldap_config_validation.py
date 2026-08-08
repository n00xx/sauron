"""Migration safety for the F-14 behaviour change.

F-14 made ``admin_group_dn`` mandatory: LDAP admin login now denies when it is
empty, where before it granted admin to any directory user who could bind.

That is the right behaviour, but it turns a silent misconfiguration into a
login failure discovered at the worst moment. These tests pin the two guards
that surface it earlier instead:

* the settings form refuses to save ``allow_admin_bind`` without a group;
* startup logs an explicit error when a database already holds that state.
"""

from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import AdminAccount, LDAPConfiguration


@pytest.fixture
def admin_client(client, app):
    with app.app_context():
        acc = AdminAccount.query.filter_by(username="ldap-cfg-admin").first()
        if acc is None:
            acc = AdminAccount(username="ldap-cfg-admin")
            acc.set_password("Password1")
            db.session.add(acc)
            db.session.commit()

    client.post("/login", data={"username": "ldap-cfg-admin", "password": "Password1"})
    return client


def _base_form(**overrides):
    data = {
        "enabled": "y",
        "server_url": "ldap://ldap.example.com:389",
        "service_account_dn": "cn=wizarr,ou=people,dc=example,dc=com",
        "user_base_dn": "ou=people,dc=example,dc=com",
        "user_search_filter": "(uid={username})",
        "username_attribute": "uid",
        "email_attribute": "mail",
        "user_object_class": "inetOrgPerson",
        "group_base_dn": "ou=groups,dc=example,dc=com",
        "group_object_class": "groupOfUniqueNames",
        "group_member_attribute": "uniqueMember",
    }
    data.update(overrides)
    return data


def _cleanup(app):
    with app.app_context():
        LDAPConfiguration.query.delete()
        db.session.commit()


# ── Form-level guard ───────────────────────────────────────────────────────


def test_form_rejects_admin_bind_without_group(app):
    """allow_admin_bind with no group must fail validation, not save."""
    from app.forms.ldap import LDAPSettingsForm

    with app.test_request_context(
        method="POST",
        data=_base_form(allow_admin_bind="y", admin_group_dn=""),
    ):
        form = LDAPSettingsForm()
        form.admin_group_dn.choices = [("", "-- None --")]

        assert form.validate() is False
        assert "admin_group_dn" in form.errors, (
            "No validation error raised for admin bind without an admin group"
        )


def test_form_accepts_admin_bind_with_group(app):
    from app.forms.ldap import LDAPSettingsForm

    group = "cn=admins,ou=groups,dc=example,dc=com"
    with app.test_request_context(
        method="POST",
        data=_base_form(allow_admin_bind="y", admin_group_dn=group),
    ):
        form = LDAPSettingsForm()
        form.admin_group_dn.choices = [("", "-- None --"), (group, "admins")]

        assert form.validate() is True, form.errors


def test_form_allows_empty_group_when_admin_bind_is_off(app):
    """The group is only required when admin login is actually enabled."""
    from app.forms.ldap import LDAPSettingsForm

    with app.test_request_context(
        method="POST", data=_base_form(admin_group_dn="")
    ):
        form = LDAPSettingsForm()
        form.admin_group_dn.choices = [("", "-- None --")]

        assert form.validate() is True, form.errors


# ── Route-level guard ──────────────────────────────────────────────────────


def test_route_does_not_persist_admin_bind_without_group(admin_client, app):
    """The dangerous state must never reach the database through the UI."""
    _cleanup(app)
    try:
        admin_client.post(
            "/settings/ldap",
            data=_base_form(allow_admin_bind="y", admin_group_dn=""),
        )

        with app.app_context():
            config = LDAPConfiguration.query.first()
            assert config is None or not (
                config.allow_admin_bind and not config.admin_group_dn
            ), "Saved an LDAP config that grants admin to any directory user"
    finally:
        _cleanup(app)


# ── Startup guard for databases already in the bad state ───────────────────


def test_startup_check_flags_existing_bad_config(app):
    """An already-saved bad config must be called out at boot."""
    from app.services.ldap.config_check import warn_on_unsafe_ldap_config

    _cleanup(app)
    with app.app_context():
        db.session.add(
            LDAPConfiguration(
                enabled=True,
                server_url="ldap://ldap.example.com:389",
                user_base_dn="ou=people,dc=example,dc=com",
                allow_admin_bind=True,
                admin_group_dn=None,
            )
        )
        db.session.commit()

    try:
        with app.app_context(), patch.object(app.logger, "error") as logged:
            unsafe = warn_on_unsafe_ldap_config(app)

        assert unsafe is True
        assert logged.called, "Startup did not log anything about the unsafe config"
        message = " ".join(str(a) for a in logged.call_args[0])
        assert "admin_group_dn" in message, (
            f"Startup message does not name the setting to fix: {message}"
        )
    finally:
        _cleanup(app)


def test_startup_check_silent_on_good_config(app):
    from app.services.ldap.config_check import warn_on_unsafe_ldap_config

    _cleanup(app)
    with app.app_context():
        db.session.add(
            LDAPConfiguration(
                enabled=True,
                server_url="ldap://ldap.example.com:389",
                user_base_dn="ou=people,dc=example,dc=com",
                allow_admin_bind=True,
                admin_group_dn="cn=admins,ou=groups,dc=example,dc=com",
            )
        )
        db.session.commit()

    try:
        with app.app_context():
            assert warn_on_unsafe_ldap_config(app) is False
    finally:
        _cleanup(app)
