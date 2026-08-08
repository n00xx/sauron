"""Regression tests for missing CSRF coverage (F-04).

Flask-WTF validates a CSRF token on every ``FlaskForm``, but the app never
registered ``CSRFProtect``. That left the 42 of 64 mutating routes that read
``request.form`` / HTMX JSON directly with no token check at all -- including
``change_password``, ``delete_admin``, ``reset_passkeys`` and ``delete_server``.

The rest of the suite runs with ``WTF_CSRF_ENABLED = False`` (TestConfig), so
these tests turn enforcement on explicitly.
"""

import re

import pytest

from app.extensions import db
from app.models import AdminAccount


@pytest.fixture
def csrf_on(app):
    """Enforce CSRF for a single test."""
    previous = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = True
    yield
    app.config["WTF_CSRF_ENABLED"] = previous


@pytest.fixture
def admin_client(client, app):
    """A logged-in admin client (login happens while CSRF is still off)."""
    with app.app_context():
        acc = AdminAccount.query.filter_by(username="csrf-admin").first()
        if acc is None:
            acc = AdminAccount(username="csrf-admin")
            acc.set_password("Password1")
            db.session.add(acc)
            db.session.commit()

    client.post("/login", data={"username": "csrf-admin", "password": "Password1"})
    return client


def test_csrf_protect_is_registered(app):
    """The global CSRFProtect extension must be initialised on the app."""
    assert "csrf" in app.extensions, (
        "CSRFProtect is not registered; only FlaskForm routes are protected"
    )


def test_mutating_route_without_token_is_rejected(admin_client, csrf_on):
    """A non-FlaskForm POST must be refused when it carries no token."""
    resp = admin_client.post("/settings/scan-libraries")

    assert resp.status_code == 400, (
        f"CSRF: mutating route accepted a tokenless POST (got {resp.status_code})"
    )


def test_destructive_route_without_token_is_rejected(admin_client, csrf_on):
    """The high-value target from the audit: admin password change."""
    resp = admin_client.post(
        "/profile/change-password",
        data={
            "current_password": "Password1",
            "new_password": "attacker-chosen",
            "confirm_password": "attacker-chosen",
        },
    )

    # 400 = CSRF rejected. 404/405 would mean the route moved -- also not a
    # successful state change, but we want to be sure it is the CSRF guard.
    assert resp.status_code == 400, (
        f"CSRF: change_password accepted a tokenless POST (got {resp.status_code})"
    )


def test_base_template_exposes_token_to_htmx(app, admin_client):
    """HTMX requests need the token as a header, set once on <body>."""
    resp = admin_client.get("/settings/")

    body = resp.get_data(as_text=True)
    assert "hx-headers" in body and "X-CSRFToken" in body, (
        "base.html does not propagate the CSRF token to HTMX requests"
    )


def test_login_form_still_works_with_csrf_enforced(client, app, csrf_on):
    """Turning CSRFProtect on must not lock everyone out of /login.

    login.html renders a plain <form method="post"> rather than a FlaskForm,
    so registering CSRFProtect without adding a token to the template made the
    login page return 400 for every submission. The rest of the suite runs
    with CSRF disabled, which hid it -- hence this explicit test.
    """
    page = client.get("/login")
    body = page.get_data(as_text=True)

    assert "csrf_token" in body, "login.html renders no CSRF token"

    token = re.search(
        r'name="csrf_token"[^>]*value="([^"]+)"', body
    ) or re.search(r'value="([^"]+)"[^>]*name="csrf_token"', body)
    assert token, "could not extract the CSRF token from login.html"

    resp = client.post(
        "/login",
        data={
            "username": "nobody",
            "password": "wrong",
            "csrf_token": token.group(1),
        },
    )

    # 200 = credentials rejected, which is the correct outcome here.
    # 400 would mean CSRF refused a legitimately tokened submission.
    assert resp.status_code != 400, "CSRF rejected a properly tokened login POST"


@pytest.mark.parametrize(
    "template",
    [
        "login.html",
        "password-reset-form.html",
        "choose-password.html",
        "user-plex-login.html",
        "settings/wizard/form.html",
    ],
)
def test_post_forms_carry_a_token(template):
    """Every plain POST form must emit a token now that CSRF is enforced."""
    from pathlib import Path

    src = Path("app/templates") / template
    body = src.read_text(encoding="utf-8")

    if not re.search(r'<form\b[^>]*method\s*=\s*["\']?post', body, re.IGNORECASE):
        pytest.skip(f"{template} has no POST form")

    assert "csrf_token" in body or "hidden_tag" in body, (
        f"{template} posts without a CSRF token and will 400"
    )


def test_api_key_routes_stay_exempt(client, app, csrf_on):
    """The X-API-Key API is cookie-less, so CSRF does not apply to it.

    It must not start returning 400 once CSRFProtect is registered -- that
    would break every API client.
    """
    resp = client.post(
        "/api/invitations",
        headers={"X-API-Key": "definitely-not-a-valid-key"},
        json={},
    )

    # 401 = reached the API key check (correct). 400 = CSRF blocked it (wrong).
    assert resp.status_code != 400, (
        "CSRFProtect is blocking the X-API-Key API; the blueprint needs an exemption"
    )
