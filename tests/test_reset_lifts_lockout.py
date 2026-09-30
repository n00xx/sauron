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


# ─── A "Validar cuenta" in between must not hide the lockout ────────────────
#
# The renewal page's credential check answers a locked-out account with 403 and,
# having learned that Jellyfin has it disabled, records ``is_disabled = True``.
# Without more, the reset would then read that as a disable sauron made on
# purpose and leave the account locked for good — the customer tried to renew
# before trying to recover, and that was enough to strand them.


OLD_PASSWORD = "ClaveVieja1"


class _LockedJellyfin:
    """Jellyfin's lockout semantics, as far as these flows touch them."""

    def __init__(self, disabled=True):
        self.disabled = disabled
        self.password = OLD_PASSWORD

    def is_user_disabled(self, user_id):
        return self.disabled

    def enable_user(self, user_id):
        self.disabled = False
        return True

    def disable_user(self, user_id):
        self.disabled = True
        return True

    def authenticate_user(self, username, password):
        # A disabled account is refused before the password is looked at.
        if self.disabled:
            return False, 403, None
        if password != self.password:
            return False, 401, None
        return True, 200, "token"

    def logout_token(self, token):
        pass


def _everywhere(fake):
    """Every module in these flows builds its client through its own import."""
    return (
        patch(
            "app.services.credentials.get_client_for_media_server", return_value=fake
        ),
        patch("app.services.lockout.get_client_for_media_server", return_value=fake),
        patch(
            "app.services.media.service.get_client_for_media_server", return_value=fake
        ),
    )


def test_a_failed_validar_cuenta_does_not_stop_the_reset_from_unlocking(
    app, session, jellyfin
):
    from app.services.credentials import verify_media_credentials

    user = _user(session, jellyfin, expires=datetime.now(UTC) + timedelta(days=20))
    fake = _LockedJellyfin(disabled=True)
    a, b, c = _everywhere(fake)

    with a, b, c:
        # Locked out, the customer tries to renew first — even with the right
        # password Jellyfin refuses a disabled account, so the check fails.
        assert verify_media_credentials("juanperez1", OLD_PASSWORD) is None
        db.session.refresh(user)
        assert user.is_disabled is True, "the check still learns the real state"
        assert user.disabled_externally is True

        token = create_reset_token(user.id)
        with patch("app.services.media.service.reset_user_password", return_value=True):
            ok, _msg = use_reset_token(token.code, "NuevaClave123")

    assert ok is True
    assert fake.disabled is False, "the reset left the account locked"
    db.session.refresh(user)
    # Back to what it was before the check: active, and for the expiry sweep to
    # pick up when its time comes.
    assert user.is_disabled is False
    assert user.disabled_externally is False


def test_a_learned_disable_on_an_expired_account_stays(app, session, jellyfin):
    user = _user(
        session,
        jellyfin,
        is_disabled=True,
        expires=datetime.now(UTC) - timedelta(days=1),
    )
    user.disabled_externally = True
    session.commit()
    media = _media(disabled=True)

    with _patched(media):
        assert lift_lockout(user) is False

    media.enable_user.assert_not_called()


def test_a_disable_sauron_made_itself_is_not_mistaken_for_a_learned_one(
    app, session, jellyfin
):
    """The admin's button or the sweep: those go through sauron and clear the mark."""
    from app.services.media.service import disable_user

    user = _user(session, jellyfin)
    user.is_disabled = True
    user.disabled_externally = True  # learned earlier, by a credential check
    session.commit()
    fake = _LockedJellyfin(disabled=False)

    with patch(
        "app.services.media.service.get_client_for_media_server", return_value=fake
    ):
        assert disable_user(user.id) is True

    db.session.refresh(user)
    assert user.is_disabled is True
    assert user.disabled_externally is False

    with _patched(_media(disabled=True)) as factory:
        assert lift_lockout(user) is False
    factory.assert_not_called()


def test_enabling_through_sauron_clears_the_mark_too(app, session, jellyfin):
    """A renewal reactivates through the same path."""
    from app.services.media.service import enable_user

    user = _user(session, jellyfin)
    user.is_disabled = True
    user.disabled_externally = True
    session.commit()

    with patch(
        "app.services.media.service.get_client_for_media_server",
        return_value=_LockedJellyfin(disabled=True),
    ):
        assert enable_user(user.id) is True

    db.session.refresh(user)
    assert user.is_disabled is False
    assert user.disabled_externally is False


# ─── Migration: additive, and it must not touch the user's children ─────────


def test_migration_adds_the_column_and_keeps_every_child_row():
    import os
    import sqlite3
    import tempfile

    from flask_migrate import upgrade

    from app import create_app
    from app.config import BaseConfig

    class _Config(BaseConfig):
        TESTING = True
        WTF_CSRF_ENABLED = False

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        app = create_app(_Config)  # type: ignore[arg-type]
        with app.app_context():
            upgrade(revision="20260929_bound_email")

        conn = sqlite3.connect(path)
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO user (token, username, code, email, is_disabled) "
            "VALUES ('jf-1', 'juanperez1', 'X', 'juan@example.com', 1)"
        )
        # A child with ON DELETE CASCADE: a table rebuild would wipe it.
        conn.execute(
            "INSERT INTO password_reset_token (code, user_id, created_at, expires_at, used) "
            "VALUES ('ABCDEFGHIJ', 1, '2026-09-30 00:00:00', '2026-10-01 00:00:00', 0)"
        )
        conn.close()

        with app.app_context():
            upgrade()
            upgrade()  # re-running is a no-op

        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute('PRAGMA table_info("user")')]
        assert "disabled_externally" in cols
        assert conn.execute(
            'SELECT is_disabled, disabled_externally FROM "user"'
        ).fetchone() == (1, 0), "existing disables are sauron's, not learned"
        assert (
            conn.execute("SELECT count(*) FROM password_reset_token").fetchone()[0] == 1
        )
        conn.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)
