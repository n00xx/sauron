"""The Overseerr/Jellyseerr connection can carry Seerr's URL and API key.

The renewal screen zeroes a lapsed member's Seerr permissions through that
connection (app/services/seerr_access.py), but the form treated the type as
info-only: no URL or API key fields, and name and server disabled — so the
browser never even submitted them. Both are optional; without them the
connection stays info-only, as before.
"""

import pytest
import requests

from app.extensions import db
from app.models import AdminAccount, Connection, MediaServer
from app.services.companions.overseerr import OverseerrClient


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def admin_client(client, session):
    admin = AdminAccount(username="conn-admin")
    admin.set_password("irrelevant")
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def seerr(monkeypatch):
    """Seerr answering the connection test; records what was asked."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return _Resp(200 if headers.get("X-Api-Key") == "good-key" else 401)

    monkeypatch.setattr(
        "app.services.companions.overseerr.requests.get", fake_get, raising=True
    )
    return calls


def test_the_form_offers_url_and_api_key_for_seerr(admin_client, server):
    html = admin_client.get(
        "/settings/connections/create?connection_type=overseerr"
    ).data.decode()

    assert 'name="url"' in html
    assert 'name="api_key"' in html
    assert "disabled" not in html.split('name="name"')[1].split(">")[0]


def test_saving_seerr_with_url_and_key(admin_client, server, seerr):
    response = admin_client.post(
        "/settings/connections/create",
        data={
            "connection_type": "overseerr",
            "name": "Seerr",
            "media_server_id": server.id,
            "url": "http://192.168.8.207:30357",
            "api_key": "good-key",
        },
    )

    assert response.status_code in (200, 302)
    conn = Connection.query.filter_by(connection_type="overseerr").one()
    assert conn.url == "http://192.168.8.207:30357"
    assert conn.api_key == "good-key"
    assert seerr == [
        ("http://192.168.8.207:30357/api/v1/settings/main", {"X-Api-Key": "good-key"})
    ]


def test_a_wrong_key_is_not_saved(admin_client, server, seerr):
    admin_client.post(
        "/settings/connections/create",
        data={
            "connection_type": "overseerr",
            "name": "Seerr",
            "media_server_id": server.id,
            "url": "http://192.168.8.207:30357",
            "api_key": "bad-key",
        },
    )

    assert Connection.query.filter_by(connection_type="overseerr").count() == 0


def test_without_url_and_key_it_stays_info_only(admin_client, server, seerr):
    admin_client.post(
        "/settings/connections/create",
        data={
            "connection_type": "overseerr",
            "name": "Seerr",
            "media_server_id": server.id,
        },
    )

    conn = Connection.query.filter_by(connection_type="overseerr").one()
    assert conn.url in (None, "")
    assert seerr == []


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(None, "success"), (requests.ConnectionError("down"), "error")],
)
def test_the_connection_test_talks_to_seerr(monkeypatch, raised, expected):
    def fake_get(url, headers=None, timeout=None):
        if raised:
            raise raised
        return _Resp(200)

    monkeypatch.setattr("app.services.companions.overseerr.requests.get", fake_get)
    conn = Connection(connection_type="overseerr", url="http://s:1/", api_key="k")

    assert OverseerrClient().test_connection(conn)["status"] == expected


def test_the_modal_offers_url_and_api_key_for_seerr(admin_client, server):
    """The panel's "Add connection" button opens the modal, not the page."""
    html = admin_client.get(
        "/settings/connections/create?connection_type=overseerr",
        headers={"HX-Request": "true"},
    ).data.decode()

    assert "connection-modal-backdrop" in html
    assert 'name="url"' in html
    assert 'name="api_key"' in html
    assert "disabled" not in html.split('name="name"')[1].split(">")[0]
