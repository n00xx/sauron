import logging
import os

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_babel import _
from flask_login import login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from app.extensions import db, limiter, scaled_limit
from app.models import AdminAccount, AdminUser, Settings

auth_bp = Blueprint("auth", __name__)


def _client_ip() -> str:
    """Resolve the client IP, ignoring headers unless a proxy is configured.

    ``X-Forwarded-For`` and ``CF-Connecting-IP`` are attacker-controlled unless
    a trusted reverse proxy rewrites them. Trusting them unconditionally let
    anyone forge the IP recorded in AUTH FAIL logs and sent to Turnstile, so
    they are only honoured when TRUSTED_PROXY_COUNT says a proxy is in front.
    """
    remote_addr = request.remote_addr or ""

    try:
        trusted_proxies = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
    except ValueError:
        trusted_proxies = 0

    if trusted_proxies <= 0:
        return remote_addr

    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Right-most entries are appended by our own proxies; take the last
        # hop we do not control.
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[max(0, len(hops) - trusted_proxies)]

    return remote_addr


def _sso_proxy_authorised() -> bool:
    """Check that a DISABLE_BUILTIN_AUTH request really came from the proxy.

    The flag alone used to hand out an admin session to anyone who could reach
    /login, which is a full takeover if the container is exposed directly or
    the SSO proxy is bypassed in routing.
    """
    trusted = [
        ip.strip()
        for ip in os.getenv("SSO_TRUSTED_PROXY_IPS", "").split(",")
        if ip.strip()
    ]
    if not trusted:
        logging.error(
            "DISABLE_BUILTIN_AUTH is set but SSO_TRUSTED_PROXY_IPS is empty; "
            "refusing to bypass authentication"
        )
        return False

    identity_header = os.getenv("SSO_IDENTITY_HEADER", "X-Forwarded-User")
    if not request.headers.get(identity_header):
        logging.warning(
            "DISABLE_BUILTIN_AUTH request without %s header from %s",
            identity_header,
            request.remote_addr,
        )
        return False

    if (request.remote_addr or "") not in trusted:
        logging.warning(
            "DISABLE_BUILTIN_AUTH request from untrusted source %s",
            request.remote_addr,
        )
        return False

    return True


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(scaled_limit("10 per minute"))
def login():
    if os.getenv("DISABLE_BUILTIN_AUTH", "").lower() == "true":
        if not _sso_proxy_authorised():
            abort(403)
        login_user(AdminUser(), remember=bool(request.form.get("remember")))
        return redirect("/")

    # Pre-compute shared template context
    from app.models import LDAPConfiguration, WebAuthnCredential

    has_passkeys = WebAuthnCredential.query.first() is not None
    ldap_config = LDAPConfiguration.query.first()
    ldap_enabled = bool(
        ldap_config and ldap_config.enabled and ldap_config.allow_admin_bind
    )
    media_server_url = request.cookies.get("wizarr_media_server_url")

    if request.method == "GET":
        return render_template(
            "login.html",
            has_passkeys=has_passkeys,
            ldap_enabled=ldap_enabled,
            media_server_url=media_server_url,
            error=request.args.get("error"),
        )

    username = request.form.get("username")
    password = request.form.get("password")
    auth_method = request.form.get("auth_method", "local")

    client_ip = _client_ip()

    # ── Cloudflare Turnstile challenge ─────────────────────────────────
    # Gates the password/LDAP login form (both submit through here). Passkey
    # login is a separate JS flow and is intentionally not gated.
    from app.services.turnstile import is_turnstile_enabled, verify_turnstile

    if is_turnstile_enabled():
        token = request.form.get("cf-turnstile-response")
        if not verify_turnstile(token, client_ip):
            logging.warning(
                f"AUTH FAIL: Turnstile check failed for user '{username}' from {client_ip}"
            )
            return render_template(
                "login.html",
                error=_("Captcha verification failed. Please try again."),
                has_passkeys=has_passkeys,
                ldap_enabled=ldap_enabled,
                media_server_url=media_server_url,
                selected_auth_method=auth_method,
            )

    # ── Handle LDAP authentication ─────────────────────────────────────
    if auth_method == "ldap":
        from flask import session

        from app.models import WebAuthnCredential

        from .ldap_auth import handle_ldap_login

        success, message, account = handle_ldap_login(username, password)
        if success and account is not None:
            # Same second-factor rule as the local path: an account holding a
            # passkey must complete WebAuthn before it gets a session.
            if WebAuthnCredential.query.filter_by(
                admin_account_id=account.id
            ).first():
                session["pending_2fa_user_id"] = account.id
                session["pending_2fa_remember"] = bool(request.form.get("remember"))
                return render_template(
                    "login.html", show_2fa=True, username=username, has_passkeys=True
                )

            login_user(account, remember=bool(request.form.get("remember")))
            session.permanent = True
            return redirect("/")

        return render_template(
            "login.html",
            error=message,
            has_passkeys=has_passkeys,
            ldap_enabled=ldap_enabled,
            media_server_url=media_server_url,
            selected_auth_method=auth_method,
        )

    # ── 1) Multi-admin accounts (preferred) ────────────────────────────
    if (
        account := AdminAccount.query.filter_by(username=username).first()
    ) and account.check_password(password):
        # Check if this account has passkeys registered - if so, require 2FA
        from app.models import WebAuthnCredential

        if WebAuthnCredential.query.filter_by(admin_account_id=account.id).first():
            # Store user in session for 2FA verification
            from flask import session

            session["pending_2fa_user_id"] = account.id
            session["pending_2fa_remember"] = bool(request.form.get("remember"))
            return render_template(
                "login.html", show_2fa=True, username=username, has_passkeys=True
            )
        # No passkeys, allow direct login
        login_user(account, remember=bool(request.form.get("remember")))
        return redirect("/")

    # fetch the stored admin credentials
    admin_username = (
        db.session.query(Settings.value).filter_by(key="admin_username").scalar()
    )
    admin_password_hash = (
        db.session.query(Settings.value).filter_by(key="admin_password").scalar()
    )

    if (
        username == admin_username
        and password
        # admin_password_hash is None when the legacy Settings row is absent;
        # passing that to check_password_hash raised a 500 on a public route.
        and admin_password_hash
        and check_password_hash(admin_password_hash, password)
    ):
        # Legacy single-admin (Settings table)
        login_user(AdminUser(), remember=bool(request.form.get("remember")))
        return redirect("/")

    # Log failed login with IP (client_ip computed above)
    logging.warning(f"AUTH FAIL: Failed login for user '{username}' from {client_ip}")

    return render_template(
        "login.html",
        error=_("Invalid username or password"),
        has_passkeys=has_passkeys,
        ldap_enabled=ldap_enabled,
        media_server_url=media_server_url,
        selected_auth_method=auth_method,
    )


