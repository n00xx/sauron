"""Markup guarantees for the public create-account form (welcome-jellyfin.html).

These assert on rendered HTML because the behaviours are template-level: the
invite code arrives prefilled from the link and must not be editable, the
password rules must be visible before submitting, and the page must not
advertise the upstream project.
"""

import html
import re

from app.models import Invitation, MediaServer

INVITE_CODE = "UIFORM"


def _create_jellyfin_invitation(session):
    server = MediaServer(
        name="Test Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin.example.com",
        api_key="test-key",
    )
    invitation = Invitation(code=INVITE_CODE, unlimited=True, used=False)
    session.add(server)
    session.add(invitation)
    session.flush()
    invitation.servers.append(server)
    session.commit()
    return server, invitation


def _get_invite_page(client):
    response = client.get(f"/j/{INVITE_CODE}")
    assert response.status_code == 200
    return response.data.decode("utf-8")


def test_invite_code_field_is_prefilled_and_readonly(client, session):
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    code_input = re.search(r'<input[^>]*name="code"[^>]*>', body)

    assert code_input is not None
    markup = code_input.group(0)
    assert f'value="{INVITE_CODE}"' in markup
    # readonly, NOT disabled: a disabled input is never submitted with the form,
    # which would break redemption.
    assert "readonly" in markup
    assert "disabled" not in markup


def test_password_field_describes_its_requirements(client, session):
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    password_input = re.search(r'<input[^>]*name="password"[^>]*>', body)

    assert password_input is not None
    assert 'aria-describedby="password-requirements"' in password_input.group(0)
    assert 'id="password-requirements"' in body


def test_server_theme_colors_interpolate_into_the_style_block(client, session):
    """The :root custom properties must render as real values, not Jinja text.

    djLint's `--format-css` (wired into .pre-commit-config.yaml) rewrites the
    `{{ ... }}` inside this <style> block into `{ { ... } }`, which Jinja then
    emits verbatim and the page loses its accent colour everywhere. The damage is
    invisible in a diff review and the page still returns 200, so guard it here.
    """
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    root_block = re.search(r":root\s*\{(.*?)\}", body, re.DOTALL)

    assert root_block is not None
    declarations = root_block.group(1)
    assert re.search(r"--color-primary:\s*#[0-9A-Fa-f]{3,8};", declarations)
    assert re.search(r"--color-primary_hover:\s*#[0-9A-Fa-f]{3,8};", declarations)
    assert "gradient_start" not in declarations
    assert "gradient_end" not in declarations


def test_wizarr_footer_is_absent(client, session):
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)

    assert "powered by Wizarr" not in body
    assert "tecnología de Wizarr" not in body
    assert 'id="page-footer"' not in body
    # The reveal/back animations must not target the removed node — anime.js
    # throws on a null target and the form would stop animating open on mobile.
    assert "pageFooter" not in body


def test_the_form_is_what_the_invite_link_opens_on(client, session):
    """No "Accept invitation" card in front of it: the link lands on the form.

    That card was a click that led nowhere else, between paying and creating the
    account.
    """
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    form_screen = re.search(r'<div[^>]*id="form-screen"[^>]*>', body)

    assert form_screen is not None
    markup = form_screen.group(0)
    assert "hidden" not in markup
    assert "opacity-0" not in markup
    assert "pointer-events-none" not in markup
    # The fields used to start transparent and be faded in by the click.
    assert 'style="opacity: 0' not in body
    assert 'id="welcome-screen"' not in body
    assert 'id="accept-invite-btn"' not in body
    # Nothing to go back to, and no script left reaching for the removed nodes —
    # a null target there throws and takes the rest of the page's script with it.
    assert 'id="back-btn"' not in body
    assert "acceptBtn" not in body
    assert "welcomeScreen" not in body
    assert "backBtn" not in body


def test_form_reminds_the_buyer_to_save_their_credentials(client, session):
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    notice = re.search(
        r'<aside[^>]*id="save-credentials-notice"[^>]*>(.*?)</aside>', body, re.DOTALL
    )

    assert notice is not None
    # unescape: the emoji are glued to their sentence with &nbsp; so they
    # never wrap onto a line of their own.
    text = html.unescape(re.sub(r"<[^>]+>", "", notice.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    assert "🔐 ¡Importante! Guarda tus datos de acceso" in text
    assert (
        "Te recomendamos enviarte tu usuario y contraseña por WhatsApp a tu propio "
        "número para que puedas tenerlos siempre a la mano. 📱" in text
    )
    assert (
        "Esto te permitirá recuperarlos fácilmente cuando los necesites, por "
        "ejemplo, si cambias de dispositivo, reinstalas la aplicación o "
        "simplemente olvidas dónde los guardaste." in text
    )
    assert (
        "💡 Tip: Guarda este mensaje en tu WhatsApp para tener tus datos de acceso "
        "disponibles cuando los necesites. 😉" in text
    )
    assert "🎬 ¡Así podrás disfrutar de Neexy sin complicaciones!" in text
    # Shown before the button it is about, not after the account already exists.
    assert body.index('id="save-credentials-notice"') < body.index('id="submit-btn"')
    assert "Save your login details" not in body
