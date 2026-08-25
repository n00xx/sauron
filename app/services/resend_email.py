"""Send transactional email through Resend (https://resend.com).

Why Resend and not SMTP: sauron is self-hosted on a residential-ish box. Mail
sent straight from that IP lands in spam or is refused outright, and a password
reset that silently never arrives is worse than no reset link at all. Resend
signs with the operator's own verified domain (DKIM/SPF/DMARC) and reports a
hard failure at send time, which is exactly the signal this needs.

Called over plain ``requests`` rather than the ``resend`` SDK, matching
``app.services.stripe_events``: one POST to one endpoint does not justify a
dependency, and it keeps the failure surface (status code + error ``name``)
visible instead of wrapped in SDK exceptions.

THE FREE TIER'S REAL CONSTRAINT IS NOT THE QUOTA.
Resend only sends from a domain the account owns AND has verified — one domain
on the free plan. Until that verification completes, the only usable sender is
``onboarding@resend.dev``, and it delivers ONLY to the Resend account owner's
own address; every other recipient is rejected. So an operator who configures a
key, saves, and walks away has a working-looking tab that cannot mail a single
user. That is why ``send_test_email`` exists and why the tab pushes it: a test
send is the only thing that proves the domain is actually verified.

Quotas (free): 3.000 emails/month, 100/day. Resend exposes no quota endpoint,
so usage is counted from sauron's own ``resend_email`` log; the authoritative
answer only ever arrives as a 429.

Nothing here polls delivery status. ``GET /emails/{id}`` returns
``restricted_api_key`` (401) for a send-only key, and free-tier data is dropped
after 30 days — the send-time result is the honest, durable picture.
"""

from __future__ import annotations

import html as html_escape
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
import structlog

from app.extensions import db
from app.models import ResendEmail, Settings, User

logger = structlog.get_logger(__name__)

RESEND_API_BASE = "https://api.resend.com"

# Resend's send endpoint answers in well under a second; a longer wait here just
# holds a request thread while an admin stares at a spinner.
REQUEST_TIMEOUT = 15

# Free-plan caps, used only to render "X of Y used" in the tab. They are NOT
# enforced locally — the log can undercount (a send from another app on the same
# Resend key never touches this table), so refusing a password reset because a
# local counter says 100 would break a flow Resend would have accepted.
FREE_TIER_DAILY_LIMIT = 100
FREE_TIER_MONTHLY_LIMIT = 3000

# What the tab lists. S105 is suppressed below because this is a log label
# naming what the email was for, not a credential.
KIND_PASSWORD_RESET = "password_reset"  # noqa: S105
KIND_TEST = "test"

STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# Settings keys.
SETTING_API_KEY = "resend_api_key"
SETTING_ENABLED = "resend_enabled"
SETTING_FROM = "resend_from_address"
SETTING_REPLY_TO = "resend_reply_to"
SETTING_PUBLIC_URL = "resend_public_base_url"
SETTING_LAST_ERROR = "resend_last_error"

# Sender Resend hands every new account. Works without domain verification but
# only delivers to the account owner — useful to prove the key, useless for real
# users. The tab says so rather than letting it look production-ready.
SANDBOX_FROM_DOMAIN = "resend.dev"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    row = Settings.query.filter_by(key=key).first()
    if row is None or row.value is None or row.value == "":
        return default
    return row.value


def set_setting(key: str, value: str | None) -> None:
    row = Settings.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Settings(key=key, value=value))
    else:
        row.value = value


def is_configured() -> bool:
    """A key and a sender — both are required before anything can be sent."""
    return bool(get_setting(SETTING_API_KEY)) and bool(get_setting(SETTING_FROM))


def is_enabled() -> bool:
    """Sending happens only when explicitly switched on AND configured.

    Two conditions rather than one so an operator can pause all outbound mail
    (say, while a domain re-verifies) without deleting the key and losing it.
    """
    return is_configured() and get_setting(SETTING_ENABLED, "false") == "true"


def mask_api_key(api_key: str | None) -> str:
    """Render a key for display without ever putting it back in the DOM.

    The form posts this masked value back when untouched, which is why
    ``resend_settings`` refuses to store anything starting with the bullet.
    """
    if not api_key:
        return ""
    return "•" * 8 + api_key[-4:] if len(api_key) > 4 else "•" * 8


