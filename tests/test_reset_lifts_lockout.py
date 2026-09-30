"""Resetting the password also lifts a failed-login lockout.

Jellyfin's lockout is not timed: after LoginAttemptsBeforeLockout wrong
passwords it sets IsDisabled on the account and leaves it there until an admin
clears it. Before this, "Recuperar contraseña" changed the password of such an
account and left it unable to sign in anyway.

The reset is the right place to undo it: whoever holds the token proved they own
the account's inbox, and the next sign-in with the new password zeroes Jellyfin's
failed-login counter.

What it must NOT undo is a disable sauron did on purpose. Sauron records those in
``user.is_disabled`` (the expiry sweep, the admin's disable button), so a lockout
is recognisable as "Jellyfin says disabled, sauron says active and unexpired".
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from app.extensions import db
from app.models import MediaServer, User
from app.services.lockout import lift_lockout
from app.services.password_reset import create_reset_token, use_reset_token


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


def _user(session, server, *, is_disabled=False, expires=None):
    user = User(
        username="juanperez1",
        email="juan@example.com",
        token="jf-id-1",
        code="X",
        server_id=server.id,
        is_disabled=is_disabled,
        expires=expires,
    )
    session.add(user)
    session.commit()
    return user


def _media(disabled):
    """A media client reporting the live IsDisabled flag as `disabled`."""
    media = Mock()
    media.is_user_disabled.return_value = disabled
    media.enable_user.return_value = True
    return media


def _patched(media):
    return patch("app.services.lockout.get_client_for_media_server", return_value=media)


# ─── lift_lockout ───────────────────────────────────────────────────────────


def test_a_locked_out_account_is_re_enabled(app, session, jellyfin):
    user = _user(session, jellyfin, expires=datetime.now(UTC) + timedelta(days=20))
    media = _media(disabled=True)

    with _patched(media):
        assert lift_lockout(user) is True

    media.enable_user.assert_called_once_with("jf-id-1")


def test_an_account_without_an_expiry_counts_as_active(app, session, jellyfin):
    user = _user(session, jellyfin, expires=None)
    media = _media(disabled=True)

    with _patched(media):
        assert lift_lockout(user) is True


def test_an_account_sauron_disabled_is_left_alone(app, session, jellyfin):
    """Expired and swept, or switched off by the admin: renewal's job, not this."""
    user = _user(session, jellyfin, is_disabled=True)
    media = _media(disabled=True)

    with _patched(media):
        assert lift_lockout(user) is False

    media.enable_user.assert_not_called()


def test_an_expired_account_the_sweep_has_not_reached_yet_is_left_alone(
    app, session, jellyfin
):
    user = _user(session, jellyfin, expires=datetime.now(UTC) - timedelta(minutes=5))
    media = _media(disabled=True)

    with _patched(media):
        assert lift_lockout(user) is False

    media.enable_user.assert_not_called()


def test_an_account_that_is_not_locked_is_not_touched(app, session, jellyfin):
    user = _user(session, jellyfin)
    media = _media(disabled=False)

    with _patched(media):
        assert lift_lockout(user) is False

    media.enable_user.assert_not_called()


def test_it_does_not_act_when_jellyfin_cannot_say(app, session, jellyfin):
    """None is "unknown", never "disabled": no enabling on a guess."""
    user = _user(session, jellyfin)
    media = _media(disabled=None)

    with _patched(media):
        assert lift_lockout(user) is False

    media.enable_user.assert_not_called()


def test_servers_without_a_lockout_are_skipped(app, session):
    plex = MediaServer(
        name="Plex", server_type="plex", url="http://plex.local", api_key="k"
    )
    session.add(plex)
    session.commit()
    user = _user(session, plex)
    media = _media(disabled=True)

    with _patched(media) as factory:
        assert lift_lockout(user) is False

    factory.assert_not_called()


def test_a_failed_enable_reports_false(app, session, jellyfin):
    user = _user(session, jellyfin)
    media = _media(disabled=True)
    media.enable_user.return_value = False

    with _patched(media):
        assert lift_lockout(user) is False


def test_sauron_keeps_recording_the_account_as_active(app, session, jellyfin):
    """The lockout was never sauron's; its record must not start saying otherwise."""
    user = _user(session, jellyfin)

    with _patched(_media(disabled=True)):
        lift_lockout(user)

    db.session.refresh(user)
    assert user.is_disabled is False


# ─── The reset itself ───────────────────────────────────────────────────────


def _reset(user, *, password_changed=True, lift=None):
    token = create_reset_token(user.id)
    with (
        patch(
            "app.services.media.service.reset_user_password",
            return_value=password_changed,
        ),
        patch("app.services.password_reset.lift_lockout", **(lift or {})) as lifted,
    ):
        ok, msg = use_reset_token(token.code, "NuevaClave123")
    return ok, msg, lifted


def test_a_successful_reset_lifts_the_lockout(app, session, jellyfin):
    user = _user(session, jellyfin)

    ok, _msg, lifted = _reset(user, lift={"return_value": True})

    assert ok is True
    lifted.assert_called_once()
    assert lifted.call_args.args[0].id == user.id


def test_a_failed_reset_does_not_touch_the_account(app, session, jellyfin):
    user = _user(session, jellyfin)

    ok, _msg, lifted = _reset(user, password_changed=False)

    assert ok is False
    lifted.assert_not_called()


def test_the_new_password_stands_even_if_lifting_the_lockout_blows_up(
    app, session, jellyfin
):
    """The password did change; reporting failure would send them to reset again."""
    user = _user(session, jellyfin)

    ok, _msg, _lifted = _reset(user, lift={"side_effect": RuntimeError("down")})

    assert ok is True
