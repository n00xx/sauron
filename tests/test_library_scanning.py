import hashlib
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import AdminAccount, ApiKey, Library, MediaServer, Settings


@pytest.fixture
def api_key(app, session):
    """Create a test API key."""
    with app.app_context():
        # Create admin account if it doesn't exist
        admin = AdminAccount.query.filter_by(username="testadmin").first()
        if not admin:
            admin = AdminAccount(username="testadmin")
            admin.set_password("testpass")
            db.session.add(admin)

        # Create admin_username setting (required by middleware)
        admin_setting = Settings.query.filter_by(key="admin_username").first()
        if not admin_setting:
            admin_setting = Settings(key="admin_username", value="testadmin")
            db.session.add(admin_setting)

        db.session.commit()

        raw_key = "test_api_key_12345"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        # Check if API key already exists
        existing_key = ApiKey.query.filter_by(key_hash=key_hash).first()
        if not existing_key:
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
def test_server(app, session):
    """Create a test media server."""
    with app.app_context():
        # Create admin account if it doesn't exist
        admin = AdminAccount.query.filter_by(username="testadmin").first()
        if not admin:
            admin = AdminAccount(username="testadmin")
            admin.set_password("testpass")
            db.session.add(admin)

        # Create admin_username setting (required by middleware)
        admin_setting = Settings.query.filter_by(key="admin_username").first()
        if not admin_setting:
            admin_setting = Settings(key="admin_username", value="testadmin")
            db.session.add(admin_setting)

        server = MediaServer(
            name="Test Server",
            server_type="jellyfin",
            url="http://localhost:8096",
            api_key="test_api_key",
            verified=True,
        )
        db.session.add(server)
        db.session.commit()
        return server


def test_api_libraries_without_existing_libraries(client, api_key, test_server):
    """Test that API libraries endpoint scans when no libraries exist."""

    # Clear any existing libraries first
    with client.application.app_context():
        Library.query.delete()
        db.session.commit()

    # Mock the scan_libraries_for_server function to return test data
    mock_libraries = {"lib1": "Movies", "lib2": "TV Shows", "lib3": "Music"}

    with patch("app.services.media.service.scan_libraries_for_server") as mock_scan:
        mock_scan.return_value = mock_libraries

        response = client.get("/api/libraries", headers={"X-API-Key": api_key})

        assert response.status_code == 200
        data = response.get_json()

        # Should have scanned and found the libraries
        assert "libraries" in data
        assert "count" in data
        # Should have at least the 3 libraries from our mock (may have more from scanning servers)
        assert data["count"] >= 3

        # Verify the library data structure
        libraries = data["libraries"]
        assert len(libraries) >= 3

        # Verify that scan was called at least once
        assert mock_scan.call_count >= 1


def test_api_libraries_with_existing_libraries(client, api_key, test_server):
    """Test that API libraries endpoint doesn't scan when libraries already exist."""

    # Clear any existing libraries first, then create specific test libraries
    with client.application.app_context():
        Library.query.delete()
        db.session.commit()

        # Re-query the server to get it in the current session context
        server = MediaServer.query.filter_by(name="Test Server").first()
        lib1 = Library(
            external_id="existing1", name="Existing Movies", server_id=server.id
        )
        lib2 = Library(external_id="existing2", name="Existing TV", server_id=server.id)
        db.session.add_all([lib1, lib2])
        db.session.commit()

    with patch("app.services.media.service.scan_libraries_for_server") as mock_scan:
        response = client.get("/api/libraries", headers={"X-API-Key": api_key})

        assert response.status_code == 200
        data = response.get_json()

        # Should return existing libraries without scanning
        assert "libraries" in data
        assert "count" in data
        assert data["count"] == 2

        # Verify that scan was NOT called since libraries already exist
        mock_scan.assert_not_called()


def test_api_libraries_scan_failure_continues(client, api_key, test_server):
    """Test that API libraries endpoint continues even if one server scan fails."""

    # Clear any existing libraries to ensure clean test
    with client.application.app_context():
        Library.query.delete()
        db.session.commit()

        # Clear existing servers to avoid conflicts
        MediaServer.query.filter(MediaServer.name.like("Test Server%")).delete()
        db.session.commit()

        # Create our two test servers fresh
        server1 = MediaServer(
            name="Test Server",
            server_type="jellyfin",
            url="http://localhost:8096",
            api_key="test_api_key",
            verified=True,
        )
        server2 = MediaServer(
            name="Test Server 2",
            server_type="plex",
            url="http://localhost:32400",
            api_key="test_api_key_2",
            verified=True,
        )
        db.session.add_all([server1, server2])
        db.session.commit()

    # Mock scan to fail for first server but succeed for second
    mock_libraries = {"lib1": "Movies"}

    def side_effect(server):
        if server.name == "Test Server":
            raise Exception("Connection failed")
        return mock_libraries

    with patch("app.services.media.service.scan_libraries_for_server") as mock_scan:
        mock_scan.side_effect = side_effect

        response = client.get("/api/libraries", headers={"X-API-Key": api_key})

        assert response.status_code == 200
        data = response.get_json()

        # Should succeed despite one server failing
        assert "libraries" in data
        assert "count" in data
        # Should have at least the library from the successful server
        assert data["count"] >= 1

        # Should have tried to scan servers
        assert mock_scan.call_count >= 2


