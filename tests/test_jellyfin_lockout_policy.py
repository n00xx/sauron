"""A new Jellyfin account tolerates five wrong passwords before it locks.

Jellyfin disables an account once ``InvalidLoginAttemptCount`` reaches the
policy's ``LoginAttemptsBeforeLockout``. Sauron used to leave that to the
server, which gave customers three tries — spent quickly when typing a password
on a TV remote, after which the account is disabled and only an admin can bring
it back. The join sets the threshold explicitly.
"""

from app.extensions import db
from app.models import Invitation, MediaServer
from app.services.media.jellyfin import (
    LOGIN_ATTEMPTS_BEFORE_LOCKOUT,
    JellyfinClient,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _jellyfin_client(server_id, current_policy):
    client = object.__new__(JellyfinClient)
    client.server_id = server_id
    client.policy_updates = []

    client.create_user = lambda username, password: "jf-user-1"
    # _do_join reads the freshly created user's current policy
    client.get = lambda endpoint: _Response({"Policy": dict(current_policy)})
    client.set_policy = lambda user_id, policy: client.policy_updates.append(
        (user_id, policy)
    )
    # Skip the heavy library + identity-linking machinery for this unit test.
    client._set_specific_folders = lambda user_id, sections: None
    client._create_user_with_identity_linking = lambda payload: None
    return client


def _join(session, current_policy):
    server = MediaServer(
        name="JF",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="jf-key",
    )
    invitation = Invitation(code="JFLOCK", used=False, unlimited=True)
    invitation.servers = [server]
    db.session.add_all([server, invitation])
    db.session.commit()

    jf = _jellyfin_client(server.id, current_policy)
    ok, msg = jf._do_join(
        username="viewer01",
        password="Password123",
        confirm="Password123",
        email="viewer@example.com",
        code="JFLOCK",
    )
    assert ok is True, msg
    assert len(jf.policy_updates) == 1
    return jf.policy_updates[0][1]


def test_the_threshold_is_five():
    assert LOGIN_ATTEMPTS_BEFORE_LOCKOUT == 5


def test_do_join_sets_the_lockout_threshold(client, session):
    policy = _join(session, current_policy={})

    assert policy["LoginAttemptsBeforeLockout"] == 5


def test_do_join_overrides_whatever_jellyfin_handed_back(client, session):
    """The value the fresh account came with must not survive the read-modify-write."""
    policy = _join(session, current_policy={"LoginAttemptsBeforeLockout": 3})

    assert policy["LoginAttemptsBeforeLockout"] == 5
