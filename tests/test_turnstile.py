"""Tests for Cloudflare Turnstile login protection."""

import requests

from app.services import turnstile


class _FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


# ── verify_turnstile ──────────────────────────────────────────────────────


def test_verify_returns_false_when_token_missing(monkeypatch):
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")
    assert turnstile.verify_turnstile(None) is False
    assert turnstile.verify_turnstile("") is False


def test_verify_valid_token_returns_true(monkeypatch):
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")
    monkeypatch.setattr(
        turnstile.requests, "post", lambda *a, **k: _FakeResponse({"success": True})
    )
    assert turnstile.verify_turnstile("good-token", "1.2.3.4") is True


def test_verify_rejected_token_returns_false(monkeypatch):
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")
    monkeypatch.setattr(
        turnstile.requests,
        "post",
        lambda *a, **k: _FakeResponse(
            {"success": False, "error-codes": ["invalid-input-response"]}
        ),
    )
    assert turnstile.verify_turnstile("bad-token") is False


def test_verify_fails_open_on_network_error(monkeypatch):
    """A Cloudflare outage must not lock admins out (fail-open)."""
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")

    def _boom(*a, **k):
        raise requests.ConnectionError("cloudflare unreachable")

    monkeypatch.setattr(turnstile.requests, "post", _boom)
    assert turnstile.verify_turnstile("any-token") is True


def test_verify_fails_open_on_timeout(monkeypatch):
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")

    def _timeout(*a, **k):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(turnstile.requests, "post", _timeout)
    assert turnstile.verify_turnstile("any-token") is True


def test_verify_fails_open_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: None)
    assert turnstile.verify_turnstile("any-token") is True


# ── is_turnstile_enabled ──────────────────────────────────────────────────


def test_env_override_false_beats_db(monkeypatch):
    """TURNSTILE_ENABLED=false must force-disable regardless of DB (lockout escape)."""
    monkeypatch.setenv("TURNSTILE_ENABLED", "false")
    monkeypatch.setattr(turnstile, "_setting", lambda key: "true")
    monkeypatch.setattr(turnstile, "get_site_key", lambda: "site")
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")
    assert turnstile.is_turnstile_enabled() is False


def test_disabled_when_keys_missing(monkeypatch):
    """Enabled flag but no keys => treated as disabled, never blocks login."""
    monkeypatch.delenv("TURNSTILE_ENABLED", raising=False)
    monkeypatch.setattr(turnstile, "_setting", lambda key: "true")
    monkeypatch.setattr(turnstile, "get_site_key", lambda: None)
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: None)
    assert turnstile.is_turnstile_enabled() is False


def test_enabled_when_flag_and_keys_present(monkeypatch):
    monkeypatch.delenv("TURNSTILE_ENABLED", raising=False)
    monkeypatch.setattr(
        turnstile,
        "_setting",
        lambda key: "true" if key == "turnstile_enabled" else None,
    )
    monkeypatch.setattr(turnstile, "get_site_key", lambda: "site")
    monkeypatch.setattr(turnstile, "get_secret_key", lambda: "secret")
    assert turnstile.is_turnstile_enabled() is True


# ── login route integration ───────────────────────────────────────────────


def test_login_blocked_when_turnstile_fails(client, monkeypatch):
    from app.services import turnstile as ts

    monkeypatch.setattr(ts, "is_turnstile_enabled", lambda: True)
    monkeypatch.setattr(ts, "verify_turnstile", lambda token, ip=None: False)

    resp = client.post(
        "/login",
        data={"username": "admin", "password": "whatever", "auth_method": "local"},
    )
    assert resp.status_code == 200
    assert b"Captcha verification failed" in resp.data


def test_login_proceeds_past_turnstile_when_token_valid(client, monkeypatch):
    """Valid captcha but wrong creds should reach the normal auth failure."""
    from app.services import turnstile as ts

    monkeypatch.setattr(ts, "is_turnstile_enabled", lambda: True)
    monkeypatch.setattr(ts, "verify_turnstile", lambda token, ip=None: True)

    resp = client.post(
        "/login",
        data={
            "username": "nope",
            "password": "wrong",
            "auth_method": "local",
            "cf-turnstile-response": "good",
        },
    )
    assert resp.status_code == 200
    assert b"Captcha verification failed" not in resp.data
    assert b"Invalid username or password" in resp.data


def test_login_unaffected_when_turnstile_disabled(client, monkeypatch):
    from app.services import turnstile as ts

    monkeypatch.setattr(ts, "is_turnstile_enabled", lambda: False)

    resp = client.post(
        "/login",
        data={"username": "nope", "password": "wrong", "auth_method": "local"},
    )
    assert resp.status_code == 200
    assert b"Captcha verification failed" not in resp.data
    assert b"Invalid username or password" in resp.data