@auth_bp.route("/complete-2fa", methods=["POST"])
@limiter.limit(scaled_limit("10 per minute"))
def complete_2fa():
    """Complete 2FA authentication with passkey.

    Only the WebAuthn route may authorise this step: it stamps
    ``2fa_verified_user_id`` into the session after
    ``verify_authentication_response`` succeeds. The marker is single-use and
    must name the same account as the pending login, otherwise knowing the
    password alone would be enough to obtain a session.
    """
    from flask import abort, session

    user_id = session.get("pending_2fa_user_id")
    remember = session.get("pending_2fa_remember", False)
    # Consume the marker so a ceremony cannot be replayed for a later login.
    verified_user_id = session.pop("2fa_verified_user_id", None)

    if user_id and (verified_user_id is None or verified_user_id != user_id):
        logging.warning(
            "AUTH FAIL: /complete-2fa reached without a verified WebAuthn "
            "ceremony for account id %s",
            user_id,
        )
        session.pop("pending_2fa_user_id", None)
        session.pop("pending_2fa_remember", None)
        abort(403)

    if not user_id:
        # Check if there are any passkeys registered for error page
        from app.models import WebAuthnCredential

        has_passkeys = WebAuthnCredential.query.first() is not None
        return render_template(
            "login.html",
            error=_("No pending 2FA authentication"),
            has_passkeys=has_passkeys,
        )

    # Get the user account
    account = db.session.get(AdminAccount, user_id)
    if not account:
        session.pop("pending_2fa_user_id", None)
        session.pop("pending_2fa_remember", None)
        # Check if there are any passkeys registered for error page
        from app.models import WebAuthnCredential

        has_passkeys = WebAuthnCredential.query.first() is not None
        return render_template(
            "login.html", error=_("Authentication failed"), has_passkeys=has_passkeys
        )

    # The actual WebAuthn verification will be handled by the existing WebAuthn route
    # This route is called after successful WebAuthn authentication
    session.pop("pending_2fa_user_id", None)
    session.pop("pending_2fa_remember", None)

    login_user(account, remember=remember)
    return redirect("/")


# ── Logout ────────────────────────────────────────────────────────────


@auth_bp.route("/logout", methods=["GET"])
@login_required
def logout():
    """Terminate session and redirect to login page."""
    logout_user()
    return redirect(url_for("auth.login"))
