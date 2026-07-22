import dns.exception
import dns.resolver

from app.forms.join import JoinForm
from app.forms.setup import AdminAccountForm
from app.forms.validators import (
    EMAIL_DOMAIN_INVALID_MESSAGE,
    USERNAME_ALLOWED_CHARS_MESSAGE,
    _domain_has_dns_records,
)


def _make_resolve(*, mx=False, a=False, aaaa=False, nxdomain=False, timeout=False):
    """Build a fake ``Resolver.resolve`` for deterministic, network-free tests."""
    present = {"MX": mx, "A": a, "AAAA": aaaa}

    def _resolve(self, domain, record_type, *args, **kwargs):
        if timeout:
            raise dns.exception.Timeout
        if nxdomain:
            raise dns.resolver.NXDOMAIN
        if present.get(record_type):
            return [record_type]  # truthy, non-empty answer set
        raise dns.resolver.NoAnswer

    return _resolve


def _join_form_payload(**overrides):
    base = {
        "username": "validuser",
        "email": "user@example.com",
        "password": "ValidPass1",
        "confirm_password": "ValidPass1",
        "code": "ABCDEF",
    }
    base.update(overrides)
    return base


def _admin_form_payload(**overrides):
    base = {
        "username": "adminuser",
        "password": "ValidPass1",
        "confirm": "ValidPass1",
    }
    base.update(overrides)
    return base


def test_join_form_rejects_spaces_in_username(app):
    with app.test_request_context(
        method="POST", data=_join_form_payload(username="invalid user")
    ):
        form = JoinForm()

        assert not form.validate()
        assert USERNAME_ALLOWED_CHARS_MESSAGE in form.username.errors


def test_join_form_strips_trailing_whitespace(app):
    with app.test_request_context(
        method="POST", data=_join_form_payload(username="validuser ")
    ):
        form = JoinForm()

        assert form.validate()
        assert form.username.data == "validuser"


def test_join_form_rejects_invalid_symbols(app):
    with app.test_request_context(
        method="POST", data=_join_form_payload(username="bad$user")
    ):
        form = JoinForm()

        assert not form.validate()
        assert USERNAME_ALLOWED_CHARS_MESSAGE in form.username.errors


def test_admin_account_form_strips_username_whitespace(app):
    with app.test_request_context(
        method="POST", data=_admin_form_payload(username=" adminuser ")
    ):
        form = AdminAccountForm()

        assert form.validate()
        assert form.username.data == "adminuser"


def test_admin_account_form_rejects_invalid_username(app):
    with app.test_request_context(
        method="POST", data=_admin_form_payload(username="admin user")
    ):
        form = AdminAccountForm()

        assert not form.validate()
        assert USERNAME_ALLOWED_CHARS_MESSAGE in form.username.errors


def test_join_form_rejects_nonexistent_email_domain(app, monkeypatch):
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve", _make_resolve(nxdomain=True)
    )
    with app.test_request_context(
        method="POST", data=_join_form_payload(email="abernal@1232as.com")
    ):
        form = JoinForm()

        assert not form.validate()
        assert EMAIL_DOMAIN_INVALID_MESSAGE in form.email.errors


def test_join_form_accepts_valid_email_domain(app, monkeypatch):
    monkeypatch.setattr(dns.resolver.Resolver, "resolve", _make_resolve(mx=True))
    with app.test_request_context(
        method="POST", data=_join_form_payload(email="user@example.com")
    ):
        form = JoinForm()

        assert form.validate()


def test_join_form_accepts_domain_with_only_a_record(app, monkeypatch):
    # Domain has no MX but resolves via an A record — still deliverable.
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve", _make_resolve(mx=False, a=True)
    )
    with app.test_request_context(
        method="POST", data=_join_form_payload(email="user@a-only.example")
    ):
        form = JoinForm()

        assert form.validate()


def test_join_form_fails_open_on_dns_timeout(app, monkeypatch):
    # A transient DNS timeout must never block a legitimate signup.
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve", _make_resolve(timeout=True)
    )
    with app.test_request_context(
        method="POST", data=_join_form_payload(email="user@slow.example")
    ):
        form = JoinForm()

        assert form.validate()


def test_domain_has_dns_records_falls_back_to_a_record(monkeypatch):
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve", _make_resolve(mx=False, a=True)
    )

    assert _domain_has_dns_records("a-only.example") is True


def test_domain_has_dns_records_false_for_nxdomain(monkeypatch):
    monkeypatch.setattr(
        dns.resolver.Resolver, "resolve", _make_resolve(nxdomain=True)
    )

    assert _domain_has_dns_records("1232as.com") is False
