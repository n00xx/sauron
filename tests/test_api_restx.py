"""Flask-RESTX API endpoint tests for Wizarr.

This test suite is designed specifically for Flask-RESTX APIs and handles:
- Proper Content-Type headers for JSON requests
- Flask-RESTX error response structures
- OpenAPI schema validation
"""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from app import create_app
from app.config import BaseConfig
from app.extensions import db
from app.models import AdminAccount, ApiKey, Invitation, Library, MediaServer, User


class TestConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


@pytest.fixture(scope="session")
def app():
    """Create application for testing."""
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        # Create a test admin account
        admin = AdminAccount(username="testadmin")
        admin.set_password("testpass")
        db.session.add(admin)
        db.session.commit()

    yield app
    with app.app_context():
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def api_key(app):
    """Create a test API key."""
    with app.app_context():
        admin = AdminAccount.query.first()
        raw_key = "test_api_key_12345"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        # Check if API key already exists
        existing_key = ApiKey.query.filter_by(key_hash=key_hash).first()
        if existing_key:
            return raw_key

        api_key = ApiKey(
            name="Test API Key",
            key_hash=key_hash,
            created_by_id=admin.id,
            is_active=True,
        )
        db.session.add(api_key)
        db.session.commit()

        return raw_key


@pytest.fixture
def sample_data(app):
    """Create sample data for testing."""
    with app.app_context():
        # Clean up any existing data first - delete in correct order to respect foreign keys
        User.query.delete()
        Invitation.query.delete()
        Library.query.delete()
        MediaServer.query.delete()
        db.session.commit()

        # Create media server
        server = MediaServer(
            name="Test Plex Server",
            server_type="plex",
            url="http://localhost:32400",
            api_key="test_plex_key",
            verified=True,
        )
        db.session.add(server)
        db.session.flush()

        # Create library
        library = Library(external_id="1", name="Movies", server_id=server.id)
        db.session.add(library)
        db.session.flush()

        # Create user
        user = User(
            token="test_user_token",
            username="testuser",
            email="test@example.com",
            code="ABC123",
            expires=datetime.now(UTC) + timedelta(days=30),
            server_id=server.id,
        )
        db.session.add(user)
        db.session.flush()

        # Create invitation
        invitation = Invitation(
            code="INV123",
            expires=datetime.now(UTC) + timedelta(days=7),
            duration="30",
            unlimited=False,
            server_id=server.id,
        )
        db.session.add(invitation)

        db.session.commit()

        # Return IDs instead of objects to avoid session issues
        return {
            "server_id": server.id,
            "library_id": library.id,
            "user_id": user.id,
            "invitation_id": invitation.id,
        }


class TestAPIStatus:
    """Test the API status endpoint."""

    def test_status_without_key(self, client):
        """Test status endpoint without API key."""
        response = client.get("/api/status")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_status_with_invalid_key(self, client):
        """Test status endpoint with invalid API key."""
        response = client.get("/api/status", headers={"X-API-Key": "invalid_key"})
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_status_with_valid_key(self, client, api_key, sample_data):
        """Test status endpoint with valid API key."""
        response = client.get("/api/status", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "users" in data
        assert "invites" in data
        assert "pending" in data
        assert "expired" in data
        assert data["users"] == 1
        assert data["invites"] == 1
        assert data["pending"] == 1
        assert data["expired"] == 0


class TestAPIUsers:
    """Test the API users endpoints."""

    def test_list_users_unauthorized(self, client):
        """Test users list without authentication."""
        response = client.get("/api/users")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_users_success(self, client, api_key, sample_data):
        """Test successful users list."""
        response = client.get("/api/users", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "users" in data
        assert "count" in data
        assert data["count"] == len(data["users"])

        # Check user data structure
        if data["users"]:
            user = data["users"][0]
            assert "id" in user
            assert "username" in user
            assert "email" in user
            assert "server" in user
            assert "server_type" in user

    def test_delete_user_unauthorized(self, client, sample_data):
        """Test user deletion without authentication."""
        response = client.delete(f"/api/users/{sample_data['user_id']}")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_delete_user_not_found(self, client, api_key):
        """Test deletion of non-existent user."""
        response = client.delete("/api/users/99999", headers={"X-API-Key": api_key})
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "User not found"

    def test_extend_user_expiry_unauthorized(self, client, sample_data):
        """Test user expiry extension without authentication."""
        response = client.post(
            f"/api/users/{sample_data['user_id']}/extend",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"days": 30}),
        )
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_extend_user_expiry_success(self, client, api_key, sample_data):
        """Test successful user expiry extension."""
        data = {"days": 15}
        response = client.post(
            f"/api/users/{sample_data['user_id']}/extend",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(data),
        )
        assert response.status_code == 200

        response_data = response.get_json()
        assert "message" in response_data
        assert "new_expiry" in response_data

    def test_extend_user_expiry_not_found(self, client, api_key):
        """Test expiry extension for non-existent user."""
        data = {"days": 15}
        response = client.post(
            "/api/users/99999/extend",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(data),
        )
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "User not found"


