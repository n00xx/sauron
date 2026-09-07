"""End-to-end checks that the public invite / create-account flow renders in
Mexican Spanish (es_MX), including the translated validation error messages.

These guard against silent English fallback caused by an msgid mismatch — a
failure mode unit tests on the validators cannot catch.
"""

import dns.resolver

from app.models import Invitation, MediaServer


def _create_jellyfin_invitation(session):
    server = MediaServer(
        name="Test Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin.example.com",
        api_key="test-key",
    )
    invitation = Invitation(code="ESMX01", unlimited=True, used=False)
    session.add(server)
    session.add(invitation)
    session.flush()
    invitation.servers.append(server)
    session.commit()
    return server, invitation


def test_invite_landing_renders_in_spanish(client, session):
    _create_jellyfin_invitation(session)

    response = client.get("/j/ESMX01")
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "¡Te han invitado!" in body
    assert "Crear cuenta" in body
    assert "Aceptar invitación" in body
    # Password rules are shown up-front, in Spanish, before the user submits.
    assert (
        "Mínimo 8 caracteres, con al menos una mayúscula, una minúscula y un número."
        in body
    )
    # So are the username rules — the reason a signup is rejected must be
    # readable before typing, not only after a failed submit.
    assert (
        "Mínimo 7 caracteres, sólo letras y números — sin espacios "
        "ni caracteres especiales." in body
    )
    # The English source strings must be gone from these pages.
    assert "You've been invited!" not in body
    assert "Create Account" not in body
    assert "At least 8 characters" not in body


def test_invalid_email_and_password_errors_render_in_spanish(
    client, session, monkeypatch
):
    _create_jellyfin_invitation(session)

    # Well-formed address on a domain that does not resolve (image-15 scenario).
    def _nxdomain(self, domain, record_type, *args, **kwargs):
        raise dns.resolver.NXDOMAIN

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", _nxdomain)

    response = client.post(
        "/invitation/process",
        data={
            "code": "ESMX01",
            "username": "usuario1",
            "email": "abernal@1232as.com",
            "password": "abcdefgh",  # passes length, fails the complexity rule
            "confirm_password": "abcdefgh",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "Favor de introducir una dirección de correo válida." in body
    assert (
        "La contraseña debe contener al menos una letra mayúscula, "
        "una letra minúscula y un número." in body
    )
    assert "Favor de corregir los campos marcados." in body


def test_duplicate_user_banner_renders_in_spanish(client, session, monkeypatch):
    _create_jellyfin_invitation(session)

    # Form passes validation; the media server reports a duplicate. The real
    # Jellyfin client returns a lazy string, translated under the es_MX request.
    from app.services.media import jellyfin as jellyfin_module

    def _join_conflict(self, *args, **kwargs):
        from flask_babel import lazy_gettext as _l

        return False, _l("User or e-mail already exists.")

    monkeypatch.setattr(jellyfin_module.JellyfinClient, "join", _join_conflict)

    response = client.post(
        "/invitation/process",
        data={
            "code": "ESMX01",
            "username": "usuario1",
            "email": "user@example.com",
            "password": "ValidPass1",
            "confirm_password": "ValidPass1",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "El usuario o el correo electrónico ya existe." in body


def test_username_policy_errors_render_in_spanish(client, session, monkeypatch):
    """A hyphen at signup is what made an account unrenewable — say so in Spanish."""
    _create_jellyfin_invitation(session)

    monkeypatch.setattr(
        dns.resolver.Resolver,
        "resolve",
        lambda self, domain, record_type, *a, **kw: ["MX"],
    )

    response = client.post(
        "/invitation/process",
        data={
            "code": "ESMX01",
            "username": "qa-2026-08",
            "email": "user@example.com",
            "password": "ValidPass1",
            "confirm_password": "ValidPass1",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "El usuario sólo puede contener letras y números" in body
    assert "Username can only contain" not in body


def test_short_username_error_renders_in_spanish(client, session, monkeypatch):
    _create_jellyfin_invitation(session)

    monkeypatch.setattr(
        dns.resolver.Resolver,
        "resolve",
        lambda self, domain, record_type, *a, **kw: ["MX"],
    )

    response = client.post(
        "/invitation/process",
        data={
            "code": "ESMX01",
            "username": "corto1",
            "email": "user@example.com",
            "password": "ValidPass1",
            "confirm_password": "ValidPass1",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "El usuario debe tener entre 7 y 15 caracteres." in body
    assert "Username must be" not in body


# ── The wizard the buyer lands on after signing up ──────────────────────────
#
# The invite page was already forced to es_MX; the wizard was not, so a buyer
# went from a Spanish create-account screen straight into an English wizard.
# These pin the whole path, not just the landing page.


def _wizard_steps(session, markdowns):
    from app.models import WizardStep

    for position, markdown in enumerate(markdowns):
        session.add(
            WizardStep(
                server_type="jellyfin",
                category="post_invite",
                position=position,
                title="{{ _('Watch this first') }}",
                markdown=markdown,
                requires=[],
            )
        )
    session.commit()


def test_wizard_renders_in_spanish_for_the_buyer(client, session):
    """The real post-signup route, not the admin preview."""
    _create_jellyfin_invitation(session)
    _wizard_steps(
        session,
        [
            "## {{ _('Everything in 90 seconds') }}",
            "## {{ _('Get the best quality') }}",
        ],
    )

    with client.session_transaction() as sess:
        sess["wizard_access"] = "ESMX01"

    body = client.get("/wizard/post-wizard/0").data.decode("utf-8")

    assert "Todo en 90 segundos" in body
    assert "Everything in 90 seconds" not in body
    # Chrome around the step, not just the step body.
    assert "Siguiente" in body
    assert "Paso 1 de" in body


def test_wizard_entry_redirect_keeps_the_buyer_in_spanish(client, session):
    """Both join paths redirect to /wizard/, so it must not drop the locale."""
    _create_jellyfin_invitation(session)
    _wizard_steps(session, ["## {{ _('Everything in 90 seconds') }}"])

    with client.session_transaction() as sess:
        sess["wizard_access"] = "ESMX01"

    body = client.get("/wizard/", follow_redirects=True).data.decode("utf-8")

    assert "Todo en 90 segundos" in body


def test_quick_connect_result_is_spanish_over_htmx(client, session):
    """The code box answers over HTMX, which returns a bare partial.

    _select_locale keys off request.endpoint, which is the same either way, but
    this is the one response that never passes through a full page render — worth
    pinning rather than assuming.
    """
    _create_jellyfin_invitation(session)

    with client.session_transaction() as sess:
        sess["wizard_access"] = "ESMX01"

    response = client.post(
        "/wizard/quick-connect",
        data={"code": "123456"},
        headers={"HX-Request": "true"},
    )
    body = response.data.decode("utf-8")

    # No wizard identity in the session, so this is the "session expired" branch.
    assert "Tu sesión expiró" in body
    assert "Your session expired" not in body


def test_admin_step_preview_is_left_in_english(client, session):
    """The preview renders every server type, and only Jellyfin is translated.

    Forcing es_MX here would handstand an admin a half-Spanish Plex page, so the
    preview routes are deliberately outside the forced-locale set.
    """
    _create_jellyfin_invitation(session)
    _wizard_steps(
        session,
        [
            "## {{ _('Everything in 90 seconds') }}",
            "## {{ _('Get the best quality') }}",
        ],
    )

    with client.session_transaction() as sess:
        sess["wizard_access"] = "ESMX01"

    body = client.get("/wizard/jellyfin/0").data.decode("utf-8")

    assert "Step 1 of" in body
