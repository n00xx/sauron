"""Tests for the streaming expiry-notification feature.

Covers the Jellyfin ``send_message`` client command, the base no-op for
unsupported servers, user<->session matching, per-window idempotency, and the
end-to-end orchestration in ``notify_expiring_streaming_users``.
"""

import datetime
from datetime import UTC, timedelta
from unittest.mock import MagicMock

from app.models import MediaServer, User
from app.services import expiry_notify
from app.services.expiry_notify import (
    EXPIRY_MESSAGE_HEADER,
    EXPIRY_MESSAGE_TEXT,
    _already_notified_this_window,
    _find_session_for_user,
    notify_expiring_streaming_users,
)


# Tests for the client message command
def test_jellyfin_send_message_posts_to_session_endpoint():
    from app.services.media.jellyfin import JellyfinClient

    client = JellyfinClient.__new__(JellyfinClient)  # skip __init__/Settings
    client.post = MagicMock(return_value=MagicMock())

    ok = client.send_message("sess-1", "hola", header="Aviso", timeout_ms=5000)

    assert ok is True
    client.post.assert_called_once()
    path, kwargs = client.post.call_args[0][0], client.post.call_args[1]
    assert path == "/Sessions/sess-1/Message"
    assert kwargs["json"] == {"Header": "Aviso", "Text": "hola", "TimeoutMs": 5000}


def test_jellyfin_send_message_returns_false_on_error():
    from app.services.media.jellyfin import JellyfinClient

    client = JellyfinClient.__new__(JellyfinClient)
    client.post = MagicMock(side_effect=RuntimeError("boom"))

    assert client.send_message("sess-1", "hola") is False


def test_jellyfin_send_message_rejects_empty_input():
    from app.services.media.jellyfin import JellyfinClient

    client = JellyfinClient.__new__(JellyfinClient)
    client.post = MagicMock()

    assert client.send_message("", "hola") is False
    assert client.send_message("sess-1", "") is False
    client.post.assert_not_called()


def test_base_client_send_message_is_unsupported_noop():
    from app.services.media.audiobookshelf import AudiobookshelfClient

    client = AudiobookshelfClient.__new__(AudiobookshelfClient)
    # Base MediaClient.send_message is inherited and must report unsupported.
    assert client.send_message("sess", "text") is False


# Tests for matching an expiring user to a live session
def _user(**kw):
    """Lightweight stand-in for the User row (avoids SQLAlchemy instrumentation)."""
    from types import SimpleNamespace

    defaults = {
        "token": "tok",
        "username": "alice",
        "server_id": 1,
        "expiry_notified_at": None,
        "expires": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_find_session_matches_by_token():
    by_id = {(1, "tok"): {"session_id": "s1"}}
    match = _find_session_for_user(_user(), by_id, {})
    assert match == {"session_id": "s1"}


def test_find_session_falls_back_to_username_same_server():
    by_name = {(1, "alice"): {"session_id": "s2"}}
    match = _find_session_for_user(_user(token=""), {}, by_name)
    assert match == {"session_id": "s2"}


def test_find_session_wrong_server_no_match():
    # Session is on server 2, user is on server 1 -> no match.
    by_id = {(2, "tok"): {"session_id": "s3"}}
    assert _find_session_for_user(_user(), by_id, {}) is None


def test_find_session_none_when_not_streaming():
    assert _find_session_for_user(_user(), {}, {}) is None


# Tests for per-window idempotency
def test_not_notified_when_timestamp_missing():
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    u = _user(expiry_notified_at=None, expires=now + timedelta(days=2))
    assert _already_notified_this_window(u) is False


def test_notified_inside_current_window_is_skipped():
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=2)  # window opened 5 days ago
    u = _user(expiry_notified_at=now - timedelta(hours=1), expires=expires)
    assert _already_notified_this_window(u) is True


def test_notification_before_window_open_allows_renotify():
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=2)
    window_open = expires - timedelta(days=expiry_notify.EXPIRING_WINDOW_DAYS)
    # Notified before this window opened (e.g. a previous, since-renewed cycle).
    u = _user(expiry_notified_at=window_open - timedelta(days=1), expires=expires)
    assert _already_notified_this_window(u) is False


