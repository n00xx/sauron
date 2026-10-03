"""The Moonbase notice for a member already on the renewal screen.

The last-day notice says "vence dentro de 1 día", which is wrong the moment the
membership lapses. A restricted member can still sign in, so Moonfin shows them
a Messages entry with an unread badge; it now says the membership ENDED, with
the same renewal button (a QR code on the TV).

A plainly disabled member gets nothing: they cannot sign in to read it.
"""

import datetime
import json
from datetime import UTC, timedelta
from unittest.mock import MagicMock

import pytest

from app.extensions import db
from app.models import MediaServer, User
from app.services import moonbase_expiry_notify as notices
from app.services.expiry import RENEWAL_URL

JF_USER_ID = "jf-lapsed-1"


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    return server


@pytest.fixture
def moonbase(monkeypatch):
    client = MagicMock()
    counter = iter(range(1000))
    client.create_moonbase_message.side_effect = lambda **_: f"msg{next(counter)}"
    monkeypatch.setattr(notices, "get_client_for_media_server", lambda _server: client)
    return client


def _in(delta):
    return (datetime.datetime.now(UTC) + delta).replace(tzinfo=None)


def _lapsed(server, *, restricted=True, **extra):
    user = User(
        token=JF_USER_ID,
        username="lapsed",
        email="lapsed@example.com",
        code="INVITE1",
        server_id=server.id,
        is_disabled=True,
        expires=_in(-timedelta(hours=2)),
        restricted_policy=json.dumps({"EnabledFolders": ["pelis"]})
        if restricted
        else None,
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_a_member_on_the_renewal_screen_is_told_the_membership_ended(
    jellyfin_server, moonbase
):
    user = _lapsed(jellyfin_server)

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["lapsed"] == 1
    kwargs = moonbase.create_moonbase_message.call_args.kwargs
    assert kwargs["target_user_id"] == JF_USER_ID
    assert kwargs["title"] == notices.LAPSED_TITLE
    assert kwargs["body"] == notices.LAPSED_BODY
    assert kwargs["action_url"] == RENEWAL_URL
    assert kwargs["delivery"] == "inbox"
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg0"
    assert user.moonbase_notice_lapsed is True
    assert user.moonbase_notice_expires == user.expires


def test_the_copy_says_it_ended_not_that_it_is_ending():
    assert "venció" in notices.LAPSED_BODY
    assert "vence dentro" not in notices.LAPSED_BODY


def test_the_last_day_notice_is_replaced_not_stacked(jellyfin_server, moonbase):
    user = _lapsed(jellyfin_server)
    user.moonbase_notice_id = "last-day"
    user.moonbase_notice_expires = user.expires
    db.session.commit()

    notices.sync_moonbase_expiry_notices()

    moonbase.delete_moonbase_message.assert_called_once_with("last-day")
    db.session.refresh(user)
    assert user.moonbase_notice_id == "msg0"
    assert user.moonbase_notice_lapsed is True


def test_the_ended_notice_is_sent_once(jellyfin_server, moonbase):
    _lapsed(jellyfin_server)

    notices.sync_moonbase_expiry_notices()
    notices.sync_moonbase_expiry_notices()

    assert moonbase.create_moonbase_message.call_count == 1


def test_a_plainly_disabled_member_gets_no_notice(jellyfin_server, moonbase):
    _lapsed(jellyfin_server, restricted=False)

    notices.sync_moonbase_expiry_notices()

    moonbase.create_moonbase_message.assert_not_called()


def test_renewing_takes_the_ended_notice_down(jellyfin_server, moonbase):
    user = _lapsed(jellyfin_server)
    notices.sync_moonbase_expiry_notices()

    user.expires = _in(timedelta(days=30))
    user.is_disabled = False
    user.restricted_policy = None
    db.session.commit()
    notices.retract_notice_if_renewed(user)

    moonbase.delete_moonbase_message.assert_called_once_with("msg0")
    db.session.refresh(user)
    assert user.moonbase_notice_id is None
    assert user.moonbase_notice_lapsed is False


def test_a_refused_ended_notice_is_counted_and_retried(jellyfin_server, moonbase):
    moonbase.create_moonbase_message.side_effect = RuntimeError("down")
    user = _lapsed(jellyfin_server)

    summary = notices.sync_moonbase_expiry_notices()

    assert summary["errors"] == 1
    db.session.refresh(user)
    assert user.moonbase_notice_id is None
    assert user.moonbase_notice_lapsed is False
