"""OpenAPI models for Flask-RESTX API documentation."""

from flask_restx import fields

from app.extensions import api

# Status Models
status_model = api.model(
    "Status",
    {
        "users": fields.Integer(description="Total number of users"),
        "invites": fields.Integer(description="Total number of invitations"),
        "pending": fields.Integer(description="Number of pending invitations"),
        "expired": fields.Integer(description="Number of expired invitations"),
    },
)

# Admin Models
admin_model = api.model(
    "Admin",
    {
        "id": fields.Integer(description="Admin ID"),
        "username": fields.String(description="Admin username"),
        "passkeys": fields.Integer(description="Number of passkeys for this admin"),
        "created": fields.DateTime(description="Creation date (ISO format)"),
    },
)

admin_list_model = api.model(
    "AdminList",
    {
        "admins": fields.List(fields.Nested(admin_model)),
        "count": fields.Integer(description="Total number of admins"),
    },
)

# User Models
user_model = api.model(
    "User",
    {
        "id": fields.Integer(description="User ID"),
        "username": fields.String(description="Username"),
        "email": fields.String(description="Email address"),
        "server": fields.String(description="Media server name"),
        "server_type": fields.String(
            description="Type of media server (plex, jellyfin, etc.)"
        ),
        "expires": fields.DateTime(description="Expiration date (ISO format)"),
        "created_at": fields.DateTime(description="Creation date (ISO format)"),
    },
)

user_list_model = api.model(
    "UserList",
    {
        "users": fields.List(fields.Nested(user_model)),
        "count": fields.Integer(description="Total number of users"),
    },
)

user_extend_request = api.model(
    "UserExtendRequest",
    {
        "days": fields.Integer(
            description="Number of days to extend (default: 30)", default=30
        ),
    },
)

user_extend_response = api.model(
    "UserExtendResponse",
    {
        "message": fields.String(description="Success message"),
        "new_expiry": fields.DateTime(description="New expiration date"),
        "reactivated": fields.Boolean(
            description=(
                "True when the account was disabled and this renewal turned it "
                "back on. False when it was already active."
            )
        ),
    },
)

user_update_expiry_request = api.model(
    "UserUpdateExpiryRequest",
    {
        "expires": fields.DateTime(
            description="New expiration date (ISO format). Use null for unlimited access.",
            required=False,
            allow_null=True,
        ),
    },
)

user_update_expiry_response = api.model(
    "UserUpdateExpiryResponse",
    {
        "message": fields.String(description="Success message"),
        "new_expiry": fields.DateTime(
            description="New expiration date (null for unlimited)",
            allow_null=True,
        ),
    },
)

user_verify_credentials_request = api.model(
    "UserVerifyCredentialsRequest",
    {
        "username": fields.String(required=True, description="Media account username"),
        "password": fields.String(
            required=True, description="The media account's own password"
        ),
    },
)

user_verify_credentials_response = api.model(
    "UserVerifyCredentialsResponse",
    {
        "valid": fields.Boolean(
            description="True only when the username and password both match"
        ),
        "user_id": fields.Integer(
            description="Wizarr user id, present only when valid is true",
            allow_null=True,
        ),
    },
)

user_password_reset_request = api.model(
    "UserPasswordResetRequest",
    {
        "username": fields.String(
            required=True,
            description="Media account username; matched case-insensitively",
        ),
    },
)

user_password_reset_response = api.model(
    "UserPasswordResetResponse",
    {
        "accepted": fields.Boolean(
            description=(
                "Always true. Says the request was received, NOT that an email "
                "was sent — reporting that would leak which usernames exist."
            )
        ),
    },
)

user_max_sessions_request = api.model(
    "UserMaxSessionsRequest",
    {
        "max_active_sessions": fields.Integer(
            required=True,
            description="Jellyfin simultaneous stream limit; 0 = unlimited",
        ),
    },
)

user_max_sessions_response = api.model(
    "UserMaxSessionsResponse",
    {
        "message": fields.String(description="Success message"),
        "max_active_sessions": fields.Integer(description="The limit that was applied"),
    },
)

# Invitation Models
invitation_model = api.model(
    "Invitation",
    {
        "id": fields.Integer(description="Invitation ID"),
        "code": fields.String(description="Invitation code"),
        "url": fields.String(description="Ready-to-use invitation URL"),
        "status": fields.String(
            description="Invitation status", enum=["pending", "used", "expired"]
        ),
        "created": fields.DateTime(description="Creation date (ISO format)"),
        "expires": fields.DateTime(description="Expiration date (ISO format)"),
        "used_at": fields.DateTime(description="Date when invitation was used"),
        "used_by": fields.String(description="Username who used the invitation"),
        "duration": fields.String(
            description='User access duration in days or "unlimited"'
        ),
        "unlimited": fields.Boolean(description="Whether user access is unlimited"),
        "specific_libraries": fields.List(
            fields.Integer, description="Specific library IDs if restricted"
        ),
        "display_name": fields.String(description="Display name for the invitation"),
        "server_names": fields.List(fields.String, description="List of server names"),
        "uses_global_setting": fields.Boolean(
            description="Whether display name comes from global setting"
        ),
        "max_active_sessions": fields.Integer(
            description="Jellyfin max simultaneous streams; 0 = unlimited, null = not set",
            allow_null=True,
        ),
    },
)

