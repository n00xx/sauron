"""Cloudflare Turnstile verification for the admin login page.

Turnstile is configured either via the admin UI (persisted in the ``Settings``
key/value table) or via environment variables. The environment always wins so
that a misconfigured secret key can never lock an admin out of their own box:
set ``TURNSTILE_ENABLED=false`` to force-disable the challenge regardless of what
is stored in the database.

Failure policy:
- Missing or invalid token          -> reject   (fail-closed)
- Cloudflare siteverify unreachable -> allow    (fail-open, logged)
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SITEVERIFY_TIMEOUT = 5  # seconds


def _env_flag(name: str) -> bool | None:
    """Return a tri-state env flag: True/False if set, None if unset/blank."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _setting(key: str) -> str | None:
    """Read a single ``Settings`` value, tolerating a missing table."""
    try:
        from app.extensions import db
        from app.models import Settings

        with db.session.no_autoflush:
            row = Settings.query.filter_by(key=key).first()
        return row.value if row else None
    except Exception:
        return None


def get_site_key() -> str | None:
    """Public site key (safe to expose to the browser)."""
    return os.getenv("TURNSTILE_SITE_KEY") or _setting("turnstile_site_key")


def get_secret_key() -> str | None:
    """Private secret key (server-side only, never sent to templates)."""
    return os.getenv("TURNSTILE_SECRET_KEY") or _setting("turnstile_secret_key")


def is_turnstile_enabled() -> bool:
    """Whether the Turnstile challenge should be enforced on the login form.

    The env override wins over the stored setting. Turnstile is only considered
    enabled when both a site key and a secret key are actually available, so an
    incomplete configuration silently disables the challenge instead of blocking
    every login attempt.
    """
    override = _env_flag("TURNSTILE_ENABLED")
    if override is False:
        return False

    if override is True:
        enabled = True
    else:
        enabled = str(_setting("turnstile_enabled")).lower() == "true"

    if not enabled:
        return False

    return bool(get_site_key() and get_secret_key())


def verify_turnstile(token: str | None, remoteip: str | None = None) -> bool:
    """Validate a Turnstile response token against Cloudflare's siteverify API.

    Returns ``True`` when the token is valid, ``False`` when it is missing or
    rejected. On a network error/timeout reaching Cloudflare we fail open
    (return ``True``) and log a warning, so a Cloudflare outage cannot lock
    admins out of their own instance.
    """
    if not token:
        return False

    secret = get_secret_key()
    if not secret:
        # Enforcement requested but no secret configured: fail open rather than
        # brick logins. is_turnstile_enabled() normally prevents reaching here.
        logger.warning("Turnstile enabled but no secret key configured; skipping check")
        return True

    payload = {"secret": secret, "response": token}
    if remoteip:
        payload["remoteip"] = remoteip

    try:
        resp = requests.post(SITEVERIFY_URL, data=payload, timeout=SITEVERIFY_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Turnstile siteverify unreachable, allowing login: %s", exc)
        return True  # fail-open on outage
    except ValueError as exc:  # malformed JSON from siteverify
        logger.warning("Turnstile siteverify returned invalid JSON: %s", exc)
        return True  # fail-open: treat as service problem, not a bad user token

    if not data.get("success"):
        logger.warning("Turnstile verification failed: %s", data.get("error-codes", []))
        return False

    return True
