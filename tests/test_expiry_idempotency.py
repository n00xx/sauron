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


def test_successful_disable_is_never_mistaken_for_failure(app, session, monkeypatch):
    """Regression: a disable that SUCCEEDS must never delete the account.

    This reproduces production faithfully by letting the mocked service call
    COMMIT, exactly as the real `_set_user_enabled_state` does. That commit ends
    the caller's savepoint, so `savepoint.commit()` raised ResourceClosedError,
    the handler read it as "the disable failed", and the account was DELETED —
    right after being disabled successfully on the media server.

    Note that the other mocks in this file deliberately do NOT commit (see
    fake_delete_user above). That is precisely why the whole suite stayed green
    while live instances deleted every expiring account: the tests avoided the
    transaction interaction that causes the bug. Do not "simplify" this mock by
    dropping the commit — the commit IS the test.
    """
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")

        user = User(
            token="jf-user-commit",
            username="paying_customer",
            email="paying@example.com",
            code="CODE3",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        def committing_disable_user(uid):
            """Mirrors the REAL disable_user: succeeds and commits."""
            u = db.session.get(User, uid)
            u.is_disabled = True
            db.session.commit()
            return True

        deleted_ids = []

        def spy_delete_user(uid):
            deleted_ids.append(uid)

        monkeypatch.setattr(expiry_module, "disable_user", committing_disable_user)
        monkeypatch.setattr(expiry_module, "delete_user", spy_delete_user)

        processed = disable_or_delete_user_if_expired()

        # The account must SURVIVE, disabled.
        assert deleted_ids == [], "a successful disable deleted the account"
        assert processed == [user_id]
        surviving = db.session.get(User, user_id)
        assert surviving is not None, "the disabled account was deleted"
        assert surviving.is_disabled is True


def test_sweep_processes_every_expired_user_not_just_the_first(
    app, session, monkeypatch
):
    """Regression: one committing service call must not abort the whole run.

    The ResourceClosedError escaped the per-user handler (the rollback in it
    raised a SECOND one), killing the scheduled job. Only the first expired user
    of each 15-minute run was ever processed, so a backlog silently accumulated
    — which is why an account expired a month earlier was still sitting there
    untouched.
    """
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")

        ids = []
        for n in range(3):
            u = User(
                token=f"jf-multi-{n}",
                username=f"multi{n}",
                email=f"multi{n}@example.com",
                code=f"MULTI{n}",
                expires=datetime.datetime.now(UTC) - timedelta(days=1),
                server_id=server.id,
                is_disabled=False,
            )
            session.add(u)
            session.flush()
            ids.append(u.id)
        session.commit()

        def committing_disable_user(uid):
            u = db.session.get(User, uid)
            u.is_disabled = True
            db.session.commit()
            return True

        monkeypatch.setattr(expiry_module, "disable_user", committing_disable_user)

        processed = disable_or_delete_user_if_expired()

        assert sorted(processed) == sorted(ids), "the run stopped early"
        assert ExpiredUser.query.count() == 3


class _FakeMediaClient:
    """Stands in for the Jellyfin HTTP client, and ONLY for it.

    Everything above it — _set_user_enabled_state, its transaction handling,
    the sweep — runs for real. That matters: every other test in this file
    mocks `disable_user` wholesale, so the body of _set_user_enabled_state was
    never executed by the suite. Two separate production bugs shipped green
    through that hole, the second one being an AttributeError on the very line
    added to fix the first.
    """

    def __init__(self, ok=True):
        self.ok = ok
        self.disabled = []

    def disable_user(self, identifier):
        self.disabled.append(identifier)
        return self.ok

    def enable_user(self, identifier):
        return self.ok


def test_real_disable_user_runs_inside_a_savepoint(app, session, monkeypatch):
    """disable_user() must work when the caller owns an open savepoint.

    `db.session` is a scoped_session and does NOT proxy in_nested_transaction();
    calling it there raised AttributeError, which _set_user_enabled_state
    swallowed into a False return -> the sweep read "disable failed" and
    deleted the account.
    """
    from app.services.media import service as media_service

    fake = _FakeMediaClient()
    monkeypatch.setattr(
        media_service, "get_client_for_media_server", lambda server: fake
    )

    with app.app_context():
        server = _make_jellyfin_server(session)
        user = User(
            token="jf-real-savepoint",
            username="realsp",
            email="realsp@example.com",
            code="CODE8",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        savepoint = db.session.begin_nested()
        result = media_service.disable_user(user_id)
        savepoint.commit()
        db.session.commit()

        assert result is True, "a working media call must not report failure"
        assert fake.disabled == ["jf-real-savepoint"]
        assert db.session.get(User, user_id).is_disabled is True


def test_real_disable_user_runs_as_the_outermost_scope(app, session, monkeypatch):
    """The API routes call it with no transaction of their own — still commits."""
    from app.services.media import service as media_service

    fake = _FakeMediaClient()
    monkeypatch.setattr(
        media_service, "get_client_for_media_server", lambda server: fake
    )

    with app.app_context():
        server = _make_jellyfin_server(session)
        user = User(
            token="jf-real-outer",
            username="realouter",
            email="realouter@example.com",
            code="CODE9",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        assert media_service.disable_user(user_id) is True
        assert db.session.get(User, user_id).is_disabled is True


def test_sweep_end_to_end_without_mocking_the_media_layer(app, session, monkeypatch):
    """The whole sweep, with only the HTTP client faked. THE regression test.

    Both production incidents would have failed here: the savepoint/commit
    collision and the scoped_session AttributeError alike.
    """
    from app.services.media import service as media_service

    fake = _FakeMediaClient()
    monkeypatch.setattr(
        media_service, "get_client_for_media_server", lambda server: fake
    )
    deleted_ids = []
    monkeypatch.setattr(
        expiry_module, "delete_user", lambda uid: deleted_ids.append(uid)
    )

    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")
        for n in (1, 2):
            session.add(
                User(
                    token=f"jf-e2e-{n}",
                    username=f"e2e{n}",
                    email=f"e2e{n}@example.com",
                    code=f"E2E{n}",
                    expires=datetime.datetime.now(UTC) - timedelta(days=1),
                    server_id=server.id,
                    is_disabled=False,
                )
            )
        session.commit()

        processed = disable_or_delete_user_if_expired()

        assert deleted_ids == [], "a successful disable must never delete"
        assert len(processed) == 2, "both users must be processed in ONE run"
        assert sorted(fake.disabled) == ["jf-e2e-1", "jf-e2e-2"]
        for n in (1, 2):
            row = User.query.filter_by(token=f"jf-e2e-{n}").first()
            assert row is not None, "the account must survive"
            assert row.is_disabled is True


def test_disable_failure_never_escalates_to_deletion(app, session, monkeypatch):
    """A refused disable must LEAVE THE ACCOUNT ALONE, never delete it.

    The admin chose "disable". Deletion is irreversible and this sweep runs
    every 15 minutes against paying customers, so a failed disable has to be a
    no-op that retries, not an escalation. This is the shape that actually bit
    us: user3 and user4 were deleted in production while the setting said
    "disable".
    """
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")

        user = User(
            token="jf-refused",
            username="refused",
            email="refused@example.com",
            code="CODE4",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        deleted_ids = []
        monkeypatch.setattr(expiry_module, "disable_user", lambda uid: False)
        monkeypatch.setattr(
            expiry_module, "delete_user", lambda uid: deleted_ids.append(uid)
        )

        processed = disable_or_delete_user_if_expired()

        assert deleted_ids == [], "a failed disable must never delete"
        assert processed == []

        survivor = db.session.get(User, user_id)
        assert survivor is not None, "the account must survive a failed disable"
        # Must stay False: this column is the sweep's own filter, so flipping it
        # after a FAILED disable would exclude an account that is still enabled
        # on the media server from every future run — free access forever.
        assert survivor.is_disabled is False

        # Nothing happened to the account, so no expiry record may be left
        # behind — otherwise every 15-minute run appends another one.
        assert ExpiredUser.query.filter_by(original_user_id=user_id).count() == 0


def test_disable_raising_never_escalates_to_deletion(app, session, monkeypatch):
    """An unreachable media server is a failed disable, not permission to delete."""
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "disable")

        user = User(
            token="jf-raises",
            username="raises",
            email="raises@example.com",
            code="CODE5",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        def exploding_disable(uid):
            raise RuntimeError("media server unreachable")

        deleted_ids = []
        monkeypatch.setattr(expiry_module, "disable_user", exploding_disable)
        monkeypatch.setattr(
            expiry_module, "delete_user", lambda uid: deleted_ids.append(uid)
        )

        processed = disable_or_delete_user_if_expired()

        assert deleted_ids == []
        assert processed == []
        survivor = db.session.get(User, user_id)
        assert survivor is not None
        assert survivor.is_disabled is False
        assert ExpiredUser.query.filter_by(original_user_id=user_id).count() == 0


def test_disable_setting_on_incapable_server_does_not_delete(
    app, session, monkeypatch
):
    """"disable" + a server that cannot disable => skip, never delete.

    Plex cannot disable users. Deleting is NOT a reasonable reading of an admin
    who explicitly chose "disable".
    """
    with app.app_context():
        server = MediaServer(
            name="Test Plex",
            server_type="plex",
            url="http://plex.local",
            api_key="test-key",
        )
        session.add(server)
        session.flush()
        _set_expiry_action(session, "disable")

        user = User(
            token="plex-user",
            username="plexie",
            email="plexie@example.com",
            code="CODE6",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        deleted_ids = []
        monkeypatch.setattr(
            expiry_module, "delete_user", lambda uid: deleted_ids.append(uid)
        )

        processed = disable_or_delete_user_if_expired()

        assert deleted_ids == []
        assert processed == []
        assert db.session.get(User, user_id) is not None
        assert ExpiredUser.query.filter_by(original_user_id=user_id).count() == 0


def test_delete_setting_still_deletes(app, session, monkeypatch):
    """The one path that MAY delete: the admin explicitly chose "delete"."""
    with app.app_context():
        server = _make_jellyfin_server(session)
        _set_expiry_action(session, "delete")

        user = User(
            token="jf-delete-me",
            username="deleteme",
            email="deleteme@example.com",
            code="CODE7",
            expires=datetime.datetime.now(UTC) - timedelta(days=1),
            server_id=server.id,
            is_disabled=False,
        )
        session.add(user)
        session.commit()
        user_id = user.id

        deleted_ids = []
        monkeypatch.setattr(
            expiry_module, "delete_user", lambda uid: deleted_ids.append(uid)
        )

        processed = disable_or_delete_user_if_expired()

        assert deleted_ids == [user_id]
        assert processed == [user_id]
