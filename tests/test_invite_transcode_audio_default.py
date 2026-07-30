"""
The Jellyfin "Allow audio playback that requires transcoding" checkbox must
default to checked for every new invitation going forward, while video
transcoding stays opt-in.
"""

import re

from app.models import AdminAccount, MediaServer
from app.services.invites import create_invite


def _make_admin(session):
    admin = AdminAccount.query.filter_by(username="testadmin").first()
    if admin:
        return admin
    admin = AdminAccount(username="testadmin")
    admin.set_password("TestPass123")
    session.add(admin)
    session.commit()
    return admin


def _login(client):
    resp = client.post(
        "/login", data={"username": "testadmin", "password": "TestPass123"}
    )
    assert resp.status_code in {200, 302, 303}


def _make_jellyfin_server(session):
    server = MediaServer(
        name="Test Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin.local",
        api_key="test-key",
    )
    session.add(server)
    session.commit()
    return server


def test_invite_modal_renders_audio_transcode_checked_by_default(
    client, app, session
):
    with app.app_context():
        _make_admin(session)
        server = _make_jellyfin_server(session)
        _login(client)

        resp = client.get(
            f"/invite?server_id={server.id}", headers={"HX-Request": "true"}
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        audio_input = re.search(
            rf'<input[^>]*id="allow_transcode_audio_{server.id}"[^>]*>', body
        )
        video_input = re.search(
            rf'<input[^>]*id="allow_transcode_video_{server.id}"[^>]*>', body
        )

        assert audio_input is not None
        assert video_input is not None
        assert "checked" in audio_input.group(0)
        assert "checked" not in video_input.group(0)


def test_create_invite_defaults_audio_transcode_true_when_submitted(
    app, session
):
    with app.app_context():
        server = _make_jellyfin_server(session)

        # An untouched form submit sends "true" for a checked-by-default box.
        form = {
            "server_ids": [str(server.id)],
            "expires": "never",
            "allow_transcode_audio": "true",
        }
        invite = create_invite(form)

        assert invite.allow_transcode_audio is True
        assert invite.allow_transcode_video is False


def test_create_invite_respects_unchecked_audio_transcode(app, session):
    with app.app_context():
        server = _make_jellyfin_server(session)

        # Admin explicitly unchecked it: the field is omitted entirely.
        form = {
            "server_ids": [str(server.id)],
            "expires": "never",
        }
        invite = create_invite(form)

        assert invite.allow_transcode_audio is False