# ---------------------------------------------------------------------------
# Startup scanner (scan_all_server_libraries)
#
# Distinct from the API rescan covered above: this is the pass that runs on
# every boot and is the only one that can DELETE or DISABLE rows. Before the
# guard added alongside these tests, an unreachable Jellyfin was reported by
# the client as "zero libraries", and the scanner acted on it — deleting the
# libraries no invitation referenced and disabling the rest, silently, with
# errors == [].
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner_server(app, session):
    """A jellyfin server with two libraries: one invited-to, one orphan."""
    from app.models import Invitation, invite_libraries

    with app.app_context():
        # Clear the association first: Library.query.delete() is a bulk delete
        # that bypasses the ORM cascade, so it would leave rows in
        # invite_library pointing at ids SQLite then hands back out to the
        # libraries created below — a unique-constraint collision that only
        # shows up when the full suite has already populated those tables.
        db.session.execute(invite_libraries.delete())
        Invitation.query.delete()
        Library.query.delete()
        MediaServer.query.delete()
        db.session.commit()

        server = MediaServer(
            name="Scanner Server",
            server_type="jellyfin",
            url="http://localhost:8096",
            api_key="scanner_key",
            verified=True,
        )
        db.session.add(server)
        db.session.commit()

        referenced = Library(
            external_id="ext-referenced",
            name="Peliculas",
            server_id=server.id,
            enabled=True,
        )
        orphan = Library(
            external_id="ext-orphan", name="Anime", server_id=server.id, enabled=True
        )
        db.session.add_all([referenced, orphan])
        db.session.commit()

        invitation = Invitation(code="SCAN0001")
        invitation.libraries.append(referenced)
        db.session.add(invitation)
        db.session.commit()

        return server.id


def _library_state(server_id):
    return {
        lib.external_id: lib.enabled
        for lib in Library.query.filter_by(server_id=server_id).all()
    }


def test_unreachable_jellyfin_does_not_destroy_libraries(app, scanner_server):
    """An unreachable media server must not delete or disable anything.

    Regression: a transient Jellyfin failure during startup used to wipe the
    library set permanently, which left checkout unable to grant anything.
    """
    from app.services.library_scanner import scan_all_server_libraries

    with app.app_context():
        before = _library_state(scanner_server)

        with patch(
            "app.services.media.jellyfin.JellyfinClient.get",
            side_effect=ConnectionError("Jellyfin unreachable"),
        ):
            _, errors = scan_all_server_libraries(show_logs=False)

        assert _library_state(scanner_server) == before, (
            "library rows must survive an unreachable media server"
        )
        assert errors, "an unreachable server must be reported, not swallowed"


def test_empty_library_response_skips_destructive_pass(app, scanner_server):
    """Zero libraries returned while rows exist is treated as suspect.

    Covers the clients that still swallow their own errors into ``{}`` (emby,
    komga, audiobookshelf): the guard lives in the scanner so it protects every
    server type, not just the one whose client was fixed.
    """
    from app.services.library_scanner import scan_all_server_libraries

    with app.app_context():
        before = _library_state(scanner_server)

        with patch(
            "app.services.media.jellyfin.JellyfinClient.libraries", return_value={}
        ):
            _, errors = scan_all_server_libraries(show_logs=False)

        assert _library_state(scanner_server) == before
        assert errors, "an empty response against existing rows must be reported"


def test_genuine_removal_still_disables_and_deletes(app, scanner_server):
    """The destructive pass must still run when the server answers normally.

    The guard keys on an *empty* response, not on any shrinkage: an admin who
    really removes a library still expects it to disappear.
    """
    from app.services.library_scanner import scan_all_server_libraries

    with app.app_context():
        with patch(
            "app.services.media.jellyfin.JellyfinClient.libraries",
            return_value={"ext-referenced": "Peliculas"},
        ):
            scan_all_server_libraries(show_logs=False)

        state = _library_state(scanner_server)
        # Orphan had no invitation pointing at it, so it is safe to remove.
        assert "ext-orphan" not in state
        # The invited-to one is preserved as disabled to keep the association.
        assert state == {"ext-referenced": True}


def test_scan_failure_notifies_operators(app, scanner_server):
    """A failed scan raises an operational alert."""
    from app.services.library_scanner import scan_all_server_libraries

    with app.app_context():
        with (
            patch(
                "app.services.media.jellyfin.JellyfinClient.libraries",
                return_value={},
            ),
            patch("app.services.notifications.notify") as mock_notify,
        ):
            scan_all_server_libraries(show_logs=False)

        assert mock_notify.called
        assert mock_notify.call_args.kwargs["event_type"] == "library_scan_failed"


def test_notification_failure_is_not_reported_as_scan_failure(app, scanner_server):
    """A broken notification agent must not masquerade as a scan error.

    This runs during startup inside the per-server error handler, so an
    exception escaping the notify call would be appended to ``errors``.
    """
    from app.services.library_scanner import scan_all_server_libraries

    with app.app_context():
        with (
            patch(
                "app.services.media.jellyfin.JellyfinClient.libraries",
                return_value={"ext-referenced": "Peliculas"},
            ),
            patch(
                "app.services.notifications.notify",
                side_effect=RuntimeError("agent down"),
            ),
        ):
            _, errors = scan_all_server_libraries(show_logs=False)

        assert errors == []
