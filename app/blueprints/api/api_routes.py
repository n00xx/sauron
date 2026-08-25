"""Flask-RESTX API routes with OpenAPI documentation."""

import datetime
import hashlib
import logging
import traceback
from functools import wraps
from typing import ClassVar

from flask import Blueprint, request
from flask_login import current_user
from flask_restx import Resource, abort
from sqlalchemy import func

from app.extensions import api, db, limiter, scaled_limit
from app.models import (
    AdminAccount,
    ApiKey,
    Invitation,
    Library,
    MediaServer,
    User,
    WebAuthnCredential,
)
from app.services.credentials import verify_media_credentials
from app.services.expiry import cleanup_expired_user_by_email
from app.services.invites import create_invite
from app.services.media.service import (
    delete_user,
    disable_user,
    enable_user,
    get_client_for_media_server,
    list_users_all_servers,
)
from app.services.server_name_resolver import get_display_name_info

from .models import (
    admin_list_model,
    api_key_list_model,
    error_model,
    invitation_create_request,
    invitation_create_response,
    invitation_list_model,
    library_list_model,
    server_list_model,
    status_model,
    success_message_model,
    user_extend_request,
    user_extend_response,
    user_list_model,
    user_max_sessions_request,
    user_max_sessions_response,
    user_update_expiry_request,
    user_update_expiry_response,
    user_verify_credentials_request,
    user_verify_credentials_response,
)

# Create the Blueprint for the API
api_bp = Blueprint("api", __name__, url_prefix="/api")

# Initialize Flask-RESTX with the blueprint
api.init_app(api_bp)

# Set up logging
logger = logging.getLogger("wizarr.api")

# Create namespaces for organizing endpoints
status_ns = api.namespace("status", description="System status operations")
users_ns = api.namespace("users", description="User management operations")
invitations_ns = api.namespace(
    "invitations", description="Invitation management operations"
)
libraries_ns = api.namespace("libraries", description="Library information operations")
servers_ns = api.namespace("servers", description="Server information operations")
api_keys_ns = api.namespace("api-keys", description="API key management operations")
admins_ns = api.namespace("admins", description="Admin management operations")


def require_api_key(f):
    """Decorator to require valid API key for endpoint access."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_key = request.headers.get("X-API-Key")
        if not auth_key:
            logger.warning("API request without API key from %s", request.remote_addr)
            abort(401, error="Unauthorized")

        # Type assertion since we've already checked that auth_key exists
        assert isinstance(auth_key, str)

        # Hash the provided key to compare with stored hash
        key_hash = hashlib.sha256(auth_key.encode("utf-8")).hexdigest()
        api_key = ApiKey.query.filter_by(key_hash=key_hash, is_active=True).first()

        if not api_key:
            logger.warning(
                "API request with invalid API key from %s", request.remote_addr
            )
            abort(401, error="Unauthorized")

        # Update last used timestamp
        api_key.last_used_at = datetime.datetime.now(datetime.UTC)
        db.session.commit()

        logger.info(
            "API request authenticated with key '%s' from %s",
            api_key.name,
            request.remote_addr,
        )
        return f(*args, **kwargs)

    return decorated_function


def require_api_key_or_session(f):
    """Decorator to require either valid API key or authenticated session for endpoint access."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user is authenticated via session (Flask-Login)
        if current_user.is_authenticated:
            logger.info(
                "API request authenticated via session from %s",
                request.remote_addr,
            )
            return f(*args, **kwargs)

        # Fall back to API key authentication
        auth_key = request.headers.get("X-API-Key")
        if not auth_key:
            logger.warning(
                "API request without API key or session from %s", request.remote_addr
            )
            abort(401, error="Unauthorized - API key or session required")

        # Type assertion since we've already checked that auth_key exists
        assert isinstance(auth_key, str)

        # Hash the provided key to compare with stored hash
        key_hash = hashlib.sha256(auth_key.encode("utf-8")).hexdigest()
        api_key = ApiKey.query.filter_by(key_hash=key_hash, is_active=True).first()

        if not api_key:
            logger.warning(
                "API request with invalid API key from %s", request.remote_addr
            )
            abort(401, error="Unauthorized")

        # Update last used timestamp
        api_key.last_used_at = datetime.datetime.now(datetime.UTC)
        db.session.commit()

        logger.info(
            "API request authenticated with key '%s' from %s",
            api_key.name,
            request.remote_addr,
        )
        return f(*args, **kwargs)

    return decorated_function