invitation_list_model = api.model(
    "InvitationList",
    {
        "invitations": fields.List(fields.Nested(invitation_model)),
        "count": fields.Integer(description="Total number of invitations"),
    },
)

invitation_create_request = api.model(
    "InvitationCreateRequest",
    {
        "server_ids": fields.List(
            fields.Integer,
            required=False,
            description="Array of server IDs (required, but validated by API for better error messages)",
        ),
        "expires_in_days": fields.Integer(
            description="Days until invitation expires (1, 7, 30, or null)",
            enum=[1, 7, 30],
        ),
        "duration": fields.String(
            description='User access duration in days or "unlimited"',
            default="unlimited",
        ),
        "unlimited": fields.Boolean(
            description="Whether user access is unlimited", default=True
        ),
        "library_ids": fields.List(
            fields.Integer, description="Array of library IDs to grant access to"
        ),
        "allow_downloads": fields.Boolean(
            description="Allow user downloads", default=False
        ),
        "allow_live_tv": fields.Boolean(
            description="Allow live TV access", default=False
        ),
        "allow_mobile_uploads": fields.Boolean(
            description="Allow mobile uploads", default=False
        ),
        "allow_transcode_audio": fields.Boolean(
            description="Allow audio playback that requires transcoding (Jellyfin)",
            default=False,
        ),
        "allow_transcode_video": fields.Boolean(
            description="Allow video playback that requires transcoding (Jellyfin)",
            default=False,
        ),
        "wizard_bundle_id": fields.Integer(
            required=False,
            description="Wizard bundle ID to use for this invitation (omit for automatic selection)",
        ),
        "max_active_sessions": fields.Integer(
            required=False,
            description="Jellyfin max simultaneous streams for invited users; 0 = unlimited (Jellyfin servers only)",
        ),
    },
)

invitation_create_response = api.model(
    "InvitationCreateResponse",
    {
        "message": fields.String(description="Success message"),
        "invitation": fields.Nested(invitation_model),
    },
)

# Library Models
library_model = api.model(
    "Library",
    {
        "id": fields.Integer(description="Library ID"),
        "name": fields.String(description="Library name"),
        "external_id": fields.String(
            description="External library ID from media server"
        ),
        "server_id": fields.Integer(description="Server ID this library belongs to"),
        "server_name": fields.String(description="Server name this library belongs to"),
        "enabled": fields.Boolean(description="Whether this library is enabled"),
    },
)

library_list_model = api.model(
    "LibraryList",
    {
        "libraries": fields.List(fields.Nested(library_model)),
        "count": fields.Integer(description="Total number of libraries"),
    },
)

# Server Models
server_model = api.model(
    "Server",
    {
        "id": fields.Integer(description="Server ID"),
        "name": fields.String(description="Server name"),
        "server_type": fields.String(
            description="Server type (plex, jellyfin, emby, etc.)"
        ),
        "server_url": fields.String(description="Internal server URL"),
        "external_url": fields.String(description="External server URL"),
        "verified": fields.Boolean(description="Whether server connection is verified"),
        "allow_downloads": fields.Boolean(description="Whether downloads are allowed"),
        "allow_live_tv": fields.Boolean(description="Whether live TV is allowed"),
        "allow_mobile_uploads": fields.Boolean(
            description="Whether mobile uploads are allowed"
        ),
        "created_at": fields.DateTime(description="Server creation date"),
    },
)

server_list_model = api.model(
    "ServerList",
    {
        "servers": fields.List(fields.Nested(server_model)),
        "count": fields.Integer(description="Total number of servers"),
    },
)

# API Key Models
api_key_model = api.model(
    "ApiKey",
    {
        "id": fields.Integer(description="API key ID"),
        "name": fields.String(description="API key name"),
        "created_at": fields.DateTime(description="Creation date"),
        "last_used_at": fields.DateTime(description="Last used date"),
        "created_by": fields.String(description="Username who created the key"),
    },
)

api_key_list_model = api.model(
    "ApiKeyList",
    {
        "api_keys": fields.List(fields.Nested(api_key_model)),
        "count": fields.Integer(description="Total number of API keys"),
    },
)

# Error Models
error_model = api.model(
    "Error",
    {
        "error": fields.String(description="Error message"),
    },
)

success_message_model = api.model(
    "SuccessMessage",
    {
        "message": fields.String(description="Success message"),
    },
)