# End-to-end orchestration tests
def _make_server_and_user(session, *, token, expires_in_days=2, server_type="jellyfin"):
    server = MediaServer(
        name="JF",
        server_type=server_type,
        url="http://localhost:8096",
        api_key="k",
    )
    session.add(server)
    session.flush()
    expires = (datetime.datetime.now(UTC) + timedelta(days=expires_in_days)).replace(
        tzinfo=None
    )
    user = User(
        token=token,
        username="alice",
        email="a@e.com",
        code="C1",
        expires=expires,
        server_id=server.id,
    )
    session.add(user)
    session.commit()
    return server, user


def test_notify_sends_to_streaming_expiring_user(app, session, monkeypatch):
    with app.app_context():
        server, user = _make_server_and_user(session, token="jf-user-1")

        fake_client = MagicMock()
        fake_client.send_message.return_value = True

        monkeypatch.setattr(
            expiry_notify, "get_client_for_media_server", lambda s: fake_client
        )
        monkeypatch.setattr(
            expiry_notify,
            "get_now_playing_all_servers",
            lambda: [
                {
                    "server_id": server.id,
                    "user_id": "jf-user-1",
                    "user_name": "alice",
                    "session_id": "sess-abc",
                    "state": "playing",
                }
            ],
        )

        result = notify_expiring_streaming_users()

        assert result["notified"] == 1
        fake_client.send_message.assert_called_once_with(
            session_id="sess-abc",
            text=EXPIRY_MESSAGE_TEXT,
            header=EXPIRY_MESSAGE_HEADER,
        )
        assert user.expiry_notified_at is not None


def test_notify_skips_when_not_streaming(app, session, monkeypatch):
    with app.app_context():
        _make_server_and_user(session, token="jf-user-1")

        fake_client = MagicMock()
        monkeypatch.setattr(
            expiry_notify, "get_client_for_media_server", lambda s: fake_client
        )
        # Paused session -> not "playing".
        monkeypatch.setattr(
            expiry_notify,
            "get_now_playing_all_servers",
            lambda: [
                {
                    "server_id": 999,
                    "user_id": "jf-user-1",
                    "user_name": "alice",
                    "session_id": "sess",
                    "state": "paused",
                }
            ],
        )

        result = notify_expiring_streaming_users()

        assert result["notified"] == 0
        assert result["not_streaming"] == 1
        fake_client.send_message.assert_not_called()


def test_notify_is_idempotent_within_window(app, session, monkeypatch):
    with app.app_context():
        server, user = _make_server_and_user(session, token="jf-user-1")

        fake_client = MagicMock()
        fake_client.send_message.return_value = True
        monkeypatch.setattr(
            expiry_notify, "get_client_for_media_server", lambda s: fake_client
        )
        monkeypatch.setattr(
            expiry_notify,
            "get_now_playing_all_servers",
            lambda: [
                {
                    "server_id": server.id,
                    "user_id": "jf-user-1",
                    "user_name": "alice",
                    "session_id": "sess-abc",
                    "state": "playing",
                }
            ],
        )

        first = notify_expiring_streaming_users()
        second = notify_expiring_streaming_users()

        assert first["notified"] == 1
        assert second["notified"] == 0
        assert second["already_notified"] == 1
        assert fake_client.send_message.call_count == 1


def test_notify_skips_unsupported_server(app, session, monkeypatch):
    with app.app_context():
        server, user = _make_server_and_user(
            session, token="p-user-1", server_type="plex"
        )

        monkeypatch.setattr(
            expiry_notify,
            "get_now_playing_all_servers",
            lambda: [
                {
                    "server_id": server.id,
                    "user_id": "p-user-1",
                    "user_name": "alice",
                    "session_id": "sess",
                    "state": "playing",
                }
            ],
        )

        result = notify_expiring_streaming_users()

        assert result["notified"] == 0
        assert result["unsupported"] == 1
