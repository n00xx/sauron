"""Markup guarantees for the public create-account form (welcome-jellyfin.html).

These assert on rendered HTML because the behaviours are template-level: the
invite code arrives prefilled from the link and must not be editable, the
password rules must be visible before submitting, and the page must not
advertise the upstream project.
"""

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