def uses_sandbox_sender() -> bool:
    """True when the configured sender is Resend's shared onboarding domain.

    Worth its own flag: this is the state where sends succeed for the operator
    and fail for everyone else, which reads as "working" on every screen that
    only checks for errors.
    """
    sender = get_setting(SETTING_FROM) or ""
    return SANDBOX_FROM_DOMAIN in sender.lower()


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send attempt.

    Frozen because callers log it, render it, and pass it on — a result that
    can be edited downstream is a result you cannot trust in the log.
    """

    ok: bool
    resend_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def status(self) -> str:
        return STATUS_SENT if self.ok else STATUS_FAILED


# Resend's error `name` → what an operator should actually do about it. Resend's
# own messages describe the API; these describe the fix in sauron's terms.
_ERROR_HINTS: dict[str, str] = {
    "missing_api_key": (
        "No API key was sent. Save a Resend API key below before sending."
    ),
    "invalid_api_key": (
        "Resend rejected the API key. Generate a new one at resend.com/api-keys "
        "and save it below."
    ),
    "restricted_api_key": (
        "This API key is restricted and cannot perform this action. A "
        "sending-only key is enough for password resets."
    ),
    "suspended_api_key": (
        "Resend has suspended this API key. Contact Resend support before "
        "sending again."
    ),
    "validation_error": (
        "Resend rejected the request. The most common cause is a 'From' "
        "address on a domain that is not verified in Resend."
    ),
    "not_found": (
        "Resend could not find the resource. Check the 'From' domain is added "
        "and verified in Resend."
    ),
    "daily_quota_exceeded": (
        "The free tier's 100 emails/day limit has been reached. Sending resumes "
        "automatically within 24 hours."
    ),
    "monthly_quota_exceeded": (
        "The free tier's 3,000 emails/month limit has been reached. Sending "
        "resumes when the monthly window rolls over."
    ),
    "rate_limit_exceeded": (
        "Too many requests to Resend in a short window. Wait a moment and retry."
    ),
    "application_error": "Resend hit an internal error. Retry in a few minutes.",
    "internal_server_error": "Resend hit an internal error. Retry in a few minutes.",
}


def describe_error(error_code: str | None, fallback: str | None = None) -> str:
    """Operator-facing sentence for a Resend error name.

    Falls back to Resend's own message rather than a generic string: an
    unmapped error the operator can read beats a tidy one they cannot act on.
    """
    if error_code and error_code in _ERROR_HINTS:
        return _ERROR_HINTS[error_code]
    return fallback or "Resend rejected the request."


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def _record(
    *,
    to_address: str,
    subject: str,
    kind: str,
    result: SendResult,
    user_id: int | None = None,
) -> None:
    """Write the attempt to the log. Never raises.

    A logging failure must not turn a delivered email into a reported failure,
    nor abort the caller's transaction — hence the rollback-and-continue.
    """
    try:
        db.session.add(
            ResendEmail(
                to_address=to_address,
                subject=subject,
                kind=kind,
                status=result.status,
                resend_id=result.resend_id,
                error_code=result.error_code,
                error_message=result.error_message,
                user_id=user_id,
                created_at=datetime.now(UTC),
            )
        )
        set_setting(SETTING_LAST_ERROR, None if result.ok else result.error_message)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to log Resend send: %s", exc, exc_info=True)


def _parse_error(response: requests.Response) -> tuple[str | None, str]:
    """Pull ``(name, message)` out of a Resend error body.

    Resend answers errors as ``{"name": ..., "message": ..., "statusCode": ...}``
    but an edge (a proxy, a 502) can return HTML, so the body is treated as
    untrusted and the status code is always kept as the last resort.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        name = body.get("name") or body.get("error")
        message = body.get("message") or response.text[:500]
        return (
            str(name) if name else None,
            str(message) if message else f"HTTP {response.status_code}",
        )
    return None, f"HTTP {response.status_code}: {response.text[:500]}"


def send_email(
    *,
    to_address: str,
    subject: str,
    html: str,
    text: str,
    kind: str,
    user_id: int | None = None,
) -> SendResult:
    """POST one email to Resend and log the outcome.

    Every failure path returns a ``SendResult`` rather than raising: the callers
    are a web request and a future public "forgot my password" form, and neither
    should 500 because a third party is having a bad day.
    """
    api_key = get_setting(SETTING_API_KEY)
    sender = get_setting(SETTING_FROM)

    if not api_key or not sender:
        result = SendResult(
            ok=False,
            error_code="not_configured",
            error_message=(
                "Resend is not configured: an API key and a 'From' address are "
                "both required."
            ),
        )
        _record(
            to_address=to_address,
            subject=subject,
            kind=kind,
            result=result,
            user_id=user_id,
        )
        return result

    payload: dict[str, Any] = {
        "from": sender,
        "to": [to_address],
        "subject": subject,
        "html": html,
        # Always send a plaintext part. Some clients render it instead of the
        # HTML, and a reset link that only exists inside a <a href> is a link
        # those users cannot follow.
        "text": text,
    }

    reply_to = get_setting(SETTING_REPLY_TO)
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        response = requests.post(
            f"{RESEND_API_BASE}/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        result = SendResult(
            ok=False,
            error_code="network_error",
            error_message=f"Could not reach Resend: {exc}",
        )
        logger.error("Resend request failed: %s", exc)
        _record(
            to_address=to_address,
            subject=subject,
            kind=kind,
            result=result,
            user_id=user_id,
        )
        return result

    if response.status_code >= 400:
        code, message = _parse_error(response)
        result = SendResult(ok=False, error_code=code, error_message=message)
        logger.warning(
            "Resend rejected send: status=%s code=%s message=%s",
            response.status_code,
            code,
            message,
        )
        _record(
            to_address=to_address,
            subject=subject,
            kind=kind,
            result=result,
            user_id=user_id,
        )
        return result

    try:
        body = response.json()
        resend_id = body.get("id") if isinstance(body, dict) else None
    except ValueError:
        resend_id = None

    result = SendResult(ok=True, resend_id=resend_id)
    logger.info("Resend accepted email id=%s kind=%s", resend_id, kind)
    _record(
        to_address=to_address,
        subject=subject,
        kind=kind,
        result=result,
        user_id=user_id,
    )
    return result


# --------------------------------------------------------------------------
# Message bodies
# --------------------------------------------------------------------------


def _public_base_url(fallback: str | None = None) -> str:
    """Base URL to build the reset link on.

    The stored setting wins over the request's own host because sauron sits
    behind a reverse proxy: ``request.url_root`` there can be an internal
    address, which produces a link that works for the admin who generated it
    and for nobody who receives it.
    """
    configured = get_setting(SETTING_PUBLIC_URL)
    base = configured or fallback or ""
    return base.rstrip("/")


def _reset_email_bodies(
    username: str, reset_url: str, expires_at: str
) -> tuple[str, str]:
    """Return ``(html, text)`` for the password reset email.

    Built as literals rather than Jinja templates on purpose: an email body has
    to survive clients that strip <style>, so it is inline-styled and table-free,
    and keeping it beside the sender is what stops the two drifting apart.
    """
    safe_username = html_escape.escape(username)
    safe_url = html_escape.escape(reset_url, quote=True)

    html = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:12px;padding:32px;">
      <h1 style="margin:0 0 16px;font-size:20px;line-height:1.3;color:#18181b;">
        Restablecer tu contrase&ntilde;a
      </h1>
      <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#3f3f46;">
        Hola <strong>{safe_username}</strong>, recibimos una solicitud para
        cambiar la contrase&ntilde;a de tu cuenta.
      </p>
      <p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#3f3f46;">
        Pulsa el bot&oacute;n para elegir una nueva. El enlace caduca el
        <strong>{expires_at}</strong> y solo puede usarse una vez.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{safe_url}"
           style="display:inline-block;background:#4f46e5;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-size:15px;font-weight:600;">
          Cambiar mi contrase&ntilde;a
        </a>
      </p>
      <p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:#71717a;">
        Si el bot&oacute;n no funciona, copia esta direcci&oacute;n en tu navegador:
      </p>
      <p style="margin:0 0 24px;font-size:13px;line-height:1.6;color:#4f46e5;word-break:break-all;">
        {safe_url}
      </p>
      <p style="margin:0;font-size:13px;line-height:1.6;color:#71717a;">
        Si no pediste este cambio, ignora este correo: tu contrase&ntilde;a
        actual sigue siendo v&aacute;lida.
      </p>
    </div>
  </body>
</html>"""

    text = f"""\
Restablecer tu contraseña

Hola {username}, recibimos una solicitud para cambiar la contraseña de tu
cuenta.

Abre esta dirección para elegir una nueva:

{reset_url}

El enlace caduca el {expires_at} y solo puede usarse una vez.

Si no pediste este cambio, ignora este correo: tu contraseña actual sigue
siendo válida.
"""
    return html, text


# --------------------------------------------------------------------------
# Callers
# --------------------------------------------------------------------------


def send_password_reset_email(
    user: User,
    *,
    token: Any | None = None,
    request_base_url: str | None = None,
) -> SendResult:
    """Email ``user`` a password reset link, minting a token if none is given.

    This is the function the future public "olvidé mi contraseña" form calls;
    everything it needs — token creation, link construction, delivery, logging —
    happens here so that form stays a thin route.

    ``token`` exists for the admin modal, which has already generated a link and
    is displaying it. Without it, this would mint a second token and
    ``create_reset_token`` would burn the first — the admin would be looking at
    a dead link on screen while the user received a different, live one.

    When minting, the token is created only after the preconditions pass.
    Creating it first would invalidate any existing valid link and then fail to
    deliver the replacement, leaving the user with strictly less than before.
    """
    if not is_enabled():
        return SendResult(
            ok=False,
            error_code="not_enabled",
            error_message="Resend sending is turned off.",
        )

    if not user.email:
        return SendResult(
            ok=False,
            error_code="no_email",
            error_message=f"{user.username} has no email address on file.",
        )

    base = _public_base_url(request_base_url)
    if not base:
        return SendResult(
            ok=False,
            error_code="no_base_url",
            error_message=(
                "No public URL is configured, so the reset link would point "
                "nowhere. Set it in Activity > Resend."
            ),
        )

    if token is None:
        from app.services.password_reset import create_reset_token

        token = create_reset_token(user.id)

    if not token:
        return SendResult(
            ok=False,
            error_code="token_error",
            error_message="Could not create a password reset token.",
        )

    reset_url = f"{base}/reset/{token.code}"
    expires_at = token.expires_at.strftime("%Y-%m-%d %H:%M UTC")
    html, text = _reset_email_bodies(user.username, reset_url, expires_at)

    return send_email(
        to_address=user.email,
        subject="Restablece tu contraseña",
        html=html,
        text=text,
        kind=KIND_PASSWORD_RESET,
        user_id=user.id,
    )


def send_test_email(to_address: str) -> SendResult:
    """Send a throwaway email to prove the key AND the domain both work.

    The domain check is the point. A saved key with an unverified sending domain
    produces a tab that looks configured and cannot mail anyone; only an actual
    send surfaces that, as a ``validation_error`` from Resend.
    """
    if not is_configured():
        return SendResult(
            ok=False,
            error_code="not_configured",
            error_message=(
                "Add a Resend API key and a 'From' address before sending a test."
            ),
        )

    sent_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:12px;padding:32px;">
      <h1 style="margin:0 0 16px;font-size:20px;color:#18181b;">Resend funciona</h1>
      <p style="margin:0;font-size:15px;line-height:1.6;color:#3f3f46;">
        Este correo de prueba sali&oacute; de sauron el {sent_at}. Si lo est&aacute;s
        leyendo, la clave de API y el dominio de env&iacute;o est&aacute;n bien
        configurados.
      </p>
    </div>
  </body>
</html>"""
    text = (
        f"Resend funciona.\n\nEste correo de prueba salió de sauron el {sent_at}. "
        "Si lo estás leyendo, la clave de API y el dominio de envío están bien "
        "configurados.\n"
    )

    return send_email(
        to_address=to_address,
        subject="Prueba de envío de sauron",
        html=html,
        text=text,
        kind=KIND_TEST,
    )


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def quota_usage() -> dict[str, int]:
    """Successful sends today and this calendar month, with the free-tier caps.

    Counted locally because Resend has no quota endpoint. Only ``sent`` rows
    count: a rejected request never consumed quota, and counting failures would
    make a burst of validation errors look like an exhausted allowance.

    "This month" is the calendar month, which is an approximation — Resend's
    monthly window follows the billing cycle. Close enough to warn on; the
    authoritative answer is the 429.
    """
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    sent = ResendEmail.query.filter(ResendEmail.status == STATUS_SENT)
    return {
        "today": sent.filter(ResendEmail.created_at >= day_start).count(),
        "today_limit": FREE_TIER_DAILY_LIMIT,
        "month": sent.filter(ResendEmail.created_at >= month_start).count(),
        "month_limit": FREE_TIER_MONTHLY_LIMIT,
    }
