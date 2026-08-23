"""Regression tests for single-use invitation replay (F-18).

``process_invitation_submission`` validated the code with ``is_invite_valid``
and only marked it used *after* provisioning finished -- and provisioning is a
network round-trip to the media server. Two submissions of the same single-use
code could both pass validation before either marked it used, so one invite
provisioned two accounts.

The fix claims the invitation atomically before provisioning and releases the
claim if every server failed, so a genuine error does not burn the invite.

The reservation lives in ``claimed_at``, not in ``used``: an earlier version
claimed via ``used`` and thereby made the invitation fail its own redemption,
since the media-server clients re-validate the code while provisioning. See
``test_invitation_claim_collision.py``. ``try_claim_invitation`` returns the
claim token, so these tests assert truthiness rather than ``is True``.
"""

import datetime

import pytest

from app.extensions import db
from app.models import Invitation


def _make_invite(app, code, *, unlimited=False, used=False):
    with app.app_context():
        db.session.add(
            Invitation(
                code=code,
                used=used,
                unlimited=unlimited,
                created=datetime.datetime.now(datetime.UTC),
            )
        )
        db.session.commit()


def _cleanup(app, code):
    with app.app_context():
        Invitation.query.filter_by(code=code).delete()
        db.session.commit()


def test_claim_succeeds_once_for_limited_invitation(app):
    """The core guarantee: two claims, only the first wins."""
    from app.services.invites import try_claim_invitation

    code = "RACELIM01"
    _make_invite(app, code)
    try:
        with app.app_context():
            assert try_claim_invitation(code)
            assert not try_claim_invitation(code), (
                "A single-use invitation was claimed twice; concurrent "
                "submissions could each provision an account"
            )
    finally:
        _cleanup(app, code)


def test_claim_records_a_reservation_without_consuming(app):
    """The claim reserves; only real provisioning consumes."""
    from app.services.invites import try_claim_invitation

    code = "RACELIM02"
    _make_invite(app, code)
    try:
        with app.app_context():
            token = try_claim_invitation(code)
            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.claimed_at is not None
            assert invitation.claim_token == token
            assert invitation.used is False, (
                "the reservation consumed the invitation, so the media-server "
                "client will reject the signup it was claimed for"
            )
    finally:
        _cleanup(app, code)


def test_unlimited_invitation_can_be_claimed_repeatedly(app):
    """Unlimited invites are meant to be reusable; the claim must not block."""
    from app.services.invites import try_claim_invitation

    code = "RACEUNL01"
    _make_invite(app, code, unlimited=True)
    try:
        with app.app_context():
            assert try_claim_invitation(code)
            assert try_claim_invitation(code)
            assert try_claim_invitation(code)
    finally:
        _cleanup(app, code)


def test_already_used_invitation_cannot_be_claimed(app):
    from app.services.invites import try_claim_invitation

    code = "RACEUSED1"
    _make_invite(app, code, used=True)
    try:
        with app.app_context():
            assert not try_claim_invitation(code)
    finally:
        _cleanup(app, code)


def test_release_restores_claimability(app):
    """A failed provisioning attempt must not burn the invitation."""
    from app.services.invites import release_invitation_claim, try_claim_invitation

    code = "RACEREL01"
    _make_invite(app, code)
    try:
        with app.app_context():
            assert try_claim_invitation(code)
            release_invitation_claim(code)

            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.used is False
            assert invitation.used_at is None
            assert try_claim_invitation(code)
    finally:
        _cleanup(app, code)


def test_unlimited_invitations_are_never_reserved(app):
    """Unlimited invites bypass the reservation entirely, so nothing to release."""
    from app.services.invites import release_invitation_claim, try_claim_invitation

    code = "RACEUNL02"
    _make_invite(app, code, unlimited=True)
    try:
        with app.app_context():
            token = try_claim_invitation(code)
            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.claimed_at is None, (
                "an unlimited invite took a reservation it can never need"
            )

            release_invitation_claim(code, token)

            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.claimed_at is None
            assert invitation.used is False
            assert try_claim_invitation(code)
    finally:
        _cleanup(app, code)


@pytest.mark.parametrize("missing_code", ["", None, "NOSUCHCODE"])
def test_claim_of_unknown_code_is_false(app, missing_code):
    from app.services.invites import try_claim_invitation

    with app.app_context():
        assert try_claim_invitation(missing_code) is None


def test_failed_provisioning_does_not_burn_the_invitation(app, monkeypatch):
    """A media-server outage must leave the invite usable.

    Claiming before provisioning would otherwise consume the code on any
    transient failure, which is worse for the user than the race it fixes.
    """
    from app.services.invitation_flow import manager as manager_module
    from app.services.invitation_flow.results import (
        InvitationResult,
        ProcessingStatus,
    )

    code = "RACEFAIL1"
    _make_invite(app, code)

    class _FailingWorkflow:
        def process_submission(self, invitation, servers, form_data):
            return InvitationResult(
                status=ProcessingStatus.FAILURE,
                message="Media server unreachable",
                successful_servers=[],
                failed_servers=[],
            )

    monkeypatch.setattr(
        manager_module.WorkflowFactory,
        "create_workflow",
        lambda servers: _FailingWorkflow(),
    )

    try:
        with app.app_context():
            manager_module.InvitationFlowManager().process_invitation_submission(
                {"code": code}
            )

            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.used is False, (
                "A failed provisioning attempt consumed the invitation"
            )
            assert invitation.claimed_at is None, (
                "A failed provisioning attempt left the invitation reserved"
            )
    finally:
        _cleanup(app, code)


def test_exception_during_provisioning_releases_the_claim(app, monkeypatch):
    from app.services.invitation_flow import manager as manager_module

    code = "RACEEXC01"
    _make_invite(app, code)

    class _ExplodingWorkflow:
        def process_submission(self, invitation, servers, form_data):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(
        manager_module.WorkflowFactory,
        "create_workflow",
        lambda servers: _ExplodingWorkflow(),
    )

    try:
        with app.app_context():
            manager_module.InvitationFlowManager().process_invitation_submission(
                {"code": code}
            )

            invitation = Invitation.query.filter_by(code=code).first()
            assert invitation.used is False
            assert invitation.claimed_at is None
    finally:
        _cleanup(app, code)


def test_second_submission_of_single_use_code_is_rejected(app, client, monkeypatch):
    """End-to-end: the same code submitted twice must provision once."""
    from app.services.invitation_flow import manager as manager_module

    code = "RACEE2E01"
    _make_invite(app, code)

    dispatched = []

    class _Workflow:
        def process_submission(self, invitation, servers, form_data):
            dispatched.append(form_data.get("code"))

            class _R:
                def to_flask_response(self):
                    return "ok"

            return _R()

    monkeypatch.setattr(
        manager_module.WorkflowFactory, "create_workflow", lambda servers: _Workflow()
    )

    try:
        with app.app_context():
            mgr = manager_module.InvitationFlowManager()
            mgr.process_invitation_submission({"code": code})
            mgr.process_invitation_submission({"code": code})

        assert len(dispatched) == 1, (
            f"Provisioning ran {len(dispatched)} times for a single-use invite"
        )
    finally:
        _cleanup(app, code)
