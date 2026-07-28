"""Markup guarantees for the show/hide password control on the public
create-account form (welcome-jellyfin.html).

Scope note: these assert on rendered HTML only. The click behaviour itself —
that pressing the eye actually swaps input.type — is not covered here; the e2e
suite needs Playwright browsers that are not installed in this environment. What
is covered is everything that would make the control silently broken or harmful
on arrival: the button submitting the form, the fields not starting hidden, and
the script/input pairing drifting apart after a rename.
"""

import re

from app.models import Invitation, MediaServer

INVITE_CODE = "PWTOG1"
PASSWORD_FIELDS = ("password", "confirm_password")


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


def _toggle_buttons(body):
    return re.findall(r"<button[^>]*class=\"[^\"]*password-toggle[^\"]*\"[^>]*>", body)


def test_both_password_fields_have_a_toggle(client, session):
    _create_jellyfin_invitation(session)

    buttons = _toggle_buttons(_get_invite_page(client))

    assert len(buttons) == 2
    targets = {re.search(r'data-toggle-for="([^"]+)"', b).group(1) for b in buttons}
    assert targets == set(PASSWORD_FIELDS)


def test_toggle_is_type_button_so_it_never_submits_the_form(client, session):
    """A <button> in a <form> defaults to submit — clicking the eye would post."""
    _create_jellyfin_invitation(session)

    buttons = _toggle_buttons(_get_invite_page(client))

    assert buttons, "no toggle buttons rendered"
    for markup in buttons:
        assert 'type="button"' in markup


def test_toggle_targets_match_real_input_ids(client, session):
    """Guards a rename turning the toggle into a silent no-op."""
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)
    rendered_ids = set(re.findall(r'<input[^>]*id="([^"]+)"', body))

    for markup in _toggle_buttons(body):
        target = re.search(r'data-toggle-for="([^"]+)"', markup).group(1)
        assert target in rendered_ids


def test_passwords_start_hidden(client, session):
    """ "Desactivado por defecto": both fields render as type=password."""
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)

    for name in PASSWORD_FIELDS:
        field = re.search(rf'<input[^>]*name="{name}"[^>]*>', body)
        assert field is not None, f"{name} not rendered"
        assert 'type="password"' in field.group(0)

    for markup in _toggle_buttons(body):
        assert 'aria-pressed="false"' in markup


def test_toggle_labels_render_in_spanish(client, session):
    _create_jellyfin_invitation(session)

    body = _get_invite_page(client)

    assert "Mostrar contraseña" in body
    assert "Ocultar contraseña" in body
    assert "Show password" not in body
    assert "Hide password" not in body


def test_toggles_survive_the_validation_error_render(client, session):
    """The form-with-errors screen is a different render path from GET /j/<code>.

    It is also the screen a user is most likely to be staring at when they want
    to check what they typed, so the toggles have to be there too.
    """
    _create_jellyfin_invitation(session)

    response = client.post(
        "/invitation/process",
        data={
            "code": INVITE_CODE,
            "username": "user1",
            "email": "user@example.com",
            "password": "abcdefgh",  # long enough, fails the complexity rule
            "confirm_password": "abcdefgh",
        },
    )
    body = response.data.decode("utf-8")

    assert response.status_code == 200
    assert "Favor de corregir los campos marcados." in body, "not the error render"
    assert len(_toggle_buttons(body)) == 2
