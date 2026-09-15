"""Expiry notice delivered through Moonbase's server messages.

A member whose membership ends within 24 hours gets a message in the Moonfin
app addressed to them alone — never to everyone. Once they renew it has to go
away: a week of "vence dentro de 1 día" after paying reads as a billing error.

Pinned against Moonbase 2.2.0 (Moonfin-Client/Plugin tag 2.2.0), the version on
the production server.
"""

import datetime
import hashlib
from datetime import UTC, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from app.extensions import db
from app.models import AdminAccount, ApiKey, MediaServer, User
from app.services import moonbase_expiry_notify as notices
from app.services.media.jellyfin import JellyfinClient

JF_USER_ID = "d47c230ba54d42bd874cdf0240530b5a"
END = datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC)


# ── Client: POST /Moonfin/Admin/Messages ────────────────────────────────────


def _jellyfin_client(response_json=None):
    client = JellyfinClient.__new__(JellyfinClient)  # skip __init__/Settings
    response = MagicMock()
    response.json.return_value = (
        response_json
        if response_json is not None
        else {"success": True, "item": {"Id": "abc123"}}
    )
    client.post = MagicMock(return_value=response)
    client.delete = MagicMock()
    return client


def _create(client, **overrides):
    kwargs = {
        "title": "Aviso",
        "body": "Hola",
        "target_user_id": JF_USER_ID,
        "end_utc": END,
        "delivery": "popup",
        "color": "white",
        "action_label": "Renovar",
        "action_url": "https://neexy.net/pay?renovar=1",
    }
    kwargs.update(overrides)
    return client.create_moonbase_message(**kwargs)


def _payload(client):
    return client.post.call_args.kwargs["json"]


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def test_create_message_is_addressed_to_the_given_user_only():
    client = _jellyfin_client()

    message_id = _create(client)

    assert message_id == "abc123"
    assert client.post.call_args.args[0] == "/Moonfin/Admin/Messages"
    payload = _payload(client)
    assert payload["Audience"] == "users"
    assert payload["TargetUserIds"] == [JF_USER_ID]


def test_create_message_shows_right_away_until_the_end_date():
    client = _jellyfin_client()

    _create(client)

    payload = _payload(client)
    assert payload["StartUtc"] is None
    assert payload["EndUtc"] == "2026-09-22T01:00:00Z"


def test_create_message_passes_the_display_options_through():
    client = _jellyfin_client()

    _create(client)

    payload = _payload(client)
    assert payload["Title"] == "Aviso"
    assert payload["Body"] == "Hola"
    assert payload["Delivery"] == "popup"
    assert payload["Color"] == "white"
    assert payload["ActionLabel"] == "Renovar"
    assert payload["ActionUrl"] == "https://neexy.net/pay?renovar=1"


def test_create_message_never_sends_an_id():
    """An Id Moonbase already knows replaces that message and skips the push."""
    client = _jellyfin_client()

    _create(client)

    assert "Id" not in _payload(client)


@pytest.mark.parametrize("target", ["", None])
def test_create_message_refuses_a_missing_target(target):
    """Audience "users" with no targets is a message nobody can see."""
    client = _jellyfin_client()

    with pytest.raises(ValueError):
        _create(client, target_user_id=target)

    client.post.assert_not_called()


def test_create_message_strips_characters_that_corrupt_moonbase_config():
    """Moonbase 2.2.0 writes these into its XML config, Jellyfin cannot read the
    file back, and every Moonbase setting is silently reset to defaults."""
    client = _jellyfin_client()

    _create(
        client,
        title="A\x1fviso",
        body="uno\x0bdos\x00tres\ud83d cuatro\n🍿\tfin",
        action_label="Reno\x08var",
    )

    payload = _payload(client)
    assert payload["Title"] == "Aviso"
    assert payload["Body"] == "unodostres cuatro\n🍿\tfin"
    assert payload["ActionLabel"] == "Renovar"


