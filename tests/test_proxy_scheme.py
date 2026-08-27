"""Absolute URLs must not downgrade to http behind a TLS-terminating proxy.

`/settings/notifications` (no trailing slash) answered 308 towards
`http://sauron.neexy.net/settings/notifications/` — scheme http. HSTS repairs
that in a browser that already knows the host, but a client that does not
applies the redirect as given and sends the session cookie in clear.

The cause is not that route: nothing translated ``X-Forwarded-Proto``, so Flask
saw ``wsgi.url_scheme == "http"`` for every request and built EVERY absolute URL
on it — the strict_slashes redirects and the reset link shown in the admin modal
alike.

The header is attacker-controlled unless a proxy really is in front, so it is
honoured only when ``TRUSTED_PROXY_COUNT`` says so — the same gate
``app.blueprints.auth.routes._client_ip`` already applies to
``X-Forwarded-For``.
"""

from __future__ import annotations

import pytest

from app import create_app
from tests.conftest import TestConfig


@pytest.fixture
def proxied_client(monkeypatch):
    """An app built while a proxy is declared in front of it."""

    def _build(trusted: str | None):
        if trusted is None:
            monkeypatch.delenv("TRUSTED_PROXY_COUNT", raising=False)
        else:
            monkeypatch.setenv("TRUSTED_PROXY_COUNT", trusted)
        return create_app(TestConfig).test_client()

    return _build


def test_redirect_keeps_https_when_a_proxy_is_declared(proxied_client):
    client = proxied_client("1")

    response = client.get(
        "/settings/notifications",
        headers={"X-Forwarded-Proto": "https", "Host": "sauron.neexy.net"},
        follow_redirects=False,
    )

    assert response.status_code in {301, 302, 308}
    assert response.headers["Location"].startswith("https://")


def test_header_is_ignored_when_no_proxy_is_declared(proxied_client):
    """Unproxied, the header is just attacker input and must not be believed."""
    client = proxied_client(None)

    response = client.get(
        "/settings/notifications",
        headers={"X-Forwarded-Proto": "https", "Host": "sauron.neexy.net"},
        follow_redirects=False,
    )

    assert response.status_code in {301, 302, 308}
    assert not response.headers["Location"].startswith("https://")


def test_client_address_is_left_alone(proxied_client):
    """Only the scheme is corrected — never the address.

    ``get_remote_address`` keys every rate limit, and
    ``api_routes.RequestPasswordReset`` deliberately sized its unkeyed caps as a
    QUOTA guard precisely because the storefront's egress address is shared.
    Rewriting remote_addr here would silently redefine those limits.
    """
    client = proxied_client("1")

    seen: dict[str, str | None] = {}
    app = client.application

    @app.route("/__remote_addr_probe")
    def _probe():
        from flask import request

        seen["addr"] = request.remote_addr
        return "ok"

    client.get(
        "/__remote_addr_probe",
        headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"},
    )

    assert seen["addr"] != "203.0.113.9"


def test_url_root_is_https_too(proxied_client):
    """The 308 was one symptom; ``request.url_root`` is the consequential one.

    ``admin.routes`` builds the password reset link shown in the modal on it,
    and an admin who copies an ``http://`` link sends a plaintext one. The
    EMAILED link is already safe — ``resend_email._public_base_url`` prefers the
    stored public URL — but the copyable one was not.
    """
    client = proxied_client("1")
    app = client.application

    seen: dict[str, str] = {}

    @app.route("/__url_root_probe")
    def _root_probe():
        from flask import request

        seen["root"] = request.url_root
        return "ok"

    client.get(
        "/__url_root_probe",
        headers={"X-Forwarded-Proto": "https", "Host": "sauron.neexy.net"},
    )

    assert seen["root"].startswith("https://")
