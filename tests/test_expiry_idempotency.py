"""
Tests for the expiry-job idempotency fix and the shared status classifier.

Regression coverage for the bug where disable_or_delete_user_if_expired()
never marked users as locally disabled nor excluded them from its own query,
causing the same expired user to be re-logged into ExpiredUser on every
15-minute scheduler tick forever.
"""

import datetime
from datetime import UTC, timedelta

from app.extensions import db
from app.models import ExpiredUser, MediaServer, Settings, User
from app.services import expiry as expiry_module
from app.services.expiry import (
    delete_expired_user_records,
    disable_or_delete_user_if_expired,
    get_expiry_status,
    get_recently_expired_users,
)


def _make_jellyfin_server(session):
    server = MediaServer(
        name="Test Jellyfin",
        server_type="jellyfin",
        url="http://jellyfin.local",
        api_key="test-key",
    )
    session.add(server)
    session.flush()
    return server


def _set_expiry_action(session, value):
    setting = Settings(key="expiry_action", value=value)
    session.add(setting)
    session.flush()


def test_disable_branch_does_not_reprocess_already_disabled_user(
    app, session, monkeypatch
):
    """Regression test: running the expiry job twice must not create a
    second ExpiredUser row for the same still-expired user."""
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")

        user = User(
            token="jf-user-1",
            username="user666",
            email="user666@example.com",
            code="CODE1",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()

        monkeypatch.setattr(expiry_module, "disable_user", lambda uid: True)

        # First run: should disable + log exactly once.
        processed = disable_or_delete_user_if_expired()
        assert processed == [user.id]
        assert ExpiredUser.query.count() == 1

        db.session.refresh(user)
        assert user.is_disabled is True

        # Second run (simulating the next 15-minute scheduler tick): the
        # user must be excluded now that is_disabled is True.
        processed_again = disable_or_delete_user_if_expired()
        assert processed_again == []
        assert ExpiredUser.query.count() == 1


def test_delete_branch_removes_user_row(app, session, monkeypatch):
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "delete")

        user = User(
            token="jf-user-2",
            username="user_to_delete",
            email="del@example.com",
            code="CODE2",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        def fake_delete_user(uid):
            # Mirrors the real delete_user()'s local-DB effect without its
            # remote-server call. Deliberately does NOT call db.session.commit()
            # here: the caller (disable_or_delete_user_if_expired) is running
            # inside its own db.session.begin_nested() savepoint, and committing
            # from within it would close that savepoint early.
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
                db.session.flush()

        monkeypatch.setattr(expiry_module, "delete_user", fake_delete_user)

        processed = disable_or_delete_user_if_expired()
        assert processed == [user_id]
        assert ExpiredUser.query.count() == 1
        assert db.session.get(User, user_id) is None


def test_get_expiry_status_classification():
    now = datetime.datetime.now(UTC)

    assert get_expiry_status(None, now=now) == "active"
    assert get_expiry_status(now - timedelta(days=1), now=now) == "expired"
    assert get_expiry_status(now + timedelta(days=1), now=now) == "expiring_soon"
    assert get_expiry_status(now + timedelta(days=3), now=now) == "expiring_soon"
    assert get_expiry_status(now + timedelta(days=10), now=now) == "active"


def test_get_expiry_status_handles_naive_datetime():
    """DB round-trips strip tzinfo; the helper must normalise before comparing."""
    now = datetime.datetime.now(UTC)
    naive_future = (now + timedelta(days=1)).replace(tzinfo=None)

    assert get_expiry_status(naive_future, now=now) == "expiring_soon"


def test_get_recently_expired_users_scopes_to_window(app, session):
    with app.app_context():
        server = _make_jellyfin_server(session)
        now = datetime.datetime.now(UTC)

        recent = ExpiredUser(
            original_user_id=1,
            username="recent_user",
            invitation_code="C1",
            server_id=server.id,
            expired_at=now - timedelta(days=6),
            deleted_at=now - timedelta(days=5),
        )
        old = ExpiredUser(
            original_user_id=2,
            username="old_user",
            invitation_code="C2",
            server_id=server.id,
            expired_at=now - timedelta(days=41),
            deleted_at=now - timedelta(days=40),
        )
        session.add_all([recent, old])
        session.commit()

        recent_results = get_recently_expired_users(days=30)
        usernames = {u.username for u in recent_results}

        assert "recent_user" in usernames
        assert "old_user" not in usernames


def test_delete_expired_user_records_selected_and_all(app, session):
    with app.app_context():
        server = _make_jellyfin_server(session)
        now = datetime.datetime.now(UTC)

        rows = [
            ExpiredUser(
                original_user_id=i,
                username=f"expired_{i}",
                invitation_code=f"C{i}",
                server_id=server.id,
                expired_at=now - timedelta(days=1),
                deleted_at=now,
            )
            for i in range(3)
        ]
        session.add_all(rows)
        session.commit()
        ids = [r.id for r in rows]

        deleted_count = delete_expired_user_records([ids[0]])
        assert deleted_count == 1
        assert ExpiredUser.query.count() == 2

        deleted_count = delete_expired_user_records()
        assert deleted_count == 2
        assert ExpiredUser.query.count() == 0