def test_create_message_refuses_text_longer_than_moonbase_keeps():
    """Moonbase truncates with Substring, which can split an emoji and leave a
    lone surrogate in the config. Refuse instead of letting it cut."""
    client = _jellyfin_client()

    _create(client, title="🔔" * 60)  # 120 UTF-16 units: exactly the cap

    with pytest.raises(ValueError):
        _create(client, title="🔔" * 61)
    with pytest.raises(ValueError):
        _create(client, body="x" * 2001)


@pytest.mark.parametrize("item", [{}, None, {"Id": ""}])
def test_create_message_raises_when_moonbase_returns_no_id(item):
    client = _jellyfin_client({"success": True, "item": item})

    with pytest.raises(ValueError):
        _create(client)


def test_delete_message_calls_the_admin_endpoint():
    client = _jellyfin_client()

    client.delete_moonbase_message("abc123")

    client.delete.assert_called_once_with("/Moonfin/Admin/Messages/abc123")


def test_delete_message_treats_an_already_gone_message_as_deleted():
    """Moonbase prunes a message once its end date passes, so a renewal after
    the week is up finds nothing left to delete."""
    client = _jellyfin_client()
    client.delete.side_effect = _http_error(404)

    client.delete_moonbase_message("abc123")  # does not raise


def test_delete_message_raises_on_other_failures():
    client = _jellyfin_client()
    client.delete.side_effect = _http_error(500)

    with pytest.raises(requests.HTTPError):
        client.delete_moonbase_message("abc123")


@pytest.mark.parametrize("message_id", ["", "../Plugins", "a/b"])
def test_delete_message_refuses_ids_that_are_not_a_single_path_segment(message_id):
    client = _jellyfin_client()

    with pytest.raises(ValueError):
        client.delete_moonbase_message(message_id)

    client.delete.assert_not_called()


def test_other_media_servers_have_no_moonbase_messages():
    from app.services.media.audiobookshelf import AudiobookshelfClient

    client = AudiobookshelfClient.__new__(AudiobookshelfClient)

    with pytest.raises(NotImplementedError):
        _create(client)
    with pytest.raises(NotImplementedError):
        client.delete_moonbase_message("abc123")


# ── The approved copy ───────────────────────────────────────────────────────


def test_the_notice_is_the_approved_copy():
    assert notices.NOTICE_TITLE == "🔔 AVISO DE VENCIMIENTO"
    assert notices.NOTICE_BODY == (
        "Hola 👋\n"
        "\n"
        "Tu membresía **vence dentro de 1 día**. ⏳\n"
        "\n"
        "Para evitar interrupciones en el servicio, puedes realizar tu "
        "renovación desde el siguiente enlace:\n"
        "\n"
        "👉 **Renovar mi membresía**\n"
        "\n"
        "🎬 ¡Gracias por seguir disfrutando de nuestro servicio! 🍿"
    )
    assert notices.NOTICE_ACTION_LABEL == "Renovar mi membresía"
    assert notices.NOTICE_ACTION_URL == "https://neexy.net/pay?renovar=1"
    assert notices.NOTICE_DELIVERY == "popup"
    assert notices.NOTICE_COLOR == "white"


def test_the_approved_copy_reaches_moonbase_unchanged():
    """Nothing in it is stripped or over the caps, so the member reads it as written."""
    client = _jellyfin_client()

    _create(
        client,
        title=notices.NOTICE_TITLE,
        body=notices.NOTICE_BODY,
        action_label=notices.NOTICE_ACTION_LABEL,
        action_url=notices.NOTICE_ACTION_URL,
    )

    payload = _payload(client)
    assert payload["Title"] == notices.NOTICE_TITLE
    assert payload["Body"] == notices.NOTICE_BODY
    assert payload["ActionLabel"] == notices.NOTICE_ACTION_LABEL


