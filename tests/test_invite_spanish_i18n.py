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
            "username": "user1",
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
            "username": "user1",
            "email": "user@example.com",
            "password": "ValidPass1",
            "confirm_password": "ValidPass1",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "El usuario o el correo electrónico ya existe." in body
