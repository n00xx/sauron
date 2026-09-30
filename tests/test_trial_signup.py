"""The free-trial signup path: the invitation carries the email the storefront
already verified, and the join form tells people up front when a username is
taken.

Two contracts with the storefront (neexy) are pinned here:

* ``POST /api/invitations`` accepts ``email``; the invitation stores it as
  ``bound_email`` and echoes it back, which is how the storefront knows this
  sauron honours it.
* ``GET /j/<code>/username-available`` answers for a VALID invite only, so it
  is no more of a username oracle than the join form's own error already is.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from unittest.mock import Mock, patch

import pytest
from flask_migrate import upgrade

from app import create_app
from app.config import BaseConfig
from app.extensions import db
from app.models import AdminAccount, ApiKey, Invitation, MediaServer, User

RAW_KEY = "trial-signup-test-key"


@pytest.fixture
def jellyfin(session):
    server = MediaServer(
        name="Jelly",
        server_type="jellyfin",
        url="http://jellyfin.example.com",
        api_key="k",
        verified=True,
    )
    session.add(server)
    session.commit()
    return server


@pytest.fixture
def api_key(session):
    admin = AdminAccount(username="admin")
    session.add(admin)
    session.flush()
    session.add(
        ApiKey(
            name="neexy",
            key_hash=hashlib.sha256(RAW_KEY.encode()).hexdigest(),
            created_by_id=admin.id,
            is_active=True,
        )
    )
    session.commit()
    return RAW_KEY


def _invite(session, server, code="TRIAL1", bound_email=None):
    invitation = Invitation(
        code=code, used=False, unlimited=False, bound_email=bound_email
    )
    session.add(invitation)
    session.flush()
    invitation.servers.append(server)
    session.commit()
    return invitation


def _create(client, key, **extra):
    body = {
        "server_ids": [extra.pop("server_id")],
        "expires_in_days": 1,
        "duration": "1",
        "unlimited": False,
        **extra,
    }
    return client.post(
        "/api/invitations",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        data=json.dumps(body),
    )


# ─── API: the storefront hands over the verified email ──────────────────────


def test_api_stores_and_echoes_the_bound_email(client, session, api_key, jellyfin):
    res = _create(
        client, api_key, server_id=jellyfin.id, email="  Juan.Perez@Gmail.com "
    )
    assert res.status_code == 201
    body = res.get_json()["invitation"]
    assert body["bound_email"] == "juan.perez@gmail.com"
    assert db.session.get(Invitation, body["id"]).bound_email == "juan.perez@gmail.com"


def test_api_without_email_is_unchanged(client, session, api_key, jellyfin):
    res = _create(client, api_key, server_id=jellyfin.id)
    assert res.status_code == 201
    body = res.get_json()["invitation"]
    assert body["bound_email"] is None
    assert db.session.get(Invitation, body["id"]).bound_email is None


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-email",
        "a@b",
        "x@y.com;DROP TABLE invitation",
        "a b@c.com",
        "a@b.com\r\nBcc: x@y.z",
        5,
        "a" * 250 + "@x.com",
    ],
)
def test_api_refuses_a_malformed_email_and_creates_nothing(
    client, session, api_key, jellyfin, bad
):
    res = _create(client, api_key, server_id=jellyfin.id, email=bad)
    assert res.status_code == 400
    assert Invitation.query.count() == 0


# ─── Join form: prefilled, read-only, and enforced by the server ────────────


def test_join_page_prefills_the_bound_email_read_only(client, session, jellyfin):
    _invite(session, jellyfin, bound_email="juan@gmail.com")
    html = client.get("/j/TRIAL1").get_data(as_text=True)
    email_input = next(
        line
        for line in html.splitlines()
        if 'id="email"' in line or 'name="email"' in line
    )
    assert 'value="juan@gmail.com"' in email_input
    assert "readonly" in email_input


def test_join_page_wires_the_availability_check_in_spanish(client, session, jellyfin):
    _invite(session, jellyfin)
    html = client.get("/j/TRIAL1").get_data(as_text=True)
    assert 'data-username-check-url="/j/TRIAL1/username-available"' in html
    assert "Ese usuario ya existe. Elige otro." in html
    assert 'id="username-availability"' in html


def test_join_page_without_bound_email_is_unchanged(client, session, jellyfin):
    _invite(session, jellyfin)
    html = client.get("/j/TRIAL1").get_data(as_text=True)
    email_input = next(line for line in html.splitlines() if 'name="email"' in line)
    assert "readonly" not in email_input


def test_submitted_email_is_replaced_by_the_bound_one(client, session, jellyfin):
    """Editing the read-only field in devtools must not change the account email."""
    _invite(session, jellyfin, bound_email="juan@gmail.com")
    media = Mock()
    media.join.return_value = (False, "stop here")
    with patch(
        "app.services.invitation_flow.workflows.get_client_for_media_server",
        return_value=media,
    ):
        client.post(
            "/invitation/process",
            data={
                "code": "TRIAL1",
                "username": "juanperez1",
                "email": "otro@mailinator.com",
                "password": "ValidPass1",
                "confirm_password": "ValidPass1",
            },
        )
    assert media.join.call_args.kwargs["email"] == "juan@gmail.com"


def test_unbound_invitation_keeps_the_typed_email(client, session, jellyfin):
    _invite(session, jellyfin)
    media = Mock()
    media.join.return_value = (False, "stop here")
    with patch(
        "app.services.invitation_flow.workflows.get_client_for_media_server",
        return_value=media,
    ):
        client.post(
            "/invitation/process",
            data={
                "code": "TRIAL1",
                "username": "juanperez1",
                "email": "typed@example.com",
                "password": "ValidPass1",
                "confirm_password": "ValidPass1",
            },
        )
    assert media.join.call_args.kwargs["email"] == "typed@example.com"


# ─── Username availability ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_live_cache():
    from app.services import username_availability

    username_availability._LIVE_CACHE.clear()
    yield
    username_availability._LIVE_CACHE.clear()


def _live(names):
    """A media client whose GET /Users lists `names`."""
    media = Mock()
    media.get.return_value.json.return_value = [
        {"Name": n, "Id": f"id-{n}"} for n in names
    ]
    return patch(
        "app.services.username_availability.get_client_for_media_server",
        return_value=media,
    )


def _check(client, username, code="TRIAL1"):
    return client.get(
        f"/j/{code}/username-available", query_string={"username": username}
    )


def test_a_free_username_is_available(client, session, jellyfin):
    _invite(session, jellyfin)
    with _live(["admin"]):
        res = _check(client, "juanperez1")
    assert res.status_code == 200
    assert res.get_json() == {"available": True, "reason": None}
    assert res.headers["Cache-Control"] == "no-store"


def test_a_username_sauron_knows_is_taken_whatever_the_case(client, session, jellyfin):
    _invite(session, jellyfin)
    session.add(
        User(
            username="juanperez1",
            email="a@b.com",
            token="t",
            code="X",
            server_id=jellyfin.id,
        )
    )
    session.commit()
    with _live([]):
        res = _check(client, "JuanPerez1")
    assert res.get_json() == {"available": False, "reason": "taken"}


def test_a_username_only_jellyfin_knows_is_taken(client, session, jellyfin):
    """Accounts created straight in Jellyfin (the admin, say) are not in sauron's table."""
    _invite(session, jellyfin)
    with _live(["Abraham2025"]):
        res = _check(client, "abraham2025")
    assert res.get_json() == {"available": False, "reason": "taken"}