# ── Who gets the notice ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    monkeypatch.setattr(
        "app.services.notifications.notify", lambda *a, **k: None, raising=True
    )


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="admin-api-key",
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def moonbase(monkeypatch):
    """The Jellyfin client as the service sees it; hands out msg0, msg1, …"""
    client = MagicMock()
    counter = iter(range(1000))
    client.create_moonbase_message.side_effect = lambda **_: f"msg{next(counter)}"
    client.delete_moonbase_message.return_value = True
    monkeypatch.setattr(notices, "get_client_for_media_server", lambda _server: client)
    return client


def _in(delta):
    return (datetime.datetime.now(UTC) + delta).replace(tzinfo=None)


def _member(server, *, expires_in, token=JF_USER_ID, disabled=False, **extra):
    user = User(
        token=token,
        username=f"member-{token or 'none'}",
        email=f"{token or 'none'}@example.com",
        code="INVITE1",
        server_id=server.id,
        is_disabled=disabled,
        expires=None if expires_in is None else _in(expires_in),
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_sends_the_notice_to_a_member_with_less_than_a_day_left(
    jellyfin_server, moonbase
):
    user = _member(jellyfin_server, expires_in=timedelta(hours=23))

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["notified"] == 1
    kwargs = moonbase.create_moonbase_message.call_args.kwargs
    assert kwargs["target_user_id"] == JF_USER_ID
    assert kwargs["title"] == notices.NOTICE_TITLE
    assert kwargs["body"] == notices.NOTICE_BODY
    assert kwargs["action_label"] == notices.NOTICE_ACTION_LABEL
    assert kwargs["action_url"] == notices.NOTICE_ACTION_URL
    assert kwargs["delivery"] == "popup"
    assert kwargs["color"] == "white"
    seven_days = datetime.datetime.now(UTC) + timedelta(days=7)
    assert abs(kwargs["end_utc"] - seven_days) < timedelta(minutes=1)

    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg0"
    assert user.moonbase_notice_expires == user.expires


@pytest.mark.parametrize(
    "expires_in",
    [timedelta(hours=24, minutes=1), timedelta(days=3), timedelta(hours=-1), None],
    ids=["just-over-a-day", "three-days", "already-expired", "no-expiry"],
)
def test_leaves_alone_a_member_outside_the_last_day(
    jellyfin_server, moonbase, expires_in
):
    _member(jellyfin_server, expires_in=expires_in)

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["notified"] == 0
    moonbase.create_moonbase_message.assert_not_called()


def test_a_member_just_inside_the_last_day_is_notified(jellyfin_server, moonbase):
    _member(jellyfin_server, expires_in=timedelta(hours=23, minutes=59))

    assert notices.sync_moonbase_expiry_notices()["notified"] == 1


def test_notifies_each_expiry_only_once(jellyfin_server, moonbase):
    _member(jellyfin_server, expires_in=timedelta(hours=23))

    first = notices.sync_moonbase_expiry_notices()
    second = notices.sync_moonbase_expiry_notices()

    assert first["notified"] == 1
    assert second["notified"] == 0
    assert second["already_notified"] == 1
    assert moonbase.create_moonbase_message.call_count == 1


def test_one_message_per_member(jellyfin_server, moonbase):
    _member(jellyfin_server, expires_in=timedelta(hours=5), token="a" * 32)
    _member(jellyfin_server, expires_in=timedelta(hours=6), token="b" * 32)

    notices.sync_moonbase_expiry_notices()

    targets = [
        c.kwargs["target_user_id"]
        for c in moonbase.create_moonbase_message.call_args_list
    ]
    assert sorted(targets) == ["a" * 32, "b" * 32]


def test_skips_a_disabled_account(jellyfin_server, moonbase):
    """It cannot sign in to read the message."""
    _member(jellyfin_server, expires_in=timedelta(hours=5), disabled=True)

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["skipped"] == 1
    moonbase.create_moonbase_message.assert_not_called()


def test_skips_a_member_with_no_jellyfin_user_id(jellyfin_server, moonbase):
    _member(jellyfin_server, expires_in=timedelta(hours=5), token="")

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["skipped"] == 1
    moonbase.create_moonbase_message.assert_not_called()


def test_skips_servers_that_are_not_jellyfin(session, moonbase):
    server = MediaServer(
        name="Plex", server_type="plex", url="http://plex.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    _member(server, expires_in=timedelta(hours=5))

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["notified"] == 0
    moonbase.create_moonbase_message.assert_not_called()


def test_a_failed_send_is_retried_on_the_next_run(jellyfin_server, moonbase):
    user = _member(jellyfin_server, expires_in=timedelta(hours=5))
    moonbase.create_moonbase_message.side_effect = [RuntimeError("down"), "msg9"]

    first = notices.sync_moonbase_expiry_notices()
    db.session.refresh(user)
    assert first["errors"] == 1
    assert user.moonbase_notice_id is None

    second = notices.sync_moonbase_expiry_notices()
    db.session.refresh(user)
    assert second["notified"] == 1
    assert user.moonbase_notice_id == "msg9"


# ── Renewal takes the notice down ───────────────────────────────────────────


def _notified_member(server, *, notified_for, expires_in, **extra):
    """A member who got the notice for `notified_for` and now expires at `expires_in`."""
    return _member(
        server,
        expires_in=expires_in,
        moonbase_notice_id="msg-old",
        moonbase_notice_expires=_in(notified_for),
        **extra,
    )


def test_renewal_removes_the_notice(jellyfin_server, moonbase):
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(days=30)
    )

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["retracted"] == 1
    moonbase.delete_moonbase_message.assert_called_once_with("msg-old")
    moonbase.create_moonbase_message.assert_not_called()
    db.session.refresh(user)
    assert user.moonbase_notice_id is None
    assert user.moonbase_notice_expires is None


def test_removing_the_expiry_removes_the_notice(jellyfin_server, moonbase):
    _notified_member(jellyfin_server, notified_for=timedelta(hours=5), expires_in=None)

    assert notices.sync_moonbase_expiry_notices()["retracted"] == 1
    moonbase.delete_moonbase_message.assert_called_once_with("msg-old")


def test_a_short_renewal_still_inside_the_last_day_gets_a_fresh_notice(
    jellyfin_server, moonbase
):
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=2), expires_in=timedelta(hours=20)
    )

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["retracted"] == 1
    assert summary["notified"] == 1
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg0"
    assert user.moonbase_notice_expires == user.expires