def _generate_invitation_url(code):
    """Generate the stable invitation path for the given code."""
    try:
        from flask import url_for

        return url_for("public.invite", code=code, _external=False)

    except Exception as e:
        logger.warning("Failed to generate invitation URL: %s", str(e))
        # Fallback to basic format
        return f"/j/{code}"


def _calculate_invitation_status(invitation):
    """Calculate the current status of an invitation based on its fields."""
    from datetime import UTC, datetime

    if invitation.used:
        return "used"

    # Check if invitation has expired
    if invitation.expires:
        # Handle timezone-aware/naive datetime comparison
        now = datetime.now(UTC)
        expires = invitation.expires

        # If expires is naive, assume it's UTC
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)

        if expires <= now:
            return "expired"

    # Otherwise it's pending
    return "pending"


@status_ns.route("")
class StatusResource(Resource):
    @api.doc("get_status", security="apikey")
    @api.marshal_with(status_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """Get overall statistics about your Wizarr instance."""
        try:
            logger.info("API: Getting system status")

            # Get statistics
            from datetime import UTC, datetime

            total_users = User.query.count()
            total_invitations = Invitation.query.count()

            # Calculate pending invitations: not used and (no expiry or not expired yet)
            now = datetime.now(UTC)

            # Get all unused invitations to check their status properly
            all_invitations = Invitation.query.filter(Invitation.used.is_(False)).all()
            pending_invitations = 0
            expired_invitations = 0

            for invitation in all_invitations:
                if invitation.expires is None:
                    pending_invitations += 1
                else:
                    expires = invitation.expires
                    # Handle timezone-naive datetime
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)

                    if expires > now:
                        pending_invitations += 1
                    else:
                        expired_invitations += 1

            return {
                "users": total_users,
                "invites": total_invitations,
                "pending": pending_invitations,
                "expired": expired_invitations,
            }
        except Exception as e:
            logger.error("Error getting system status: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@admins_ns.route("")
class AdminListResource(Resource):
    @api.doc(
        "list_admins",
        security="apikey",
        params={
            "username": "Filter by username (exact match)",
        },
    )
    @api.marshal_with(admin_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all Wizarr admins. Supports filtering by username."""
        try:
            # Get query parameters
            username_filter = request.args.get("username")
            logger.info("API: Listing all admins (username=%s)", username_filter)

            # Optimized query: JOIN admins with passkey counts in single query
            query = (
                db.session.query(
                    AdminAccount.id,
                    AdminAccount.username,
                    AdminAccount.created_at,
                    func.count(WebAuthnCredential.id).label("passkey_count"),
                )
                .outerjoin(
                    WebAuthnCredential,
                    AdminAccount.id == WebAuthnCredential.admin_account_id,
                )
                .group_by(
                    AdminAccount.id, AdminAccount.username, AdminAccount.created_at
                )
                .order_by(AdminAccount.username)
            )

            # Apply username filter at database level if specified
            if username_filter:
                query = query.filter(AdminAccount.username == username_filter)

            results = query.all()

            # Format response
            admins_list = [
                {
                    "id": result.id,
                    "username": result.username,
                    "passkeys": result.passkey_count,
                    "created": result.created_at.isoformat()
                    if result.created_at
                    else None,
                }
                for result in results
            ]

            return {"admins": admins_list, "count": len(admins_list)}

        except Exception as e:
            logger.error("Error listing admins: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("")
class UsersListResource(Resource):
    @api.doc(
        "list_users",
        security="apikey",
        params={
            "username": "Filter by username (exact match)",
            "email": "Filter by email address (exact match)",
        },
    )
    @api.marshal_with(user_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all users across all media servers. Supports filtering by username or email."""
        try:
            # Get query parameters
            username_filter = request.args.get("username")
            email_filter = request.args.get("email")

            logger.info(
                "API: Listing all users (username=%s, email=%s)",
                username_filter,
                email_filter,
            )
            users_by_server = list_users_all_servers()

            # Format response
            users_list = []
            for server_id, users in users_by_server.items():
                # Get server info
                server = db.session.get(MediaServer, server_id)
                if not server:
                    continue

                for user in users:
                    # Apply filters if specified
                    if username_filter and user.username != username_filter:
                        continue
                    if email_filter and user.email != email_filter:
                        continue

                    users_list.append(
                        {
                            "id": user.id,
                            "username": user.username,
                            "email": user.email,
                            "server": server.name,
                            "server_type": server.server_type,
                            "expires": user.expires.isoformat()
                            if user.expires
                            else None,
                            "created_at": user.created_at.isoformat()
                            if hasattr(user, "created_at") and user.created_at
                            else None,
                        }
                    )

            return {"users": users_list, "count": len(users_list)}

        except Exception as e:
            logger.error("Error listing users: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>")
class UserResource(Resource):
    @api.doc("delete_user", security="apikey")
    @api.response(200, "User deleted successfully", success_message_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def delete(self, user_id):
        """Delete a specific user by ID."""
        # Find user first, outside try block
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        # Get server info for the user
        server = db.session.get(MediaServer, user.server_id)
        if not server:
            abort(404, error="Server not found for user")

        try:
            logger.info("API: Deleting user %s", user_id)

            # Delete user using the service (takes only user.id)
            delete_user(user.id)
            return {"message": f"User {user.username} deleted successfully"}

        except Exception as e:
            logger.error("Error deleting user %s: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>/enable")
class UserEnableResource(Resource):
    @api.doc("enable_user", security="apikey")
    @api.response(200, "User enabled successfully", success_message_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(502, "Media server refused or does not support enabling", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self, user_id):
        """Enable a specific user by ID.

        This will enable the user account on the media server if the server supports it.

        Returns 502 when the media server refuses or does not support enabling.
        This used to answer 200 with a "Enable failed..." message, which made
        success and failure indistinguishable to an API client — a paid renewal
        would report as delivered while the account stayed disabled.
        """
        # Find user first, outside try block
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        # Get server info for the user
        server = db.session.get(MediaServer, user.server_id)
        if not server:
            abort(404, error="Server not found for user")

        try:
            logger.info("API: Attempting to enable user %s", user_id)

            # Try to enable user
            result = enable_user(user.id)

            if not result:
                logger.warning(
                    "Enable failed or not supported for user %s",
                    user_id,
                )
                return {
                    "error": f"Enable failed or not supported for user {user.username}"
                }, 502

            # The account is live again, so the expiry sweep's record of it is
            # stale — without this the admin UI keeps listing a renewed customer
            # under "expired users". Mirrors cleanup_expired_user_by_email's
            # existing use on re-signup.
            if user.email:
                cleanup_expired_user_by_email(user.email)

            return {"message": f"User {user.username} enabled successfully"}

        except Exception as e:
            logger.error("Error enabling user %s: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>/disable")
class UserDisableResource(Resource):
    @api.doc("disable_user", security="apikey")
    @api.response(200, "User disabled successfully", success_message_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self, user_id):
        """Disable a specific user by ID.

        Never falls back to deleting. A failed disable returns 502 so the caller
        can retry or alert: deletion is irreversible, and a caller that asked to
        DISABLE an account has not consented to losing it.
        """
        # Find user first, outside try block
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        # Get server info for the user
        server = db.session.get(MediaServer, user.server_id)
        if not server:
            abort(404, error="Server not found for user")

        try:
            logger.info("API: Attempting to disable user %s", user_id)

            # Try to disable user
            result = disable_user(user.id)

            if result:
                return {"message": f"User {user.username} disabled successfully"}

            # Do NOT delete. The caller asked to disable; escalating to an
            # irreversible delete on failure is how paying customers lose their
            # accounts. Report the failure and let the caller decide.
            logger.error(
                "Disable failed for user %s on %s - refusing to delete instead",
                user_id,
                server.server_type,
            )
            return {
                "error": f"Could not disable user {user.username} on {server.server_type}"
            }, 502

        except Exception as e:
            logger.error("Error disabling user %s: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>/extend")
class UserExtendResource(Resource):
    @api.doc("extend_user_expiry", security="apikey")
    @api.expect(user_extend_request)
    @api.marshal_with(user_extend_response)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self, user_id):
        """Extend a user's expiry date."""
        # Find user first, outside try block to allow abort to work properly
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            logger.info("API: Extending expiry for user %s", user_id)

            # Get request data
            data = api.payload or {}
            days = data.get("days", 30)

            # Extend expiry
            if user.expires:
                new_expiry = user.expires + datetime.timedelta(days=days)
            else:
                new_expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                    days=days
                )

            user.expires = new_expiry
            db.session.commit()

            return {
                "message": f"User {user.username} expiry extended by {days} days",
                "new_expiry": new_expiry.isoformat(),
            }

        except Exception as e:
            logger.error("Error extending user %s expiry: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>/update-expiry")
class UserUpdateExpiryResource(Resource):
    @api.doc("update_user_expiry", security="apikey")
    @api.expect(user_update_expiry_request)
    @api.marshal_with(user_update_expiry_response)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def put(self, user_id):
        """Update a user's expiry date to a specific date or unlimited."""
        # Find user first, outside try block to allow abort to work properly
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            logger.info("API: Updating expiry for user %s", user_id)

            # Get request data
            data = api.payload or {}
            new_expiry = data.get("expires")

            # Parse the datetime if provided
            if new_expiry is not None and isinstance(new_expiry, str):
                try:
                    # Parse ISO format datetime string
                    new_expiry = datetime.datetime.fromisoformat(
                        new_expiry.replace("Z", "+00:00")
                    )
                    # Ensure it's UTC timezone aware
                    if new_expiry.tzinfo is None:
                        new_expiry = new_expiry.replace(tzinfo=datetime.UTC)
                except ValueError as e:
                    return {
                        "error": f"Invalid datetime format. Expected ISO format: {e!s}"
                    }, 400

            # Update the user's expiry
            user.expires = new_expiry
            db.session.commit()

            # Prepare response message
            if new_expiry is None:
                message = f"User {user.username} expiry updated to unlimited"
                response_expiry = None
            else:
                message = (
                    f"User {user.username} expiry updated to {new_expiry.isoformat()}"
                )
                response_expiry = new_expiry.isoformat()

            return {
                "message": message,
                "new_expiry": response_expiry,
            }

        except Exception as e:
            logger.error("Error updating user %s expiry: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


def _verify_credentials_rate_key() -> str:
    """Rate-limit key for the credential check: the submitted username.

    Normalised so "Juan", "juan" and " juan " share one allowance instead of
    handing an attacker three. Namespaced so it can never collide with the
    per-IP bucket on the same endpoint.

    Applies to whatever string was submitted, existing account or not — the
    limiter must not become the enumeration oracle the endpoint itself avoids.
    """
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    if not isinstance(username, str):
        return "verifycreds:__malformed__"
    return f"verifycreds:{username.strip().lower()[:64]}"


@users_ns.route("/verify-credentials")
class UserVerifyCredentialsResource(Resource):
    """Prove ownership of a media account with its own username + password.

    Built for self-service renewal checkouts: before taking money to renew
    account X, the payment flow has to know the buyer actually owns X.

    ── Enumeration ──────────────────────────────────────────────────────────
    ALWAYS answers 200 with the same body shape. Unknown username, wrong
    password, disabled account and unsupported server type are indistinguishable
    from outside. Do not "improve" the error reporting here: Jellyfin itself
    leaks this distinction (401 vs 403) and collapsing it is the point.

    ── Account lockout ──────────────────────────────────────────────────────
    Jellyfin disables an account after LoginAttemptsBeforeLockout failed logins,
    so an uncapped public form here would let anyone disable a paying customer's
    account. Two mitigations, and the second is the one that actually holds:

      1. The caps below. They bound the rate, but NOT the total: Jellyfin's
         InvalidLoginAttemptCount persists and only resets on a SUCCESSFUL
         login, so failures accumulate across windows.
      2. app/services/credentials.py detects a lockout it caused and re-enables
         the account. A successful check also resets Jellyfin's counter to zero,
         which is what actually heals an account that has been probed.
    """

    # Class-level rather than per-method: this is the shape flask-restx supports
    # for Flask-Limiter. A @limiter.limit stacked directly on `post` registers
    # against the METHOD's qualname, while at request time Flask-Limiter resolves
    # limits through the endpoint's view function — which for a Resource is the
    # dispatcher, not the method — so the decorated limit is never found and the
    # endpoint silently runs uncapped. Safe here because `post` is the only
    # method on this Resource.
    decorators: ClassVar[list] = [
        limiter.limit(scaled_limit("20 per hour")),
        limiter.limit(
            scaled_limit("3 per hour"), key_func=_verify_credentials_rate_key
        ),
        limiter.limit(
            scaled_limit("10 per day"), key_func=_verify_credentials_rate_key
        ),
    ]

    @api.doc("verify_user_credentials", security="apikey")
    @api.expect(user_verify_credentials_request)
    @api.response(
        200, "Check completed (see `valid`)", user_verify_credentials_response
    )
    @api.response(400, "Malformed request body", error_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(429, "Too many attempts", error_model)
    @require_api_key
    def post(self):
        """Verify a media account's username and password."""
        data = api.payload or {}
        username = data.get("username")
        password = data.get("password")

        # Shape check only. A missing field is a caller bug, not a failed login,
        # so it is safe to report — it says nothing about any account.
        if not isinstance(username, str) or not isinstance(password, str):
            return {"error": "username and password are required strings"}, 400

        try:
            # The password is passed straight through as a local and never
            # logged, echoed, or persisted anywhere in this call.
            user_id = verify_media_credentials(username, password)
        except Exception as e:
            # Log the failure WITHOUT the payload, then answer exactly like a
            # rejected password: an internal error must not become a signal
            # that distinguishes one account from another.
            logger.error("Error verifying credentials: %s", str(e))
            logger.error(traceback.format_exc())
            return {"valid": False, "user_id": None}

        if user_id is None:
            logger.info("API: credential check rejected")
            return {"valid": False, "user_id": None}

        logger.info("API: credential check passed for user %s", user_id)
        return {"valid": True, "user_id": user_id}


@users_ns.route("/<int:user_id>/max-sessions")
class UserMaxSessionsResource(Resource):
    """Set the simultaneous-stream limit on an EXISTING account.

    Upstream applies max_active_sessions only when an invitation is redeemed,
    which covers a first purchase but not a renewal: a buyer who upgrades tier
    already has an account, so without this the money is taken for "4
    dispositivos" and the account keeps whatever limit it had.
    """

    @api.doc("set_user_max_sessions", security="apikey")
    @api.expect(user_max_sessions_request)
    @api.response(200, "Limit applied", user_max_sessions_response)
    @api.response(400, "Invalid max_active_sessions", error_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(
        502, "Media server refused or does not support the limit", error_model
    )
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self, user_id):
        """Set a user's Jellyfin MaxActiveSessions (0 = unlimited)."""
        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        server = db.session.get(MediaServer, user.server_id)
        if not server:
            abort(404, error="Server not found for user")
            return None

        data = api.payload or {}
        max_sessions = data.get("max_active_sessions")
        # Reject bools explicitly: bool is a subclass of int in Python, so
        # `True` would otherwise sail through and be applied as a limit of 1.
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int):
            return {"error": "max_active_sessions must be an integer"}, 400
        if max_sessions < 0:
            return {"error": "max_active_sessions must be >= 0"}, 400

        try:
            client = get_client_for_media_server(server)
            setter = getattr(client, "set_max_active_sessions", None)
            if setter is None:
                # Not a hypothetical: only the Jellyfin/Emby client implements
                # this. Fail loudly rather than reporting a limit that was
                # never applied.
                return {
                    "error": f"{server.server_type} does not support session limits"
                }, 502

            logger.info(
                "API: setting max_active_sessions=%s for user %s",
                max_sessions,
                user_id,
            )
            if not setter(user.token, max_sessions):
                return {
                    "error": f"Media server refused the limit for user {user.username}"
                }, 502

            return {
                "message": f"User {user.username} limited to {max_sessions} session(s)",
                "max_active_sessions": max_sessions,
            }

        except Exception as e:
            logger.error("Error setting max sessions for user %s: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@invitations_ns.route("")
class InvitationsListResource(Resource):
    @api.doc("list_invitations", security="apikey")
    @api.marshal_with(invitation_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all invitations with their current status and server information."""
        try:
            logger.info("API: Listing all invitations")

            invitations = Invitation.query.all()
            invitations_list = []

            for invitation in invitations:
                # Get servers associated with the invitation
                servers = []

                # Check new multi-server relationship first
                if invitation.servers:
                    servers = invitation.servers
                # Fall back to legacy single server field
                elif invitation.server_id:
                    server = db.session.get(MediaServer, invitation.server_id)
                    if server:
                        servers = [server]

                # Use server name resolver for display name logic
                display_info = get_display_name_info(servers)

                # Convert specific_libraries from string to list of integers
                specific_libraries = []
                if invitation.specific_libraries:
                    try:
                        # Parse comma-separated string to list of integers
                        specific_libraries = [
                            int(lib_id.strip())
                            for lib_id in invitation.specific_libraries.split(",")
                            if lib_id.strip().isdigit()
                        ]
                    except (ValueError, AttributeError):
                        # If parsing fails, use empty list
                        specific_libraries = []

                invitations_list.append(
                    {
                        "id": invitation.id,
                        "code": invitation.code,
                        "url": _generate_invitation_url(invitation.code),
                        "status": _calculate_invitation_status(invitation),
                        "created": invitation.created.isoformat()
                        if invitation.created
                        else None,
                        "expires": invitation.expires.isoformat()
                        if invitation.expires
                        else None,
                        "used_at": invitation.used_at.isoformat()
                        if invitation.used_at
                        else None,
                        "used_by": invitation.used_by,
                        "duration": str(invitation.duration)
                        if invitation.duration
                        else "unlimited",
                        "unlimited": invitation.unlimited,
                        "specific_libraries": specific_libraries,
                        "display_name": display_info["display_name"],
                        "server_names": display_info["server_names"],
                        "uses_global_setting": display_info["uses_global_setting"],
                        "max_active_sessions": invitation.max_active_sessions,
                    }
                )

            return {"invitations": invitations_list, "count": len(invitations_list)}

        except Exception as e:
            logger.error("Error listing invitations: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500

    @api.doc("create_invitation", security="apikey")
    @api.expect(invitation_create_request)
    @api.response(201, "Invitation created successfully", invitation_create_response)
    @api.response(400, "Bad request - missing required fields", error_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self):
        """Create a new invitation."""
        try:
            logger.info("API: Creating new invitation")

            data = api.payload or {}
            server_ids = data.get("server_ids")

            if not server_ids:
                # Return available servers for selection
                servers = MediaServer.query.filter_by(verified=True).all()
                available_servers = [
                    {"id": s.id, "name": s.name, "server_type": s.server_type}
                    for s in servers
                ]
                # Return error response without marshalling
                response_data = {
                    "error": "Server selection is required. Please specify server_ids in request.",
                    "available_servers": available_servers,
                }
                from flask import jsonify, make_response

                return make_response(jsonify(response_data), 400)

            # Validate that all server IDs exist and are verified
            servers = MediaServer.query.filter(
                MediaServer.id.in_(server_ids), MediaServer.verified
            ).all()

            found_server_ids = {s.id for s in servers}
            invalid_ids = [sid for sid in server_ids if sid not in found_server_ids]

            if invalid_ids:
                from flask import jsonify, make_response

                response_data = {
                    "error": f"Server IDs {invalid_ids} not found or not verified"
                }
                return make_response(jsonify(response_data), 400)

            # Create a form-like object that create_invite expects
            class FormLike:
                def __init__(self, data):
                    self.data = data

                def get(self, key, default=None):
                    return self.data.get(key, default)

                def getlist(self, key):
                    val = self.data.get(key, [])
                    return (
                        val
                        if isinstance(val, list)
                        else [val]
                        if val is not None
                        else []
                    )

            # Map expires_in_days to the format expected by create_invite
            expires_mapping = {1: "day", 7: "week", 30: "month"}
            expires_key = expires_mapping.get(data.get("expires_in_days"), "never")  # type: ignore

            form_data = FormLike(
                {
                    "server_ids": server_ids,
                    "expires": expires_key,
                    "duration": data.get("duration", "unlimited"),
                    "unlimited": data.get("unlimited", True),
                    "libraries": [
                        str(lid) for lid in data.get("library_ids", [])
                    ],  # Convert to strings
                    "allow_downloads": data.get("allow_downloads", False),
                    "allow_live_tv": data.get("allow_live_tv", False),
                    "allow_mobile_uploads": data.get("allow_mobile_uploads", False),
                    # Jellyfin transcoding toggles default ON (parity with the
                    # Create Invitation modal, which renders them checked).
                    "allow_transcode_audio": data.get("allow_transcode_audio", False),
                    "allow_transcode_video": data.get("allow_transcode_video", False),
                    "wizard_bundle_id": data.get("wizard_bundle_id"),
                    # Jellyfin max simultaneous streams. create_invite() calls
                    # .strip() on this, so coerce JSON ints/None to the string
                    # contract the web form uses. 0 = unlimited.
                    "max_active_sessions": (
                        str(data["max_active_sessions"])
                        if data.get("max_active_sessions") is not None
                        else None
                    ),
                }
            )

            invitation = create_invite(form_data)

            if invitation:
                server = db.session.get(MediaServer, server_ids[0])
                return {
                    "message": "Invitation created successfully",
                    "invitation": {
                        "id": invitation.id,
                        "code": invitation.code,
                        "url": _generate_invitation_url(invitation.code),
                        "expires": invitation.expires.isoformat()
                        if invitation.expires
                        else None,
                        "duration": str(invitation.duration)
                        if invitation.duration
                        else "unlimited",
                        "unlimited": invitation.unlimited,
                        "display_name": server.name if server else "Unknown",
                        "server_names": [server.name] if server else [],
                        "uses_global_setting": False,
                        "max_active_sessions": invitation.max_active_sessions,
                    },
                }, 201
            from flask import jsonify, make_response

            return make_response(jsonify({"error": "Failed to create invitation"}), 500)

        except Exception as e:
            logger.error("Error creating invitation: %s", str(e))
            logger.error(traceback.format_exc())
            from flask import jsonify, make_response

            return make_response(jsonify({"error": "Internal server error"}), 500)


@invitations_ns.route("/<int:invitation_id>")
class InvitationResource(Resource):
    @api.doc("delete_invitation", security="apikey")
    @api.response(200, "Invitation deleted successfully", success_message_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "Invitation not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def delete(self, invitation_id):
        """Delete a specific invitation."""
        # Find invitation first, outside try block
        invitation = db.session.get(Invitation, invitation_id)
        if not invitation:
            abort(404, error="Invitation not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            logger.info("API: Deleting invitation %s", invitation_id)

            code = invitation.code
            db.session.delete(invitation)
            db.session.commit()

            return {"message": f"Invitation {code} deleted successfully"}

        except Exception as e:
            logger.error("Error deleting invitation %s: %s", invitation_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@invitations_ns.route("/<int:invitation_id>/disable-users")
class InvitationDisableUsersResource(Resource):
    @api.doc("disable_invitation_users", security="apikey")
    @api.response(200, "Redeemed users disabled")
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "Invitation not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def post(self, invitation_id):
        """Disable every user who redeemed this invitation.

        Chargeback/refund revocation: resolves the invitation→users mapping
        (the many-to-many `invitation_user` table; falls back to the legacy
        `used_by_id` column for pre-2025-08 rows) and disables each user on
        their media server — falling back to deleting the user when the
        server can't disable, same semantics as POST /users/<id>/disable.

        Call this BEFORE deleting the invitation: the mapping rows cascade
        away with the invitation (`ondelete="CASCADE"`).
        """
        invitation = db.session.get(Invitation, invitation_id)
        if not invitation:
            abort(404, error="Invitation not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            users = list(invitation.users)
            # Legacy fallback: old rows only populated used_by_id.
            if not users and invitation.used_by:
                users = [invitation.used_by]

            results = []
            failed = []
            for user in users:
                logger.info(
                    "API: Disabling user %s (invitation %s revocation)",
                    user.id,
                    invitation_id,
                )
                if disable_user(user.id):
                    results.append(
                        {
                            "user_id": user.id,
                            "username": user.username,
                            "action": "disabled",
                        }
                    )
                else:
                    # Never escalate to deletion: revoking access must not be
                    # able to destroy the account. Report it so the caller can
                    # alert a human instead of assuming success.
                    logger.error(
                        "Disable failed for user %s (invitation %s) - refusing "
                        "to delete instead",
                        user.id,
                        invitation_id,
                    )
                    failed.append(
                        {
                            "user_id": user.id,
                            "username": user.username,
                            "action": "failed",
                        }
                    )

            # `count` stays the number of users actually DISABLED, so an older
            # caller that only reads count keeps reading a true number.
            return {"count": len(results), "users": results, "failed": failed}

        except Exception as e:
            logger.error(
                "Error disabling users for invitation %s: %s", invitation_id, str(e)
            )
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@libraries_ns.route("")
class LibrariesResource(Resource):
    @api.doc("list_libraries", security="apikey")
    @api.marshal_with(library_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all available libraries across all servers."""
        try:
            logger.info("API: Listing all libraries")

            libraries = Library.query.all()

            # If no libraries exist, scan all servers to populate them
            if not libraries:
                logger.info("No libraries found, scanning all verified servers")
                servers = MediaServer.query.filter_by(verified=True).all()
                for server in servers:
                    try:
                        logger.info(f"Scanning libraries for server {server.name}")
                        from app.services.media.service import scan_libraries_for_server

                        library_data = scan_libraries_for_server(server)

                        # Create Library records for each scanned library
                        for external_id, name in library_data.items():
                            # Check if library already exists to avoid duplicates
                            existing = Library.query.filter_by(
                                external_id=external_id, server_id=server.id
                            ).first()

                            if not existing:
                                library = Library(
                                    external_id=external_id,
                                    name=name,
                                    server_id=server.id,
                                    enabled=True,
                                )
                                db.session.add(library)

                        db.session.commit()
                        logger.info(
                            f"Added {len(library_data)} libraries for server {server.name}"
                        )

                    except Exception as e:
                        logger.error(
                            f"Failed to scan libraries for server {server.name}: {e!s}"
                        )
                        # Continue with other servers even if one fails
                        continue

                # Re-query libraries after scanning
                libraries = Library.query.all()

            libraries_list = []

            for lib in libraries:
                # Get server name
                server = (
                    db.session.get(MediaServer, lib.server_id)
                    if lib.server_id
                    else None
                )
                server_name = server.name if server else "Unknown"

                libraries_list.append(
                    {
                        "id": lib.id,
                        "name": lib.name,
                        "external_id": lib.external_id,
                        "server_id": lib.server_id,
                        "server_name": server_name,
                        "enabled": lib.enabled,
                    }
                )

            return {"libraries": libraries_list, "count": len(libraries_list)}

        except Exception as e:
            logger.error("Error listing libraries: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@servers_ns.route("")
class ServersResource(Resource):
    @api.doc("list_servers", security="apikey")
    @api.marshal_with(server_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all configured media servers."""
        try:
            logger.info("API: Listing all servers")

            servers = MediaServer.query.all()
            servers_list = [
                {
                    "id": server.id,
                    "name": server.name,
                    "server_type": server.server_type,
                    "server_url": server.url,
                    "external_url": getattr(server, "external_url", None),
                    "verified": server.verified,
                    "allow_downloads": getattr(server, "allow_downloads", False),
                    "allow_live_tv": getattr(server, "allow_live_tv", False),
                    "allow_mobile_uploads": getattr(
                        server, "allow_mobile_uploads", False
                    ),
                    "created_at": server.created_at.isoformat()
                    if hasattr(server, "created_at") and server.created_at
                    else None,
                }
                for server in servers
            ]

            return {"servers": servers_list, "count": len(servers_list)}

        except Exception as e:
            logger.error("Error listing servers: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@api_keys_ns.route("")
class ApiKeysResource(Resource):
    @api.doc("list_api_keys", security="apikey")
    @api.marshal_with(api_key_list_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def get(self):
        """List all active API keys (excluding the actual key values for security)."""
        try:
            logger.info("API: Listing all API keys")

            api_keys = ApiKey.query.filter_by(is_active=True).all()
            keys_list = [
                {
                    "id": key.id,
                    "name": key.name,
                    "created_at": key.created_at.isoformat()
                    if key.created_at
                    else None,
                    "last_used_at": key.last_used_at.isoformat()
                    if key.last_used_at
                    else None,
                    "created_by": getattr(key, "created_by", "admin"),
                }
                for key in api_keys
            ]

            return {"api_keys": keys_list, "count": len(keys_list)}

        except Exception as e:
            logger.error("Error listing API keys: %s", str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@api_keys_ns.route("/<int:key_id>")
class ApiKeyResource(Resource):
    @api.doc("delete_api_key", security="apikey")
    @api.response(200, "API key deleted successfully", success_message_model)
    @api.response(401, "Invalid or missing API key", error_model)
    @api.response(404, "API key not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key
    def delete(self, key_id):
        """Delete a specific API key (soft delete - marks as inactive)."""
        # Find API key first, outside try block
        api_key = db.session.get(ApiKey, key_id)
        if not api_key:
            abort(404, error="API key not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            logger.info("API: Deleting API key %s", key_id)

            # Check if trying to delete the currently used key
            auth_key = request.headers.get("X-API-Key")
            if auth_key:
                current_key_hash = hashlib.sha256(auth_key.encode()).hexdigest()
                if api_key.key_hash == current_key_hash:
                    return {
                        "error": "Cannot delete the API key currently being used"
                    }, 400

            key_name = api_key.name
            api_key.is_active = False
            db.session.commit()

            return {"message": f"API key '{key_name}' deleted successfully"}

        except Exception as e:
            logger.error("Error deleting API key %s: %s", key_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500


@users_ns.route("/<int:user_id>/reset-password")
class UserResetPasswordResource(Resource):
    @api.doc("create_password_reset_token", security="apikey")
    @api.response(
        200, "Password reset link created successfully", success_message_model
    )
    @api.response(401, "Invalid or missing API key or session", error_model)
    @api.response(404, "User not found", error_model)
    @api.response(500, "Internal server error", error_model)
    @require_api_key_or_session
    def post(self, user_id):
        """Create a password reset token and link for a specific user.

        Returns a secure link that the user can use to reset their password.
        The link expires after 24 hours.
        Can be authenticated via API key (X-API-Key header) or admin session.
        """
        from app.services.password_reset import create_reset_token

        user = db.session.get(User, user_id)
        if not user:
            abort(404, error="User not found")
            return None  # Type narrowing: unreachable but helps type checker

        try:
            token = create_reset_token(user.id)
            if not token:
                return {"error": "Failed to create password reset token"}, 500

            # Generate the reset URL - always return just the path
            # The frontend will construct the full URL if needed
            reset_path = f"/reset/{token.code}"

            return {
                "message": f"Password reset link created for {user.username}",
                "code": token.code,
                "url": reset_path,
                "expires_at": token.expires_at.isoformat(),
            }

        except Exception as e:
            logger.error("Error creating reset token for user %s: %s", user_id, str(e))
            logger.error(traceback.format_exc())
            return {"error": "Internal server error"}, 500
