"""
Integration tests for the Expired Users management UI (Recently Expired /
All Expired) and the Users grid status filter/badges added alongside the
expiry idempotency fix.
"""

import datetime
from datetime import UTC, timedelta

import pytest

from app.extensions import db
from app.models import AdminAccount, ExpiredUser, MediaServer, User


@pytest.fixture
def admin_user(app):
    with app.app_context():
        created = False
        previous_hash = None
        admin = AdminAccount.query.filter_by(username="testadmin").first()
        if not admin:
            admin = AdminAccount(username="testadmin")
            admin.set_password("TestPass123")
            db.session.add(admin)
            db.session.commit()
            created = True
        else:
            previous_hash = admin.password_hash
            admin.set_password("TestPass123")
            db.session.commit()
        yield admin
        if created:
            db.session.delete(admin)
            db.session.commit()
        elif previous_hash is not None:
            admin = AdminAccount.query.filter_by(username="testadmin").first()
            if admin:
                admin.password_hash = previous_hash
                db.session.commit()


def _login(client):
    resp = client.post(
        "/login", data={"username": "testadmin", "password": "TestPass123"}
    )
    assert resp.status_code in {200, 302, 303}


def _make_server(session):
    server = MediaServer(
        name="Test Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin.local",
        api_key="test-key",
    )
    session.add(server)
    session.flush()
    return server


def test_recently_expired_table_shows_clear_and_delete_buttons(
    client, app, session, admin_user
):
    with app.app_context():
        server = _make_server(session)
        now = datetime.datetime.now(UTC)
        session.add(
            ExpiredUser(
                original_user_id=1,
                username="recent_expired_user",
                invitation_code="C1",
                server_id=server.id,
                expired_at=now - timedelta(days=1),
                deleted_at=now,
            )
        )
        session.commit()

        _login(client)
        resp = client.get("/recently-expired-users/table")

        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "recent_expired_user" in body
        assert "Clear All" in body
        assert "Delete Selected" in body


def test_all_expired_table_shows_delete_all_button(client, app, session, admin_user):
    with app.app_context():
        server = _make_server(session)
        now = datetime.datetime.now(UTC)
        session.add(
            ExpiredUser(
                original_user_id=1,
                username="history_user",
                invitation_code="C1",
                server_id=server.id,
                expired_at=now - timedelta(days=90),
                deleted_at=now - timedelta(days=89),
            )
        )
        session.commit()

        _login(client)
        resp = client.get("/expired-users/table")

        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "history_user" in body
        assert "Delete All" in body


def test_delete_selected_removes_only_targeted_rows(client, app, session, admin_user):
    with app.app_context():
        server = _make_server(session)
        now = datetime.datetime.now(UTC)
        keep = ExpiredUser(
            original_user_id=1,
            username="keep_me",
            invitation_code="C1",
            server_id=server.id,
            expired_at=now,
            deleted_at=now,
        )
        remove = ExpiredUser(
            original_user_id=2,
            username="remove_me",
            invitation_code="C2",
            server_id=server.id,
            expired_at=now,
            deleted_at=now,
        )
        session.add_all([keep, remove])
        session.commit()
        remove_id = remove.id

        _login(client)
        resp = client.post("/expired-users/delete", data={"ids": [str(remove_id)]})

        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refreshExpiredUsers"
        remaining = {u.username for u in ExpiredUser.query.all()}
        assert remaining == {"keep_me"}


def test_delete_all_wipes_history(client, app, session, admin_user):
    with app.app_context():
        server = _make_server(session)
        now = datetime.datetime.now(UTC)
        session.add_all(
            [
                ExpiredUser(
                    original_user_id=i,
                    username=f"user_{i}",
                    invitation_code=f"C{i}",
                    server_id=server.id,
                    expired_at=now,
                    deleted_at=now,
                )
                for i in range(3)
            ]
        )
        session.commit()

        _login(client)
        resp = client.post("/expired-users/delete", data={"all": "true"})

        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refreshExpiredUsers"
        assert ExpiredUser.query.count() == 0


def test_users_table_status_filter_and_badges(client, app, session, admin_user):
    with app.app_context():
        server = _make_server(session)
        now = datetime.datetime.now(UTC)

        expired = User(
            token="tok-expired",
            username="expired_user",
            email="expired@example.com",
            code="C1",
            expires=now - timedelta(days=1),
            server_id=server.id,
        )
        soon = User(
            token="tok-soon",
            username="soon_user",
            email="soon@example.com",
            code="C2",
            expires=now + timedelta(days=1),
            server_id=server.id,
        )
        active = User(
            token="tok-active",
            username="active_user",
            email="active@example.com",
            code="C3",
            expires=now + timedelta(days=30),
            server_id=server.id,
        )
        session.add_all([expired, soon, active])
        session.commit()

        _login(client)

        # No filter: all three present, each carrying its own badge text.
        resp = client.get("/users/table")
        body = resp.get_data(as_text=True)
        assert "expired_user" in body
        assert "soon_user" in body
        assert "active_user" in body

        # Filtered to expired only.
        resp = client.get("/users/table?status=expired")
        body = resp.get_data(as_text=True)
        assert "expired_user" in body
        assert "soon_user" not in body
        assert "active_user" not in body

        # Filtered to expiring_soon only.
        resp = client.get("/users/table?status=expiring_soon")
        body = resp.get_data(as_text=True)
        assert "soon_user" in body
        assert "expired_user" not in body
        assert "active_user" not in body

        # Filtered to active only.
        resp = client.get("/users/table?status=active")
        body = resp.get_data(as_text=True)
        assert "active_user" in body
        assert "expired_user" not in body
        assert "soon_user" not in body