class TestAPIAdmins:
    """Test the API admins endpoints."""

    def test_list_admins_unauthorized(self, client):
        """Test admins list without authentication."""
        response = client.get("/api/admins")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_admins_success(self, client, api_key, sample_data):
        """Test successful admins list."""
        response = client.get("/api/admins", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "admins" in data
        assert "count" in data
        assert data["count"] == len(data["admins"])

        # Check admin data structure
        if data["admins"]:
            admin = data["admins"][0]
            assert "id" in admin
            assert "username" in admin
            assert "passkeys" in admin
            assert "created" in admin


class TestAPIInvitations:
    """Test the API invitations endpoints."""

    def test_list_invitations_unauthorized(self, client):
        """Test invitations list without authentication."""
        response = client.get("/api/invitations")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_invitations_success(self, client, api_key, sample_data):
        """Test successful invitations list."""
        response = client.get("/api/invitations", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "invitations" in data
        assert "count" in data
        assert data["count"] == len(data["invitations"])

        # Check invitation data structure
        if data["invitations"]:
            invitation = data["invitations"][0]
            assert "id" in invitation
            assert "code" in invitation
            assert "status" in invitation
            assert "url" in invitation
            assert invitation["status"] == "pending"

    def test_list_invitations_returns_relative_url_for_loopback_host(
        self, client, api_key, sample_data
    ):
        """Test invitation URLs do not include internal loopback hosts."""
        response = client.get(
            "/api/invitations",
            headers={"X-API-Key": api_key, "Host": "127.0.0.1:5690"},
        )
        assert response.status_code == 200

        invitation = response.get_json()["invitations"][0]
        assert invitation["url"] == f"/j/{invitation['code']}"

    def test_create_invitation_unauthorized(self, client):
        """Test invitation creation without authentication."""
        response = client.post(
            "/api/invitations",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"server_ids": [1]}),
        )
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_create_invitation_requires_server_ids(self, client, api_key, sample_data):
        """Test that server_ids is required for invitation creation."""
        data = {
            "expires_in_days": 7,
            "duration": "30",
            "unlimited": False,
            "library_ids": [sample_data["library_id"]],
        }

        response = client.post(
            "/api/invitations",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(data),
        )
        assert response.status_code == 400

        response_data = response.get_json()
        # Our custom error response structure
        assert "error" in response_data
        assert "Server selection is required" in response_data["error"]
        assert "available_servers" in response_data

    def test_delete_invitation_unauthorized(self, client, sample_data):
        """Test invitation deletion without authentication."""
        response = client.delete(f"/api/invitations/{sample_data['invitation_id']}")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_delete_invitation_success(self, client, api_key, sample_data):
        """Test successful invitation deletion."""
        response = client.delete(
            f"/api/invitations/{sample_data['invitation_id']}",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200

        data = response.get_json()
        assert "message" in data
        assert "INV123" in data["message"]

    def test_delete_invitation_not_found(self, client, api_key):
        """Test deletion of non-existent invitation."""
        response = client.delete(
            "/api/invitations/99999", headers={"X-API-Key": api_key}
        )
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "Invitation not found"


class TestAPIMaxActiveSessions:
    """sauron fork: POST /api/invitations must accept & persist max_active_sessions.

    Upstream Wizarr drops this field in the REST create handler, so the device
    limit could only be set from the web form. These tests lock in the fork's
    passthrough end-to-end (request -> create_invite -> Invitation -> GET echo).
    """

    def _create(self, client, api_key, server_id, payload_extra):
        body = {"server_ids": [server_id], "duration": "30", "unlimited": False}
        body.update(payload_extra)
        return client.post(
            "/api/invitations",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(body),
        )

    def test_create_persists_and_echoes_max_active_sessions(
        self, client, api_key, sample_data
    ):
        """Creating with max_active_sessions=2 stores it and echoes it back."""
        response = self._create(
            client, api_key, sample_data["server_id"], {"max_active_sessions": 2}
        )
        assert response.status_code == 201, response.get_json()

        invitation = response.get_json()["invitation"]
        assert invitation["max_active_sessions"] == 2

        # And it is persisted / echoed by the GET list endpoint.
        listing = client.get("/api/invitations", headers={"X-API-Key": api_key})
        match = next(
            inv
            for inv in listing.get_json()["invitations"]
            if inv["code"] == invitation["code"]
        )
        assert match["max_active_sessions"] == 2

    def test_integer_payload_does_not_500(self, client, api_key, sample_data):
        """Regression: create_invite calls .strip() on the value.

        A raw JSON integer (not a string) must not crash the endpoint; the fork
        coerces at the API boundary. Without the coercion this returns 500.
        """
        response = self._create(
            client, api_key, sample_data["server_id"], {"max_active_sessions": 4}
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["invitation"]["max_active_sessions"] == 4

    def test_zero_means_unlimited_and_is_preserved(self, client, api_key, sample_data):
        """max_active_sessions=0 (Jellyfin 'unlimited') is stored as 0, not None."""
        response = self._create(
            client, api_key, sample_data["server_id"], {"max_active_sessions": 0}
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["invitation"]["max_active_sessions"] == 0

    def test_omitted_field_stays_none(self, client, api_key, sample_data):
        """Backward compatible: omitting the field leaves it unset (None)."""
        response = self._create(client, api_key, sample_data["server_id"], {})
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["invitation"]["max_active_sessions"] is None


class TestAPILibraries:
    """Test the API libraries endpoints."""

    def test_list_libraries_unauthorized(self, client):
        """Test libraries list without authentication."""
        response = client.get("/api/libraries")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_libraries_success(self, client, api_key, sample_data):
        """Test successful libraries list."""
        response = client.get("/api/libraries", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "libraries" in data
        assert "count" in data
        assert data["count"] == len(data["libraries"])

        # Check library data structure
        if data["libraries"]:
            library = data["libraries"][0]
            assert "id" in library
            assert "name" in library
            assert "external_id" in library
            assert "server_name" in library


class TestAPIServers:
    """Test the API servers endpoints."""

    def test_list_servers_unauthorized(self, client):
        """Test servers list without authentication."""
        response = client.get("/api/servers")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_servers_success(self, client, api_key, sample_data):
        """Test successful servers list."""
        response = client.get("/api/servers", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "servers" in data
        assert "count" in data
        assert data["count"] == len(data["servers"])

        # Check server data structure
        if data["servers"]:
            server = data["servers"][0]
            assert "id" in server
            assert "name" in server
            assert "server_type" in server
            assert "server_url" in server
            assert "verified" in server


class TestAPIKeyManagement:
    """Test the API key management endpoints."""

    def test_list_api_keys_unauthorized(self, client):
        """Test API keys list without authentication."""
        response = client.get("/api/api-keys")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_list_api_keys_success(self, client, api_key):
        """Test successful API keys list."""
        response = client.get("/api/api-keys", headers={"X-API-Key": api_key})
        assert response.status_code == 200

        data = response.get_json()
        assert "api_keys" in data
        assert "count" in data
        assert data["count"] == len(data["api_keys"])
        assert data["count"] >= 1  # At least our test key should be there

        # Check API key data structure
        if data["api_keys"]:
            api_key_data = data["api_keys"][0]
            assert "id" in api_key_data
            assert "name" in api_key_data
            assert "created_at" in api_key_data
            assert "last_used_at" in api_key_data

    def test_delete_api_key_unauthorized(self, client):
        """Test API key deletion without authentication."""
        response = client.delete("/api/api-keys/1")
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"

    def test_delete_api_key_not_found(self, client, api_key):
        """Test deletion of non-existent API key."""
        response = client.delete("/api/api-keys/99999", headers={"X-API-Key": api_key})
        assert response.status_code == 404
        data = response.get_json()
        assert data["error"] == "API key not found"


class TestAPIErrorHandling:
    """Test API error handling scenarios."""

    def test_api_key_authentication_with_inactive_key(self, app, client):
        """Test that inactive API keys are rejected."""
        with app.app_context():
            # Create an inactive API key
            admin = AdminAccount.query.first()
            inactive_key = "inactive_test_key"
            key_hash = hashlib.sha256(inactive_key.encode()).hexdigest()

            api_key = ApiKey(
                name="Inactive Test Key",
                key_hash=key_hash,
                created_by_id=admin.id,
                is_active=False,  # Mark as inactive
            )
            db.session.add(api_key)
            db.session.commit()

        response = client.get("/api/status", headers={"X-API-Key": inactive_key})
        assert response.status_code == 401
        data = response.get_json()
        assert data["error"] == "Unauthorized"


class TestAPIInvitationDisableUsers:
    """POST /api/invitations/<id>/disable-users — chargeback/refund revocation.

        Resolves the invitation→users many-to-many mapping and disables every
        user who redeemed the invitation (falling back to delete when the media
        server can't disable, it is reported in `failed` and NEVER deleted - same
    semantics as POST /users/<id>/disable, which returns 502 instead).
    """

    def _link_user(self, app, sample_data):
        """Attach the sample user to the sample invitation via the M2M table."""
        with app.app_context():
            invitation = db.session.get(Invitation, sample_data["invitation_id"])
            user = db.session.get(User, sample_data["user_id"])
            invitation.users.append(user)
            db.session.commit()

    def test_unauthorized(self, client, sample_data):
        response = client.post(
            f"/api/invitations/{sample_data['invitation_id']}/disable-users"
        )
        assert response.status_code == 401

    def test_invitation_not_found(self, client, api_key):
        response = client.post(
            "/api/invitations/99999/disable-users",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "Invitation not found"

    def test_disables_redeemed_users(
        self, app, client, api_key, sample_data, monkeypatch
    ):
        self._link_user(app, sample_data)
        disabled_ids = []
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.disable_user",
            lambda db_id: disabled_ids.append(db_id) or True,
        )

        response = client.post(
            f"/api/invitations/{sample_data['invitation_id']}/disable-users",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 1
        assert data["users"][0]["user_id"] == sample_data["user_id"]
        assert data["users"][0]["action"] == "disabled"
        assert disabled_ids == [sample_data["user_id"]]

    def test_failed_disable_is_reported_never_deleted(
        self, app, client, api_key, sample_data, monkeypatch
    ):
        """Revoking access must not be able to destroy the account.

        Deletion is irreversible; the caller asked to DISABLE. A failure is
        reported in `failed` so the caller can alert a human, and `count` stays
        honest about how many were actually disabled.
        """
        self._link_user(app, sample_data)
        deleted_ids = []
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.disable_user", lambda db_id: False
        )
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.delete_user",
            lambda db_id: deleted_ids.append(db_id),
        )

        response = client.post(
            f"/api/invitations/{sample_data['invitation_id']}/disable-users",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert deleted_ids == [], "a failed disable must never delete"
        assert data["count"] == 0
        assert data["users"] == []
        assert data["failed"][0]["user_id"] == sample_data["user_id"]
        assert data["failed"][0]["action"] == "failed"

    def test_unredeemed_invitation_returns_zero(self, client, api_key, sample_data):
        response = client.post(
            f"/api/invitations/{sample_data['invitation_id']}/disable-users",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data == {"count": 0, "users": [], "failed": []}

    def test_legacy_used_by_fallback(
        self, app, client, api_key, sample_data, monkeypatch
    ):
        """Old rows only have used_by_id (deprecated) — still revocable."""
        with app.app_context():
            invitation = db.session.get(Invitation, sample_data["invitation_id"])
            invitation.used_by_id = sample_data["user_id"]
            db.session.commit()
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.disable_user", lambda db_id: True
        )

        response = client.post(
            f"/api/invitations/{sample_data['invitation_id']}/disable-users",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["count"] == 1
        assert data["users"][0]["user_id"] == sample_data["user_id"]


# ─────────────────────────────────────────────────────────────────────────────
# sauron fork: self-service renewal support
# ─────────────────────────────────────────────────────────────────────────────


# The "correct" secret the fake accepts. A module constant rather than an inline
# default so the linter does not read it as a hardcoded credential.
FAKE_VALID_SECRET = "correct-horse"


class FakeJellyfinClient:
    """Stands in for JellyfinClient, emulating the real server's semantics.

    The behaviours reproduced here are the ones the credential check has to
    survive, read from Jellyfin's own source
    (Jellyfin.Server.Implementations/Users/UserManager.cs):

    - A DISABLED account is rejected with 403 before the password is even
      checked, so it cannot prove ownership without being enabled first.
    - Failed logins increment a counter that PERSISTS, and once it reaches
      `lockout_after` the server disables the account itself.
    - A successful login resets that counter to zero.
    """

    def __init__(self, password=FAKE_VALID_SECRET, disabled=False, lockout_after=3):
        self.password = password
        self.disabled = disabled
        self.lockout_after = lockout_after
        self.failed_attempts = 0
        self.logged_out_tokens = []
        self.max_sessions = None
        self.auth_calls = 0
        self.on_authenticate = None

    def enable_user(self, user_id):
        self.disabled = False
        return True

    def disable_user(self, user_id):
        self.disabled = True
        return True

    def is_user_disabled(self, user_id):
        return self.disabled

    def authenticate_user(self, username, password):
        self.auth_calls += 1
        if self.on_authenticate:
            self.on_authenticate()

        if self.disabled:
            return False, 403, None
        if password != self.password:
            self.failed_attempts += 1
            if self.failed_attempts >= self.lockout_after:
                self.disabled = True
            return False, 401, None
        self.failed_attempts = 0
        return True, 200, "access-token"

    def logout_token(self, token):
        self.logged_out_tokens.append(token)

    def set_max_active_sessions(self, user_id, max_sessions):
        self.max_sessions = max_sessions
        return True


@pytest.fixture
def jellyfin_user(app):
    """A Jellyfin-backed media user — the shape a renewal actually targets."""
    with app.app_context():
        User.query.delete()
        Invitation.query.delete()
        Library.query.delete()
        MediaServer.query.delete()
        db.session.commit()

        server = MediaServer(
            name="Test Jellyfin",
            server_type="jellyfin",
            url="http://localhost:8096",
            api_key="test_jf_key",
            verified=True,
        )
        db.session.add(server)
        db.session.flush()

        user = User(
            token="jf-user-id",
            username="renewme",
            email="renew@example.com",
            code="CODE1",
            expires=datetime.now(UTC) + timedelta(days=5),
            server_id=server.id,
        )
        db.session.add(user)
        db.session.commit()
        return {"server_id": server.id, "user_id": user.id}


def _set_disabled(app, user_id, value):
    with app.app_context():
        user = db.session.get(User, user_id)
        user.is_disabled = value
        db.session.commit()


class TestAPIVerifyCredentials:
    """sauron fork: POST /api/users/verify-credentials.

    Ownership proof for a self-service renewal checkout — the buyer proves they
    own the account they are paying to renew, before any money moves.
    """

    ENDPOINT = "/api/users/verify-credentials"

    def _post(self, client, api_key, username, password):
        return client.post(
            self.ENDPOINT,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps({"username": username, "password": password}),
        )

    def _patch_client(self, monkeypatch, fake):
        monkeypatch.setattr(
            "app.services.credentials.get_client_for_media_server",
            lambda server: fake,
        )

    def test_requires_api_key(self, client):
        response = client.post(
            self.ENDPOINT,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"username": "x", "password": "y"}),
        )
        assert response.status_code == 401

    def test_correct_password_returns_user_id(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        fake = FakeJellyfinClient()
        self._patch_client(monkeypatch, fake)

        response = self._post(client, api_key, "renewme", FAKE_VALID_SECRET)

        assert response.status_code == 200
        assert response.get_json() == {
            "valid": True,
            "user_id": jellyfin_user["user_id"],
        }
        # The session opened purely to check the password must not be left open.
        assert fake.logged_out_tokens == ["access-token"]

    def test_username_is_case_insensitive(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """A buyer typing their username from memory must not fail on case."""
        self._patch_client(monkeypatch, FakeJellyfinClient())

        response = self._post(client, api_key, "  ReNewMe ", FAKE_VALID_SECRET)

        assert response.get_json()["valid"] is True

    def test_wrong_password_and_unknown_user_are_indistinguishable(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """The whole point of the endpoint: no user-enumeration oracle.

        Jellyfin itself answers 401 for a bad password and 403 for a disabled
        account. If any of that reaches the response, an attacker can map which
        usernames exist.
        """
        self._patch_client(monkeypatch, FakeJellyfinClient())
        wrong_password = self._post(client, api_key, "renewme", "nope")

        self._patch_client(monkeypatch, FakeJellyfinClient())
        unknown_user = self._post(client, api_key, "ghostaccount", "nope")

        assert wrong_password.status_code == unknown_user.status_code == 200
        assert wrong_password.get_json() == unknown_user.get_json()
        assert wrong_password.get_json() == {"valid": False, "user_id": None}

    def test_disabled_account_can_still_prove_ownership(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        """The core case: an EXPIRED (disabled) account is what gets renewed.

        Jellyfin refuses to authenticate a disabled user at all, so the check
        has to enable it for the duration and put it back afterwards.
        """
        _set_disabled(app, jellyfin_user["user_id"], True)
        fake = FakeJellyfinClient(disabled=True)
        self._patch_client(monkeypatch, fake)

        response = self._post(client, api_key, "renewme", FAKE_VALID_SECRET)

        assert response.get_json()["valid"] is True
        # Restored. Proving ownership is not paying — access comes later, from
        # fulfillment, once Stripe confirms the charge.
        assert fake.disabled is True

    def test_disabled_account_stays_disabled_on_wrong_password(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        _set_disabled(app, jellyfin_user["user_id"], True)
        fake = FakeJellyfinClient(disabled=True)
        self._patch_client(monkeypatch, fake)

        response = self._post(client, api_key, "renewme", "wrong")

        assert response.get_json()["valid"] is False
        assert fake.disabled is True

    def test_lockout_caused_by_this_check_is_undone(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """A public form must never become a way to disable a paying customer.

        Jellyfin disables the account once failed logins reach the lockout
        threshold. The check has to notice and reverse it.
        """
        fake = FakeJellyfinClient(lockout_after=1)  # trips on the first failure
        self._patch_client(monkeypatch, fake)

        response = self._post(client, api_key, "renewme", "wrong")

        assert response.get_json()["valid"] is False
        assert fake.disabled is False, "lockout was not reversed"

    def test_already_disabled_account_is_not_switched_on(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """Stale-column guard.

        If an admin disabled the account directly in Jellyfin, sauron's own
        `is_disabled` column still reads False. A failed check must NOT treat
        that as a lockout it caused and enable the account — that would hand
        anyone a way to switch on any disabled account.
        """
        fake = FakeJellyfinClient(disabled=True)  # DB column left at False
        self._patch_client(monkeypatch, fake)

        response = self._post(client, api_key, "renewme", "whatever")

        assert response.get_json()["valid"] is False
        assert fake.disabled is True, "an already-disabled account was enabled"

    def test_stale_column_is_corrected(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        """Having learned the account is really disabled, record it."""
        self._patch_client(monkeypatch, FakeJellyfinClient(disabled=True))

        self._post(client, api_key, "renewme", "whatever")

        with app.app_context():
            assert db.session.get(User, jellyfin_user["user_id"]).is_disabled is True

    def test_concurrent_checks_do_not_corrupt_account_state(
        self, app, jellyfin_user, monkeypatch
    ):
        """Two simultaneous checks on one disabled account.

        sauron runs a single gunicorn process with 8 THREADS, so this is real
        traffic, not a hypothetical. Without the per-account lock the two
        sequences interleave — one restores the account to disabled while the
        other is still authenticating, so a buyer with perfectly correct
        credentials is told they are wrong.
        """
        import threading
        from types import SimpleNamespace

        from app.services.credentials import verify_media_credentials

        fake = FakeJellyfinClient(disabled=True)
        # Widen the window between enable and restore so an unserialised
        # implementation reliably interleaves instead of passing by luck.
        fake.on_authenticate = lambda: time.sleep(0.05)
        monkeypatch.setattr(
            "app.services.credentials.get_client_for_media_server",
            lambda server: fake,
        )

        # The lookup is stubbed rather than hitting the DB: this test is about
        # serialising the enable→authenticate→restore sequence, and two threads
        # sharing one in-memory SQLite connection would fail on the harness
        # rather than on the behaviour under test.
        stub_user = SimpleNamespace(
            id=jellyfin_user["user_id"],
            username="renewme",
            token="jf-user-id",
            is_disabled=True,
            server=SimpleNamespace(server_type="jellyfin"),
        )
        monkeypatch.setattr(
            "app.services.credentials.find_user_by_username",
            lambda username: stub_user,
        )

        results = []

        def check():
            results.append(verify_media_credentials("renewme", FAKE_VALID_SECRET))

        threads = [threading.Thread(target=check) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results == [jellyfin_user["user_id"]] * 2, (
            "a concurrent check spuriously rejected valid credentials"
        )
        assert fake.disabled is True, "account left enabled after concurrent checks"

    def test_missing_fields_rejected(self, client, api_key):
        response = client.post(
            self.ENDPOINT,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps({"username": "renewme"}),
        )
        assert response.status_code == 400

    def test_per_username_attempts_are_capped(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        """The cap is the PRIMARY defense against Jellyfin's account lockout.

        Jellyfin disables an account once failed logins reach
        LoginAttemptsBeforeLockout, and its counter has no reset API, so how
        fast an attacker can drive it up is what matters most.

        conftest disables rate limiting for every test by default, so this one
        turns it back on — otherwise the endpoint would appear capped while
        actually running wide open. That failure mode is easy to hit: a
        @limiter.limit stacked on the method instead of the Resource's
        `decorators` registers against the wrong name and never fires.
        """
        from app.extensions import limiter

        self._patch_client(monkeypatch, FakeJellyfinClient())
        limiter.enabled = True
        try:
            limiter.reset()
            codes = [
                self._post(client, api_key, "capprobe", "wrong").status_code
                for _ in range(4)
            ]
        finally:
            limiter.reset()
            limiter.enabled = False

        assert codes[:3] == [200, 200, 200], codes
        assert codes[3] == 429, f"4th attempt was not rate limited: {codes}"

    def test_non_jellyfin_server_is_rejected_generically(
        self, client, api_key, sample_data
    ):
        """sample_data is a PLEX server: no way to check a user's own password.

        Must look exactly like any other failure.
        """
        response = self._post(client, api_key, "testuser", "anything")

        assert response.status_code == 200
        assert response.get_json() == {"valid": False, "user_id": None}


class TestAPIMaxSessions:
    """sauron fork: POST /api/users/<id>/max-sessions.

    Upstream applies the device limit only when an invitation is redeemed, so
    without this a renewal into a higher tier takes the money and leaves the
    old limit in place.
    """

    def _post(self, client, api_key, user_id, body):
        return client.post(
            f"/api/users/{user_id}/max-sessions",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(body),
        )

    def _patch_client(self, monkeypatch, fake):
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.get_client_for_media_server",
            lambda server: fake,
        )

    def test_applies_limit(self, client, api_key, jellyfin_user, monkeypatch):
        fake = FakeJellyfinClient()
        self._patch_client(monkeypatch, fake)

        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": 4}
        )

        assert response.status_code == 200, response.get_json()
        assert response.get_json()["max_active_sessions"] == 4
        assert fake.max_sessions == 4

    def test_zero_means_unlimited_and_is_allowed(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        fake = FakeJellyfinClient()
        self._patch_client(monkeypatch, fake)

        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": 0}
        )

        assert response.status_code == 200
        assert fake.max_sessions == 0

    def test_booleans_are_rejected(self, client, api_key, jellyfin_user, monkeypatch):
        """bool subclasses int in Python: True would be applied as a limit of 1."""
        self._patch_client(monkeypatch, FakeJellyfinClient())

        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": True}
        )

        assert response.status_code == 400

    def test_negative_rejected(self, client, api_key, jellyfin_user, monkeypatch):
        self._patch_client(monkeypatch, FakeJellyfinClient())
        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": -1}
        )
        assert response.status_code == 400

    def test_non_integer_rejected(self, client, api_key, jellyfin_user, monkeypatch):
        self._patch_client(monkeypatch, FakeJellyfinClient())
        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": "four"}
        )
        assert response.status_code == 400

    def test_unknown_user_404(self, client, api_key, jellyfin_user):
        response = self._post(client, api_key, 999999, {"max_active_sessions": 2})
        assert response.status_code == 404

    def test_server_refusal_is_502_not_200(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """A refused limit must never report success — that is a mis-sale."""
        fake = FakeJellyfinClient()
        fake.set_max_active_sessions = lambda user_id, n: False
        self._patch_client(monkeypatch, fake)

        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": 2}
        )

        assert response.status_code == 502

    def test_unsupported_server_type_is_502(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        class PlexLikeClient:
            pass

        self._patch_client(monkeypatch, PlexLikeClient())

        response = self._post(
            client, api_key, jellyfin_user["user_id"], {"max_active_sessions": 2}
        )

        assert response.status_code == 502


class TestAPIDisableUserNeverDeletes:
    """sauron fork: POST /api/users/<id>/disable must never escalate to deletion.

    Upstream deleted the account when the disable failed, and answered 200 as if
    it had done what was asked. For a paying customer that turns "revoke access"
    into unrecoverable data loss, and the caller cannot even tell: neexy's
    revocation path reads this endpoint's result as success.
    """

    def test_failed_disable_returns_502_and_keeps_the_account(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        deleted_ids = []
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.disable_user", lambda db_id: False
        )
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.delete_user",
            lambda db_id: deleted_ids.append(db_id),
        )

        response = client.post(
            f"/api/users/{jellyfin_user['user_id']}/disable",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 502
        assert deleted_ids == [], "a failed disable must never delete"
        with app.app_context():
            assert db.session.get(User, jellyfin_user["user_id"]) is not None

    def test_success_returns_200(self, client, api_key, jellyfin_user, monkeypatch):
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.disable_user", lambda db_id: True
        )

        response = client.post(
            f"/api/users/{jellyfin_user['user_id']}/disable",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200


class TestAPIEnableUserReportsFailure:
    """sauron fork: POST /api/users/<id>/enable must not report a failure as 200.

    Upstream answered 200 with a "Enable failed or not supported" message, so an
    API client could not tell a reactivated account from one still disabled — a
    paid renewal would look delivered while the buyer had no access.
    """

    def test_failure_returns_502(self, client, api_key, jellyfin_user, monkeypatch):
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.enable_user", lambda db_id: False
        )

        response = client.post(
            f"/api/users/{jellyfin_user['user_id']}/enable",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 502

    def test_success_returns_200(self, client, api_key, jellyfin_user, monkeypatch):
        monkeypatch.setattr(
            "app.blueprints.api.api_routes.enable_user", lambda db_id: True
        )

        response = client.post(
            f"/api/users/{jellyfin_user['user_id']}/enable",
            headers={"X-API-Key": api_key},
        )

        assert response.status_code == 200

    def test_success_clears_stale_expired_record(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        """A renewed customer must stop showing up under "expired users"."""
        from app.models import ExpiredUser

        with app.app_context():
            db.session.add(
                ExpiredUser(
                    original_user_id=jellyfin_user["user_id"],
                    username="renewme",
                    email="renew@example.com",
                    server_id=jellyfin_user["server_id"],
                    expired_at=datetime.now(UTC) - timedelta(days=1),
                    deleted_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            db.session.commit()

        monkeypatch.setattr(
            "app.blueprints.api.api_routes.enable_user", lambda db_id: True
        )
        client.post(
            f"/api/users/{jellyfin_user['user_id']}/enable",
            headers={"X-API-Key": api_key},
        )

        with app.app_context():
            remaining = ExpiredUser.query.filter_by(email="renew@example.com").count()
            assert remaining == 0


class TestAPIPasswordResetRequest:
    """sauron fork: POST /api/users/password-reset-request.

    The door the public "olvidé mi contraseña" form knocks on. Its entire job
    is to reach `send_password_reset_email` without telling the caller anything
    about which usernames exist.
    """

    ENDPOINT = "/api/users/password-reset-request"

    def _post(self, client, api_key, username):
        return client.post(
            self.ENDPOINT,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps({"username": username}),
        )

    def _spy(self, monkeypatch, ok=True, error_code=None):
        """Replace the sender and record who it was asked to mail."""
        from app.services.resend_email import SendResult

        calls = []

        def fake(user, **kwargs):
            calls.append(user.id)
            return SendResult(
                ok=ok,
                resend_id="re_test" if ok else None,
                error_code=error_code,
                error_message=None if ok else "nope",
            )

        monkeypatch.setattr("app.services.resend_email.send_password_reset_email", fake)
        return calls

    def test_requires_api_key(self, client):
        response = client.post(
            self.ENDPOINT,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"username": "renewme"}),
        )
        assert response.status_code == 401

    def test_known_username_triggers_the_email(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        calls = self._spy(monkeypatch)

        response = self._post(client, api_key, "renewme")

        assert response.status_code == 200
        assert response.get_json() == {"accepted": True}
        assert calls == [jellyfin_user["user_id"]]

    def test_username_is_matched_case_and_space_insensitively(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """Someone typing their username from memory must not fail on case."""
        calls = self._spy(monkeypatch)

        response = self._post(client, api_key, "  ReNewMe ")

        assert response.get_json() == {"accepted": True}
        assert calls == [jellyfin_user["user_id"]]

    def test_unknown_username_is_indistinguishable_from_a_hit(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """The whole point of the endpoint: no user-enumeration oracle."""
        calls = self._spy(monkeypatch)

        hit = self._post(client, api_key, "renewme")
        miss = self._post(client, api_key, "ghostaccount")

        assert hit.status_code == miss.status_code == 200
        assert hit.get_json() == miss.get_json() == {"accepted": True}
        # ...and nothing was mailed for the name that does not exist.
        assert calls == [jellyfin_user["user_id"]]

    def test_send_failure_is_indistinguishable_from_success(
        self, client, api_key, jellyfin_user, monkeypatch
    ):
        """An account with no email on file must not answer differently.

        This is the case that actually happens: legacy rows imported before an
        address was mandatory. `send_password_reset_email` reports `no_email`,
        and that report must reach the log and stop there.
        """
        self._spy(monkeypatch, ok=False, error_code="no_email")

        response = self._post(client, api_key, "renewme")

        assert response.status_code == 200
        assert response.get_json() == {"accepted": True}

    def test_unconfigured_resend_still_answers_the_same(
        self, client, api_key, jellyfin_user
    ):
        """No spy: the REAL sender runs with Resend switched off in tests.

        Proves the uniform answer survives the whole call stack, not just a
        stubbed boundary.
        """
        response = self._post(client, api_key, "renewme")

        assert response.status_code == 200
        assert response.get_json() == {"accepted": True}

    def test_ambiguous_username_mails_nobody(
        self, app, client, api_key, jellyfin_user, monkeypatch
    ):
        """Two servers, one username: refuse rather than reset a guess.

        A reset token belongs to ONE user row and changes the password on that
        server alone, so picking one would reset an account the requester may
        not have meant and silently leave the other alone.
        """
        with app.app_context():
            server = MediaServer(
                name="Second Jellyfin",
                server_type="jellyfin",
                url="http://jf2.local",
                api_key="k2",
            )
            db.session.add(server)
            db.session.flush()
            db.session.add(
                User(
                    token="jf-user-id-2",
                    username="renewme",
                    email="renew@example.com",
                    code="CODE2",
                    server_id=server.id,
                )
            )
            db.session.commit()

        calls = self._spy(monkeypatch)

        response = self._post(client, api_key, "renewme")

        assert response.status_code == 200
        assert response.get_json() == {"accepted": True}
        assert calls == [], "an ambiguous username must not mail anyone"

    @pytest.mark.parametrize("payload", [{}, {"username": None}, {"username": "   "}])
    def test_malformed_body_is_a_400(self, client, api_key, payload):
        """A caller bug, safe to report — it says nothing about any account."""
        response = client.post(
            self.ENDPOINT,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            data=json.dumps(payload),
        )

        assert response.status_code == 400
