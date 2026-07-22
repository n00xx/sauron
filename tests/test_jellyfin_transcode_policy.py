"""Jellyfin redemption (_do_join) must map the invitation transcode toggles
onto the Jellyfin user Policy.

This covers the invitation_flow / invitation_manager redemption path
(client.join -> _do_join), which is distinct from the password-prompt path
exercised in test_multiserver_media_policy.py. Asymmetric values guard against
a swapped or mistyped policy key.
"""

from app.extensions import db
from app.models import Invitation, MediaServer
from app.services.media.jellyfin import JellyfinClient


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _jellyfin_client(server_id):
    client = object.__new__(JellyfinClient)
    client.server_id = server_id
    client.policy_updates = []

    def create_user(username, password):
        return "jf-user-1"

    def get(endpoint):
        # _do_join reads the freshly created user's current policy
        return _Response({"Policy": {}})

    def set_policy(user_id, policy):
        client.policy_updates.append((user_id, policy))

    # Skip the heavy library + identity-linking machinery for this unit test.
    client.create_user = create_user
    client.get = get
    client.set_policy = set_policy
    client._set_specific_folders = lambda user_id, sections: None
    client._create_user_with_identity_linking = lambda payload: None
    return client


def test_do_join_maps_transcode_toggles_to_jellyfin_policy(client, session):
    server = MediaServer(
        name="JF",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="jf-key",
        allow_downloads=False,
        allow_live_tv=False,
    )
    invitation = Invitation(
        code="JFTRANS",
        used=False,
        unlimited=True,
        allow_downloads=False,
        allow_live_tv=False,
        allow_transcode_audio=True,
        allow_transcode_video=False,
    )
    invitation.servers = [server]
    db.session.add_all([server, invitation])
    db.session.commit()

    jf = _jellyfin_client(server.id)
    ok, msg = jf._do_join(
        username="viewer",
        password="password123",
        confirm="password123",
        email="viewer@example.com",
        code="JFTRANS",
    )

    assert ok is True, msg
    assert len(jf.policy_updates) == 1
    _, policy = jf.policy_updates[0]
    assert policy["EnableAudioPlaybackTranscoding"] is True
    assert policy["EnableVideoPlaybackTranscoding"] is False
