"""Regression tests for the invitation claim colliding with its own validation.

``try_claim_invitation`` reserved a single-use invite by writing ``used = True``
before provisioning. But ``used`` already meant "consumed" to
``is_invite_valid``, which every media-server client calls again from inside
``_do_join``. So the claim made the invitation reject the very signup it was
taken for: the first legitimate redemption of a single-use code failed with
"Invitation has already been used." and no account was ever created.

These tests drive the *real* stack -- manager -> FormBasedWorkflow ->
JellyfinClient._do_join -- and stub only the HTTP layer. Every test in
``test_invitation_race.py`` monkeypatches ``WorkflowFactory.create_workflow``
and so can never reach a client at all, which is exactly why this shipped.
"""

import datetime

from app.extensions import db
from app.models import Invitation, MediaServer, User
from app.services.media.jellyfin import JellyfinClient

CODE = "CLAIMJF01"


class _Response:
    """Minimal stand-in for a requests.Response from the Jellyfin API."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _stub_http_layer(monkeypatch, server_id):
    """Stub only what talks to Jellyfin over the network.

    Everything else -- validation, policy mapping, library resolution, the
    local User row -- runs for real, so the claim/validation interaction is
    exercised as it is in production.
    """
    created = []

    def create_user(self, username, password):
        created.append(username)
        return "jf-user-1"

    def get(self, endpoint, **kwargs):
        return _Response({"Items": [], "Policy": {}})

    def set_policy(self, user_id, policy):
        return None

    def reset_home_sections(self, user_id):
        return None

    monkeypatch.setattr(JellyfinClient, "create_user", create_user)
    monkeypatch.setattr(JellyfinClient, "get", get)
    monkeypatch.setattr(JellyfinClient, "set_policy", set_policy)
    monkeypatch.setattr(JellyfinClient, "reset_home_sections", reset_home_sections)
    return created


def _make_server_and_invite(*, unlimited=False):
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="jf-key",
    )
    invitation = Invitation(
        code=CODE,
        used=False,
        unlimited=unlimited,
        created=datetime.datetime.now(datetime.UTC),
    )
    invitation.servers = [server]
    db.session.add_all([server, invitation])
    db.session.commit()
    return server, invitation


def _submit(username="us1", email="isdf@hotmail.com"):
    from app.services.invitation_flow.manager import InvitationFlowManager

    return InvitationFlowManager().process_invitation_submission(
        {
            "code": CODE,
            "username": username,
            "email": email,
            "password": "Passw0rdok",
            "confirm_password": "Passw0rdok",
        }
    )


def test_first_redemption_of_single_use_invite_succeeds(client, session, monkeypatch):
    """The bug in one line: a paid, unused, single-use code must work once."""
    server, _ = _make_server_and_invite()
    created = _stub_http_layer(monkeypatch, server.id)

    result = _submit()

    assert created == ["us1"], (
        f"no account was provisioned; workflow said: {result.message!r}"
    )
    assert result.has_successful_servers(), result.message
    assert "already been used" not in str(result.message)


def test_successful_redemption_consumes_the_invitation(client, session, monkeypatch):
    """`used` must still mean "an account was created" -- the claim is not that."""
    server, _ = _make_server_and_invite()
    _stub_http_layer(monkeypatch, server.id)

    _submit()

    invitation = Invitation.query.filter_by(code=CODE).first()
    assert invitation.used is True
    assert invitation.used_at is not None


def test_second_redemption_is_rejected(client, session, monkeypatch):
    """Replay protection must survive the fix (the F-18 guarantee)."""
    server, _ = _make_server_and_invite()
    created = _stub_http_layer(monkeypatch, server.id)

    _submit(username="us1", email="one@example.com")
    result = _submit(username="us2", email="two@example.com")

    assert created == ["us1"], (
        f"expected exactly one account from a single-use invite, got {created}"
    )
    assert not result.has_successful_servers()


def test_taken_username_does_not_consume_the_invitation(client, session, monkeypatch):
    """The branch the claim collision had made unreachable.

    A name clash is the user's mistake, not a spent invite: they must be able
    to correct it and submit the same code again.
    """
    server, _ = _make_server_and_invite()
    created = _stub_http_layer(monkeypatch, server.id)
    db.session.add(
        User(
            username="us1",
            email="taken@example.com",
            token="pre-existing",
            code="OLDINVITE",
            server_id=server.id,
        )
    )
    db.session.commit()

    result = _submit(username="us1")

    assert created == [], "provisioning ran despite the name clash"
    assert "already exists" in str(result.message), result.message

    invitation = Invitation.query.filter_by(code=CODE).first()
    assert invitation.used is False, "a name clash burned the invitation"

    # And the corrected submission goes through on the same code.
    retry = _submit(username="us2", email="fresh@example.com")
    assert retry.has_successful_servers(), retry.message


def test_stale_claim_is_reclaimable(client, session):
    """Layer 2: a claim nobody released must not strand a paid invite.

    Evaluated at read time inside the claim itself -- no scheduler, so a dead
    worker cannot leave the invitation stuck.
    """
    from app.services.invites import CLAIM_TTL, try_claim_invitation

    _make_server_and_invite()

    assert try_claim_invitation(CODE), "first claim should win"
    assert not try_claim_invitation(CODE), "a live claim must block a second one"

    invitation = Invitation.query.filter_by(code=CODE).first()
    invitation.claimed_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - (
        CLAIM_TTL + datetime.timedelta(minutes=1)
    )
    db.session.commit()

    assert try_claim_invitation(CODE), "an expired claim must be reclaimable"


def test_ttl_is_compared_against_a_real_stored_claim(client, session, monkeypatch):
    """The TTL must work on the value the claim actually wrote.

    `claimed_at` is written naive while `used_at` elsewhere is written aware,
    so an expiry test that assigns the timestamp by hand could pass while the
    real comparison silently never fires -- leaving Layer 2 non-existent.
    Nothing here is hand-assigned: the claim is taken for real and only the
    TTL is shortened.
    """
    from app.services import invites as invites_module

    _make_server_and_invite()

    assert invites_module.try_claim_invitation(CODE)

    invitation = Invitation.query.filter_by(code=CODE).first()
    assert invitation.claimed_at is not None
    assert invitation.claimed_at.tzinfo is None, (
        "claimed_at came back timezone-aware; the TTL comparison binds a naive "
        "value and would not match"
    )

    # A live claim blocks while the TTL stands...
    assert not invites_module.try_claim_invitation(CODE)

    # ...and stops blocking once that same stored value falls outside it.
    monkeypatch.setattr(invites_module, "CLAIM_TTL", datetime.timedelta(0))
    assert invites_module.try_claim_invitation(CODE), (
        "the TTL never fired against the timestamp the claim itself wrote"
    )


def test_claimed_and_used_report_different_messages(client, session, monkeypatch):
    """ "Already used" on a code nobody redeemed is what hid this bug for weeks."""
    server, _ = _make_server_and_invite()
    from app.services.invites import try_claim_invitation

    try_claim_invitation(CODE)
    contended = _submit()
    assert "being redeemed right now" in str(contended.message), contended.message

    invitation = Invitation.query.filter_by(code=CODE).first()
    invitation.used = True
    db.session.commit()

    spent = _submit()
    assert "already been used" in str(spent.message), spent.message


def test_release_does_not_clear_a_claim_it_does_not_own(client, session):
    """A late release from a stalled request must not free someone else's claim."""
    from app.services.invites import release_invitation_claim, try_claim_invitation

    _make_server_and_invite()

    stale_token = try_claim_invitation(CODE)
    invitation = Invitation.query.filter_by(code=CODE).first()
    invitation.claimed_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - (
        datetime.timedelta(hours=1)
    )
    db.session.commit()

    fresh_token = try_claim_invitation(CODE)
    assert fresh_token and fresh_token != stale_token

    release_invitation_claim(CODE, stale_token)

    assert not try_claim_invitation(CODE), (
        "a stale release freed the claim held by another request"
    )


def test_incomplete_flow_releases_the_claim(client, session, monkeypatch):
    """A workflow that only asks for more input has provisioned nothing.

    `_result_provisioned_anything` treated any non-FAILURE status as proof of
    provisioning, so OAUTH_PENDING / AUTHENTICATION_REQUIRED kept the claim and
    burned the invite mid-flow.
    """
    from app.services.invitation_flow import manager as manager_module
    from app.services.invitation_flow.results import InvitationResult, ProcessingStatus

    _make_server_and_invite()

    class _PendingWorkflow:
        def process_submission(self, invitation, servers, form_data):
            return InvitationResult(
                status=ProcessingStatus.OAUTH_PENDING,
                message="Plex OAuth authentication required",
                successful_servers=[],
                failed_servers=[],
            )

    monkeypatch.setattr(
        manager_module.WorkflowFactory,
        "create_workflow",
        lambda servers: _PendingWorkflow(),
    )

    _submit()

    invitation = Invitation.query.filter_by(code=CODE).first()
    assert invitation.used is False
    assert invitation.claimed_at is None, "an unfinished flow kept the claim"
