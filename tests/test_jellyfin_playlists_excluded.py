"""Jellyfin's Playlists library must never be granted to a provisioned account,
and the admin's per-library choice must survive a re-scan.

Two independent guarantees:

1. `_set_specific_folders` filters the Playlists folder out of `EnabledFolders`,
   matching on `CollectionType` rather than on the display name — library names
   are admin-chosen and usually localised, so a name match would break on
   exactly the servers that need this.
2. Scanning libraries no longer resets `Library.enabled`. The startup scan runs
   on every boot, so resetting it there silently undid the admin's choice on
   every container restart.
"""

from unittest.mock import patch

from app.models import AdminAccount, Library, MediaServer
from app.services.media.jellyfin import JellyfinClient

# Shape mirrors /Library/MediaFolders: BaseItemDto carries CollectionType.
MEDIA_FOLDERS = [
    {"Id": "id-peliculas", "Name": "Peliculas", "CollectionType": "movies"},
    {"Id": "id-series", "Name": "Series", "CollectionType": "tvshows"},
    {"Id": "id-playlists", "Name": "Playlists", "CollectionType": "playlists"},
]


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _folder_client(media_folders=None):
    """JellyfinClient with only the HTTP layer faked; _set_specific_folders is real."""
    client = object.__new__(JellyfinClient)
    client.policy_updates = []

    items = MEDIA_FOLDERS if media_folders is None else media_folders

    def get(endpoint, **kwargs):
        if endpoint == "/Library/MediaFolders":
            return _Response({"Items": items})
        return _Response({"Policy": {}})

    client.get = get
    client.set_policy = lambda user_id, policy: client.policy_updates.append(policy)
    return client


def _apply(client, names):
    client._set_specific_folders("jf-user-1", names)
    assert len(client.policy_updates) == 1
    return client.policy_updates[0]


def test_playlists_is_dropped_from_enabled_folders():
    client = _folder_client()
    policy = _apply(client, ["Peliculas", "Series", "Playlists"])

    assert policy["EnabledFolders"] == ["id-peliculas", "id-series"]
    assert policy["EnableAllFolders"] is False


def test_playlists_is_matched_by_collection_type_not_by_name():
    """The decisive test: names are localised, CollectionType is not."""
    client = _folder_client(
        [
            # Spanish-localised playlists folder — must still be excluded.
            {
                "Id": "id-listas",
                "Name": "Listas de reproduccion",
                "CollectionType": "playlists",
            },
            # Named "Playlists" but is an ordinary movie library — must be kept.
            {"Id": "id-decoy", "Name": "Playlists", "CollectionType": "movies"},
        ]
    )
    policy = _apply(client, ["Listas de reproduccion", "Playlists"])

    assert policy["EnabledFolders"] == ["id-decoy"]


def test_playlists_only_request_grants_nothing_rather_than_everything():
    """EnableAllFolders is `not folder_ids`, so an empty list would grant all."""
    client = _folder_client()
    policy = _apply(client, ["Playlists"])

    assert policy["EnabledFolders"] == []
    assert policy["EnableAllFolders"] is False


def test_folders_without_collection_type_are_untouched():
    """A null CollectionType must not be mistaken for a playlists folder."""
    client = _folder_client(
        [{"Id": "id-mixed", "Name": "Mixed", "CollectionType": None}]
    )
    policy = _apply(client, ["Mixed"])

    assert policy["EnabledFolders"] == ["id-mixed"]


def _jellyfin_server(session, name="JF"):
    server = MediaServer(
        name=name, server_type="jellyfin", url="http://jelly.local", api_key="jf-key"
    )
    session.add(server)
    session.commit()
    return server


def _disabled_playlists_library(session, server):
    lib = Library(
        external_id="id-playlists",
        name="Playlists",
        server_id=server.id,
        enabled=False,
    )
    session.add(lib)
    session.commit()
    return lib


class _LibraryListingClient:
    def libraries(self):
        return {"id-playlists": "Playlists", "id-peliculas": "Peliculas"}


def test_startup_scan_preserves_a_disabled_library(app, session):
    """This scan runs on every boot — it used to re-enable everything."""
    from app.services.library_scanner import scan_all_server_libraries

    server = _jellyfin_server(session)
    _disabled_playlists_library(session, server)

    with patch(
        "app.services.media.service.get_client_for_media_server",
        return_value=_LibraryListingClient(),
    ):
        scan_all_server_libraries(show_logs=False)

    playlists = Library.query.filter_by(
        external_id="id-playlists", server_id=server.id
    ).one()
    assert playlists.enabled is False
    # New libraries still arrive enabled.
    peliculas = Library.query.filter_by(
        external_id="id-peliculas", server_id=server.id
    ).one()
    assert peliculas.enabled is True


def test_scan_libraries_button_preserves_a_disabled_library(client, session):
    """The "Scan Libraries" button sitting directly above the checkbox list."""
    admin = AdminAccount(username="testadmin")
    admin.set_password("TestPass123")
    session.add(admin)
    session.commit()
    login = client.post(
        "/login", data={"username": "testadmin", "password": "TestPass123"}
    )
    assert login.status_code in {200, 302, 303}

    server = _jellyfin_server(session, name="JF-Route")
    _disabled_playlists_library(session, server)

    with patch(
        "app.blueprints.media_servers.routes.scan_libraries_for_server",
        return_value={"id-playlists": "Playlists", "id-peliculas": "Peliculas"},
    ):
        response = client.post(f"/settings/servers/{server.id}/scan-libraries")

    assert response.status_code == 200
    playlists = Library.query.filter_by(
        external_id="id-playlists", server_id=server.id
    ).one()
    assert playlists.enabled is False
