"""Tests for outbound email through Resend.

The value here is not "does requests.post get called" — it is the set of ways
this can fail quietly. A password reset that never arrives leaves the user
locked out with nobody notified, so every guard that stops a send from *looking*
successful is worth a test.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import ResendEmail, Settings, User
from app.services import resend_email as service


@pytest.fixture(autouse=True)
def _clean_resend_state(app):
    """Reset Resend settings and the send log around every test."""
    with app.app_context():
        ResendEmail.query.delete()
        Settings.query.filter(
            Settings.key.in_(
                [
                    service.SETTING_API_KEY,
                    service.SETTING_ENABLED,
                    service.SETTING_FROM,
                    service.SETTING_REPLY_TO,
                    service.SETTING_PUBLIC_URL,
                    service.SETTING_LAST_ERROR,
                ]
            )
        ).delete(synchronize_session=False)
        db.session.commit()
    yield
    with app.app_context():
        ResendEmail.query.delete()
        db.session.commit()


def _configure(*, enabled=True, sender="sauron <no-reply@example.com>"):
    service.set_setting(service.SETTING_API_KEY, "re_test_key")
    service.set_setting(service.SETTING_FROM, sender)
    service.set_setting(service.SETTING_PUBLIC_URL, "https://sauron.example.com")
    service.set_setting(service.SETTING_ENABLED, "true" if enabled else "false")
    db.session.commit()


def _response(status_code: int, payload: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = str(payload)
    return resp


# --------------------------------------------------------------------------
# Configuration guards
# --------------------------------------------------------------------------


def test_not_configured_without_from_address(app):
    """A key alone is not enough — Resend rejects a send with no sender."""
    with app.app_context():
        service.set_setting(service.SETTING_API_KEY, "re_test_key")
        db.session.commit()

        assert service.is_configured() is False
        assert service.is_enabled() is False


def test_enabled_requires_both_key_and_switch(app):
    with app.app_context():
        _configure(enabled=False)
        assert service.is_configured() is True
        assert service.is_enabled() is False

        service.set_setting(service.SETTING_ENABLED, "true")
        db.session.commit()
        assert service.is_enabled() is True


def test_send_refused_when_unconfigured_and_never_calls_resend(app):
    """No key means no HTTP call at all, and a logged failure."""
    with app.app_context(), patch("requests.post") as mock_post:
        result = service.send_email(
            to_address="user@example.com",
            subject="hi",
            html="<p>hi</p>",
            text="hi",
            kind=service.KIND_TEST,
        )

        mock_post.assert_not_called()
        assert result.ok is False
        assert result.error_code == "not_configured"
        assert ResendEmail.query.count() == 1
        assert ResendEmail.query.first().status == service.STATUS_FAILED


def test_sandbox_sender_is_detected(app):
    """resend.dev delivers only to the account owner — the tab must know."""
    with app.app_context():
        _configure(sender="Acme <onboarding@resend.dev>")
        assert service.uses_sandbox_sender() is True

        service.set_setting(service.SETTING_FROM, "sauron <no-reply@example.com>")
        db.session.commit()
        assert service.uses_sandbox_sender() is False


# --------------------------------------------------------------------------
# Send outcomes
# --------------------------------------------------------------------------


def test_successful_send_logs_resend_id(app):
    with app.app_context():
        _configure()
        with patch(
            "requests.post", return_value=_response(200, {"id": "abc-123"})
        ) as mock_post:
            result = service.send_email(
                to_address="user@example.com",
                subject="hi",
                html="<p>hi</p>",
                text="hi",
                kind=service.KIND_TEST,
            )

        assert result.ok is True
        assert result.resend_id == "abc-123"

        sent_payload = mock_post.call_args.kwargs["json"]
        # A plaintext part always ships: some clients render it instead of the
        # HTML, and a reset link only inside an <a href> is unreachable there.
        assert sent_payload["text"] == "hi"
        assert sent_payload["to"] == ["user@example.com"]

        row = ResendEmail.query.first()
        assert row.status == service.STATUS_SENT
        assert row.resend_id == "abc-123"


@pytest.mark.parametrize(
    "error_name",
    ["daily_quota_exceeded", "monthly_quota_exceeded", "rate_limit_exceeded"],
)
def test_429_family_is_logged_with_its_own_code(app, error_name):
    """The three 429s mean different things and need different operator advice."""
    with app.app_context():
        _configure()
        with patch(
            "requests.post",
            return_value=_response(429, {"name": error_name, "message": "nope"}),
        ):
            result = service.send_email(
                to_address="user@example.com",
                subject="hi",
                html="<p>hi</p>",
                text="hi",
                kind=service.KIND_TEST,
            )

        assert result.ok is False
        assert result.error_code == error_name
        assert ResendEmail.query.first().error_code == error_name

        # Each maps to distinct advice rather than one generic "try later".
        hint = service.describe_error(error_name)
        assert hint != service.describe_error("some_unmapped_error")


def test_network_failure_returns_result_instead_of_raising(app):
    """A web request must not 500 because Resend is unreachable."""
    import requests

    with app.app_context():
        _configure()
        with patch("requests.post", side_effect=requests.ConnectionError("boom")):
            result = service.send_email(
                to_address="user@example.com",
                subject="hi",
                html="<p>hi</p>",
                text="hi",
                kind=service.KIND_TEST,
            )

        assert result.ok is False
        assert result.error_code == "network_error"
        assert ResendEmail.query.count() == 1


def test_non_json_error_body_still_produces_a_message(app):
    """A proxy returning HTML must not crash the parser."""
    with app.app_context():
        _configure()
        resp = MagicMock()
        resp.status_code = 502
        resp.json.side_effect = ValueError("not json")
        resp.text = "<html>Bad Gateway</html>"

        with patch("requests.post", return_value=resp):
            result = service.send_email(
                to_address="user@example.com",
                subject="hi",
                html="<p>hi</p>",
                text="hi",
                kind=service.KIND_TEST,
            )

        assert result.ok is False
        assert "502" in (result.error_message or "")


# --------------------------------------------------------------------------
# Password reset delivery
# --------------------------------------------------------------------------


def _make_user(app, email="user@example.com"):
    user = User(
        token="tok-resend-test",
        username="resend-tester",
        email=email,
        code="CODE123456",
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_password_reset_email_contains_the_public_link(app):
    with app.app_context():
        _configure()
        user = _make_user(app)

        with patch(
            "requests.post", return_value=_response(200, {"id": "reset-1"})
        ) as mock_post:
            result = service.send_password_reset_email(user)

        assert result.ok is True
        payload = mock_post.call_args.kwargs["json"]
        # The configured public URL wins over the request host: behind a proxy
        # the host sauron sees is often unreachable from a recipient's inbox.
        assert "https://sauron.example.com/reset/" in payload["html"]
        assert "https://sauron.example.com/reset/" in payload["text"]

        row = ResendEmail.query.first()
        assert row.kind == service.KIND_PASSWORD_RESET
        assert row.user_id == user.id


def test_no_token_is_minted_when_sending_is_disabled(app):
    """Guards run before token creation.

    create_reset_token burns any existing unused token. Minting first and then
    discovering sending is off would leave the user with a dead old link and no
    new one — strictly worse than doing nothing.
    """
    from app.models import PasswordResetToken

    with app.app_context():
        _configure(enabled=False)
        user = _make_user(app, email="disabled@example.com")
        before = PasswordResetToken.query.filter_by(user_id=user.id).count()

        with patch("requests.post") as mock_post:
            result = service.send_password_reset_email(user)

        mock_post.assert_not_called()
        assert result.ok is False
        assert result.error_code == "not_enabled"
        assert PasswordResetToken.query.filter_by(user_id=user.id).count() == before


def test_user_without_email_is_refused_before_minting(app):
    from app.models import PasswordResetToken

    with app.app_context():
        _configure()
        user = _make_user(app, email=None)

        with patch("requests.post") as mock_post:
            result = service.send_password_reset_email(user)

        mock_post.assert_not_called()
        assert result.error_code == "no_email"
        assert PasswordResetToken.query.filter_by(user_id=user.id).count() == 0


def test_supplied_token_is_reused_not_replaced(app):
    """The admin modal passes the token it is already displaying.

    Minting a fresh one here would invalidate the link on the admin's screen
    while mailing the user a different one.
    """
    from app.services.password_reset import create_reset_token

    with app.app_context():
        _configure()
        user = _make_user(app, email="reuse@example.com")
        token = create_reset_token(user.id)

        with patch(
            "requests.post", return_value=_response(200, {"id": "reset-2"})
        ) as mock_post:
            result = service.send_password_reset_email(user, token=token)

        assert result.ok is True
        payload = mock_post.call_args.kwargs["json"]
        assert token.code in payload["text"]
        assert token.used is False


def test_missing_public_url_is_refused(app):
    """A reset link built on an empty base would point nowhere."""
    with app.app_context():
        _configure()
        service.set_setting(service.SETTING_PUBLIC_URL, None)
        db.session.commit()
        user = _make_user(app, email="nobase@example.com")

        with patch("requests.post") as mock_post:
            result = service.send_password_reset_email(user)

        mock_post.assert_not_called()
        assert result.error_code == "no_base_url"


# --------------------------------------------------------------------------
# Quota accounting
# --------------------------------------------------------------------------


def test_quota_counts_only_successful_sends(app):
    """A rejected request consumed no quota; counting it would mislead."""
    with app.app_context():
        _configure()

        with patch("requests.post", return_value=_response(200, {"id": "ok-1"})):
            service.send_email(
                to_address="a@example.com",
                subject="s",
                html="h",
                text="t",
                kind=service.KIND_TEST,
            )
        with patch(
            "requests.post",
            return_value=_response(400, {"name": "validation_error", "message": "no"}),
        ):
            service.send_email(
                to_address="b@example.com",
                subject="s",
                html="h",
                text="t",
                kind=service.KIND_TEST,
            )

        usage = service.quota_usage()
        assert usage["today"] == 1
        assert usage["month"] == 1
        assert usage["today_limit"] == service.FREE_TIER_DAILY_LIMIT
        assert usage["month_limit"] == service.FREE_TIER_MONTHLY_LIMIT


# --------------------------------------------------------------------------
# Key masking
# --------------------------------------------------------------------------


def test_masked_key_never_exposes_the_secret():
    masked = service.mask_api_key("re_abcdefghijklmnop")
    assert "abcdefghij" not in masked
    assert masked.endswith("mnop")
    assert masked.startswith("•")
    assert service.mask_api_key(None) == ""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
#
# These render the real templates. The point is not coverage — it is that the
# tab is the only place an operator can see the sandbox-domain warning, and a
# template that raises would hide it behind the blueprint's generic error path.


@pytest.fixture
def admin_client(app, client):
    """A client logged in as an admin, for the @login_required activity routes."""
    from app.models import AdminAccount

    with app.app_context():
        account = AdminAccount.query.filter_by(username="resend-admin").first()
        if account is None:
            account = AdminAccount(username="resend-admin")
            account.set_password("Password1")
            db.session.add(account)
            db.session.commit()
        account_id = account.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(account_id)
        sess["_fresh"] = True
    return client


def test_resend_tab_renders_empty_state_when_unconfigured(app, admin_client):
    response = admin_client.get("/activity/resend")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Email delivery is not connected yet" in body
    assert "Failed to load email delivery log" not in body


def test_resend_tab_warns_about_the_sandbox_sender(app, admin_client):
    """The state where sends work for the admin and fail for every real user."""
    with app.app_context():
        _configure(sender="Acme <onboarding@resend.dev>")

    response = admin_client.get("/activity/resend")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "shared onboarding domain" in body


def test_resend_grid_renders_the_log(app, admin_client):
    with app.app_context():
        _configure()
        with patch(
            "requests.post",
            return_value=_response(
                429, {"name": "daily_quota_exceeded", "message": "x"}
            ),
        ):
            service.send_email(
                to_address="quota@example.com",
                subject="s",
                html="h",
                text="t",
                kind=service.KIND_TEST,
            )

    response = admin_client.get("/activity/resend/grid")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "quota@example.com" in body
    # Resend's own error name is shown verbatim: a paraphrased provider error
    # is a support ticket nobody can answer.
    assert "daily_quota_exceeded" in body


def test_saving_a_masked_key_does_not_wipe_the_stored_one(app, admin_client):
    """The form renders the mask, so the browser posts bullets back on save."""
    with app.app_context():
        _configure()
        stored = service.get_setting(service.SETTING_API_KEY)

    admin_client.post(
        "/activity/resend/settings",
        data={
            "resend_api_key": service.mask_api_key(stored),
            "resend_from_address": "sauron <no-reply@example.com>",
            "resend_public_base_url": "https://sauron.example.com",
            "resend_enabled": "on",
        },
    )

    with app.app_context():
        assert service.get_setting(service.SETTING_API_KEY) == stored


def test_clearing_the_key_also_disables_sending(app, admin_client):
    """Otherwise sauron believes it can mail users with nothing to authenticate."""
    with app.app_context():
        _configure()

    admin_client.post(
        "/activity/resend/settings",
        data={
            "clear_api_key": "1",
            "resend_api_key": "•••••••• key",
            "resend_from_address": "sauron <no-reply@example.com>",
            "resend_enabled": "on",
        },
    )

    with app.app_context():
        assert service.get_setting(service.SETTING_API_KEY) is None
        assert service.get_setting(service.SETTING_ENABLED) == "false"
        assert service.is_enabled() is False


def test_configured_but_disabled_is_flagged_on_the_tab(app, admin_client):
    """The other "looks healthy, delivers nothing" state.

    A test send only needs a key, so it succeeds and paints the tab green while
    every real password reset refuses with not_enabled. Nothing else on screen
    would say so.
    """
    with app.app_context():
        _configure(enabled=False)

    response = admin_client.get("/activity/resend")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Outbound email is turned off" in body
    # The unconfigured empty state must not also fire — the key IS saved.
    assert "Email delivery is not connected yet" not in body


def test_saving_with_sending_off_is_a_warning_not_a_success(app, admin_client):
    response = admin_client.post(
        "/activity/resend/settings",
        data={
            "resend_api_key": "re_test_key",
            "resend_from_address": "sauron <no-reply@example.com>",
            "resend_public_base_url": "https://sauron.example.com",
            # "resend_enabled" deliberately absent — the unticked checkbox.
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "outbound email is turned off" in body
    assert 'Use "Send test" to verify your domain' not in body


def test_successful_test_send_still_warns_while_sending_is_off(app, admin_client):
    """A passing test proves the key and domain, not that resets will go out."""
    with app.app_context():
        _configure(enabled=False)

    with patch("requests.post", return_value=_response(200, {"id": "test-1"})):
        response = admin_client.post(
            "/activity/resend/test", data={"test_recipient": "me@example.com"}
        )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "still turned off" in body