def test_moving_the_expiry_earlier_keeps_the_notice(jellyfin_server, moonbase):
    """Not a renewal, and the notice still holds: they expire within the day."""
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=23), expires_in=timedelta(hours=5)
    )

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["retracted"] == 0
    assert summary["already_notified"] == 1
    moonbase.delete_moonbase_message.assert_not_called()
    moonbase.create_moonbase_message.assert_not_called()
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg-old"


def test_a_failed_removal_is_retried_on_the_next_run(jellyfin_server, moonbase):
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(days=30)
    )
    moonbase.delete_moonbase_message.side_effect = [RuntimeError("down"), True]

    first = notices.sync_moonbase_expiry_notices()
    db.session.refresh(user)
    assert first["errors"] == 1
    assert user.moonbase_notice_id == "msg-old"

    second = notices.sync_moonbase_expiry_notices()
    db.session.refresh(user)
    assert second["retracted"] == 1
    assert user.moonbase_notice_id is None


def test_removal_also_runs_for_an_account_that_was_disabled(jellyfin_server, moonbase):
    """A lapsed member renewing late still has the old notice on file."""
    _notified_member(
        jellyfin_server,
        notified_for=timedelta(days=-2),
        expires_in=timedelta(days=30),
        disabled=True,
    )

    assert notices.sync_moonbase_expiry_notices()["retracted"] == 1


# ── The renewal endpoints take it down immediately ──────────────────────────