def test_jellyfin_down_falls_back_to_sauron_and_does_not_error(
    client, session, jellyfin
):
    _invite(session, jellyfin)
    media = Mock()
    media.get.side_effect = ConnectionError("down")
    with patch(
        "app.services.username_availability.get_client_for_media_server",
        return_value=media,
    ):
        res = _check(client, "juanperez1")
    assert res.status_code == 200
    assert res.get_json()["available"] is True


@pytest.mark.parametrize(
    "bad", ["corto", "con-guion1", "con espacio", "x" * 16, "", "juan'; --"]
)
def test_a_name_that_breaks_the_join_rules_is_reported_as_invalid(
    client, session, jellyfin, bad
):
    _invite(session, jellyfin)
    with _live([]):
        assert _check(client, bad).get_json() == {
            "available": False,
            "reason": "invalid",
        }


def test_without_a_valid_invite_it_answers_nothing(client, session, jellyfin):
    """No invite, no oracle: a stranger cannot probe usernames."""
    with _live(["juanperez1"]):
        assert _check(client, "juanperez1", code="NOPE99").status_code == 404
    used = _invite(session, jellyfin, code="USED99")
    used.used = True
    session.commit()
    with _live(["juanperez1"]):
        assert _check(client, "juanperez1", code="USED99").status_code == 404


# ─── The join itself compares usernames the way Jellyfin does ───────────────


def test_join_refuses_a_case_variant_of_an_existing_username(app, session, jellyfin):
    """Jellyfin rejects "Juan" when "juan" exists; sauron used to let it through
    and the person saw "An unexpected error occurred." instead of the reason."""
    from app.services.media.jellyfin import JellyfinClient

    inv = _invite(session, jellyfin)
    session.add(
        User(
            username="juanperez1",
            email="x@y.com",
            token="t",
            code="X",
            server_id=jellyfin.id,
        )
    )
    session.commit()
    with app.test_request_context():
        client = JellyfinClient(media_server=jellyfin)
        with patch.object(JellyfinClient, "create_user") as create:
            ok, msg = client._do_join(
                "JuanPerez1", "ValidPass1", "ValidPass1", "new@y.com", inv.code
            )
    assert ok is False
    assert "already exists" in str(msg)
    create.assert_not_called()


# ─── Migration: additive, and it must not touch the invitation's children ───


class _Config(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = None


def test_migration_adds_the_column_and_keeps_every_child_row():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        config = _Config()
        config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        app = create_app(config)  # type: ignore[arg-type]
        with app.app_context():
            upgrade(revision="20260928_no_id_reuse")

        conn = sqlite3.connect(path)
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO media_server (name, server_type, url, api_key, verified, created_at) VALUES ('j','jellyfin','http://j','k',1,'2026-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO invitation (code, used, created, unlimited) VALUES ('KEEPME', 0, '2026-01-01 00:00:00', 0)"
        )
        conn.execute(
            "INSERT INTO invitation_server (invite_id, server_id, used) VALUES (1, 1, 0)"
        )
        conn.close()

        with app.app_context():
            upgrade()
            upgrade()  # re-running is a no-op

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(invitation)")]
        assert "bound_email" in cols
        assert conn.execute("SELECT count(*) FROM invitation").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM invitation_server").fetchone()[0] == 1
        assert conn.execute("SELECT bound_email FROM invitation").fetchone()[0] is None
        conn.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)
