"""Regression tests for the hardening findings (F-05..F-09, F-15)."""

import json
import stat

import pytest

from app.extensions import db
from app.models import Settings

# ── F-05: DISABLE_BUILTIN_AUTH must not be a blank cheque ──────────────────


def test_disable_builtin_auth_requires_trusted_proxy(client, monkeypatch):
    """The SSO escape hatch must verify the request really came from the proxy.

    Previously the flag alone was enough: any GET /login returned a full admin
    session, so a directly exposed container handed out admin to anyone.
    """
    monkeypatch.setenv("DISABLE_BUILTIN_AUTH", "true")
    monkeypatch.delenv("SSO_TRUSTED_PROXY_IPS", raising=False)

    resp = client.get("/login")

    assert resp.status_code != 302 or not resp.headers.get("Location", "").endswith(
        "/"
    ), "DISABLE_BUILTIN_AUTH granted a session with no trusted-proxy configuration"


def test_disable_builtin_auth_rejects_untrusted_source(client, monkeypatch):
    """A request from outside the trusted proxy list must be refused."""
    monkeypatch.setenv("DISABLE_BUILTIN_AUTH", "true")
    monkeypatch.setenv("SSO_TRUSTED_PROXY_IPS", "10.0.0.1")

    resp = client.get(
        "/login",
        headers={"X-Forwarded-User": "someone"},
        environ_base={"REMOTE_ADDR": "203.0.113.9"},
    )

    assert resp.status_code == 403


def test_disable_builtin_auth_works_from_trusted_proxy(client, app, monkeypatch):
    """The legitimate SSO deployment still works."""
    monkeypatch.setenv("DISABLE_BUILTIN_AUTH", "true")
    monkeypatch.setenv("SSO_TRUSTED_PROXY_IPS", "127.0.0.1")

    resp = client.get(
        "/login",
        headers={"X-Forwarded-User": "sso-admin"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code in {302, 303}


# ── F-06: cookie hardening ─────────────────────────────────────────────────


def test_production_config_hardens_cookies():
    from app.config import ProductionConfig

    assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
    assert ProductionConfig.SESSION_COOKIE_SAMESITE == "Lax"
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.REMEMBER_COOKIE_HTTPONLY is True
    assert ProductionConfig.REMEMBER_COOKIE_SAMESITE == "Lax"
    assert ProductionConfig.REMEMBER_COOKIE_SECURE is True


def test_cookie_secure_can_be_relaxed_for_plain_http(monkeypatch):
    """LAN deployments without TLS need an escape hatch, off by default."""
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    import importlib

    import app.config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.ProductionConfig.SESSION_COOKIE_SECURE is False
    finally:
        monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
        importlib.reload(config_module)


# ── F-07: client IP must not be attacker-controlled ────────────────────────


def test_client_ip_ignores_forwarded_headers_without_trusted_proxy(app):
    """Without a configured proxy count, forwarded headers must be ignored."""
    from app.blueprints.auth.routes import _client_ip

    with app.test_request_context(
        headers={"X-Forwarded-For": "8.8.8.8", "CF-Connecting-IP": "1.1.1.1"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    ):
        assert _client_ip() == "192.0.2.5", (
            "Attacker-supplied headers poisoned the logged client IP"
        )


def test_client_ip_honours_forwarded_header_when_proxy_configured(app, monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")

    from app.blueprints.auth.routes import _client_ip

    with app.test_request_context(
        headers={"X-Forwarded-For": "8.8.8.8"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    ):
        assert _client_ip() == "8.8.8.8"


def test_client_ip_ignores_spoofed_prefix_in_forwarded_chain(app, monkeypatch):
    """A client-supplied X-Forwarded-For prefix must not win.

    With one trusted proxy the real address is the right-most hop, because our
    own proxy appends the peer it actually saw. Anything to its left was sent
    by the client and is forgeable.
    """
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")

    from app.blueprints.auth.routes import _client_ip

    with app.test_request_context(
        headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    ):
        assert _client_ip() == "203.0.113.7", (
            "A spoofed left-most X-Forwarded-For entry was trusted"
        )


def test_client_ip_with_two_trusted_proxies(app, monkeypatch):
    """Two proxies in front means skipping two hops from the right."""
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "2")

    from app.blueprints.auth.routes import _client_ip

    with app.test_request_context(
        headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7, 10.0.0.9"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    ):
        assert _client_ip() == "203.0.113.7"


# ── F-08: secrets file permissions ─────────────────────────────────────────


def test_secrets_file_is_owner_only(tmp_path, monkeypatch):
    """secrets.json holds SECRET_KEY; it must not be world-readable."""
    import app.config as config_module

    target = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "SECRETS_FILE", target)
    monkeypatch.setattr(config_module, "DATABASE_DIR", tmp_path)

    config_module.save_secrets({"SECRET_KEY": "supersecret"})

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"secrets.json is mode {oct(mode)}, expected 0o600"
    assert json.loads(target.read_text())["SECRET_KEY"] == "supersecret"


# ── F-09: legacy admin path must not 500 ───────────────────────────────────


def test_login_with_no_legacy_admin_hash_does_not_error(client, app):
    """check_password_hash(None, ...) used to raise a 500 on a public route.

    Only the legacy Settings rows need to be absent; deleting AdminAccount
    rows here would trip foreign keys from credentials other tests created,
    and is not what this test is about. A username nobody owns exercises the
    same code path.
    """
    with app.app_context():
        Settings.query.filter(
            Settings.key.in_(["admin_username", "admin_password"])
        ).delete(synchronize_session=False)
        db.session.commit()

    resp = client.post(
        "/login", data={"username": "nobody-owns-this", "password": "whatever"}
    )

    assert resp.status_code == 200, (
        f"Public login endpoint returned {resp.status_code} with no legacy admin row"
    )


# ── F-15: custom invite codes ──────────────────────────────────────────────


@pytest.mark.parametrize("code", ["CASA01", "AMIGOS", "SHORT7"])
def test_short_custom_invite_codes_are_rejected(app, code):
    """Guessable 6-char custom codes must not be accepted at creation."""
    from app.services.invites import MIN_CUSTOM_CODESIZE, create_invite

    assert MIN_CUSTOM_CODESIZE >= 8

    with app.app_context(), pytest.raises(ValueError):
        create_invite({"code": code, "expires": "never"})


def test_generated_invite_codes_remain_strong():
    from app.services.invites import CODESET, MAX_CODESIZE, _generate_code

    code = _generate_code()
    assert len(code) == MAX_CODESIZE >= 10
    assert set(code) <= set(CODESET)
    # 100 draws should not collide at 36^10.
    assert len({_generate_code() for _ in range(100)}) == 100