@pytest.fixture
def api_headers(session):
    """A working API key. The route hashes what it is given, so seed the hash."""
    admin = AdminAccount(username="moonbase-admin")
    admin.set_password("irrelevant-for-this-test")
    db.session.add(admin)
    db.session.commit()

    raw = "test-api-key-moonbase"
    db.session.add(
        ApiKey(
            name="test",
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_by_id=admin.id,
            is_active=True,
        )
    )
    db.session.commit()
    return {"X-API-Key": raw, "Content-Type": "application/json"}


def test_extend_removes_the_notice_right_away(
    client, api_headers, jellyfin_server, moonbase
):
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(hours=5)
    )

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    moonbase.delete_moonbase_message.assert_called_once_with("msg-old")
    db.session.refresh(user)
    assert user.moonbase_notice_id is None


def test_update_expiry_to_a_later_date_removes_the_notice(
    client, api_headers, jellyfin_server, moonbase
):
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(hours=5)
    )
    later = (datetime.datetime.now(UTC) + timedelta(days=30)).isoformat()

    response = client.put(
        f"/api/users/{user.id}/update-expiry",
        json={"expires": later},
        headers=api_headers,
    )

    assert response.status_code == 200
    moonbase.delete_moonbase_message.assert_called_once_with("msg-old")
    db.session.refresh(user)
    assert user.moonbase_notice_id is None


def test_update_expiry_to_an_earlier_date_keeps_the_notice(
    client, api_headers, jellyfin_server, moonbase
):
    user = _notified_member(
        jellyfin_server,
        notified_for=timedelta(hours=20),
        expires_in=timedelta(hours=20),
    )
    earlier = (datetime.datetime.now(UTC) + timedelta(hours=2)).isoformat()

    response = client.put(
        f"/api/users/{user.id}/update-expiry",
        json={"expires": earlier},
        headers=api_headers,
    )

    assert response.status_code == 200
    moonbase.delete_moonbase_message.assert_not_called()
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg-old"


def test_a_renewal_still_succeeds_when_moonbase_is_down(
    client, api_headers, jellyfin_server, moonbase
):
    """The buyer paid; the scheduled sync retries the removal."""
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(hours=5)
    )
    moonbase.delete_moonbase_message.side_effect = RuntimeError("down")

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg-old"


def test_a_renewal_still_succeeds_if_the_notice_check_itself_breaks(
    client, api_headers, jellyfin_server, moonbase, monkeypatch
):
    """The hook sits inside /extend's broad except, which answers 500."""
    user = _notified_member(
        jellyfin_server, notified_for=timedelta(hours=5), expires_in=timedelta(hours=5)
    )

    def broken(_user):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notices, "_renewed", broken)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    assert response.json["new_expiry"]


def test_a_notice_sent_but_not_recorded_counts_as_an_error(
    jellyfin_server, moonbase, monkeypatch
):
    """Pins today's behaviour: Moonbase has the message and sauron has no record
    of it, so the next run sends a second one. Unlikely with one SQLite writer,
    and the log names the orphaned message id."""
    user = _member(jellyfin_server, expires_in=timedelta(hours=5))

    def failing_commit():
        db.session.rollback()
        return False

    monkeypatch.setattr(notices, "_commit", failing_commit)

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["errors"] == 1
    assert summary["notified"] == 0
    moonbase.create_moonbase_message.assert_called_once()
    db.session.refresh(user)
    assert user.moonbase_notice_id is None


# ── The scheduled task ──────────────────────────────────────────────────────


def test_the_scheduled_task_runs_the_sync(app, jellyfin_server, moonbase):
    """The entry point production actually fires."""
    from app.tasks.maintenance import sync_moonbase_notices

    user = _member(jellyfin_server, expires_in=timedelta(hours=5))

    sync_moonbase_notices(app)

    moonbase.create_moonbase_message.assert_called_once()
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg0"


def test_the_scheduled_task_survives_a_failing_sync(app, session, monkeypatch):
    from app.tasks.maintenance import sync_moonbase_notices

    def boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr(notices, "sync_moonbase_expiry_notices", boom)

    sync_moonbase_notices(app)  # does not raise
