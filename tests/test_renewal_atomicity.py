"""Renewal is one operation: extending the date must also restore access.

Before this, `POST /api/users/<id>/extend` moved the expiry and left the media
account disabled, so a caller had to remember a second `POST /enable`. A renewal
where the second call was forgotten, failed, or never happened left a customer
who had paid and could not sign in — and nothing in sauron looked wrong, because
the membership read as valid.

Two failures are pinned down here:

  * The date moved without the account coming back on.
  * Renewing a LAPSED account extended from its old expiry, so an account that
    expired 60 days ago and renewed for 30 landed 30 days in the PAST. The next
    expiry sweep disabled the buyer again minutes after they paid.
"""

import datetime

import pytest

from app.extensions import db
from app.models import AdminAccount, ApiKey, MediaServer, User


@pytest.fixture
def api_headers(session):
    """A working API key. The route hashes what it is given, so seed the hash."""
    import hashlib

    admin = AdminAccount(username="renewal-admin")
    admin.set_password("irrelevant-for-this-test")
    db.session.add(admin)
    db.session.commit()

    raw = "test-api-key-renewal"
    db.session.add(
        ApiKey(
            name="test",
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_by_id=admin.id,
            is_active=True,
        )
    )
    db.session.commit()
    return {"X-API-Key": raw, "Content-Type": "application/json"}


@pytest.fixture
def jellyfin_server(session):
    server = MediaServer(
        name="Neexy",
        server_type="jellyfin",
        url="http://jelly.local",
        api_key="admin-api-key",
    )
    db.session.add(server)
    db.session.commit()
    return server


def _member(jellyfin_server, *, disabled, expires_days):
    """A member whose expiry sits `expires_days` from now (negative = lapsed)."""
    expires = None
    if expires_days is not None:
        expires = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        ) + datetime.timedelta(days=expires_days)
    user = User(
        token="jf-user-1",
        username="buyer",
        code="INVITE1",
        server_id=jellyfin_server.id,
        is_disabled=disabled,
        expires=expires,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _stub_enable(monkeypatch, *, succeeds=True, recorder=None):
    def fake_enable(db_id):
        if recorder is not None:
            recorder.append(db_id)
        if succeeds:
            user = db.session.get(User, db_id)
            user.is_disabled = False
            db.session.commit()
        return succeeds

    monkeypatch.setattr(
        "app.blueprints.api.api_routes.enable_user", fake_enable, raising=True
    )


def _expires_aware(user):
    db.session.refresh(user)
    value = user.expires
    return value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value


# ── Reactivation ────────────────────────────────────────────────────────────


def test_renewing_a_lapsed_member_turns_the_account_back_on(
    client, api_headers, jellyfin_server, monkeypatch
):
    calls = []
    _stub_enable(monkeypatch, recorder=calls)
    user = _member(jellyfin_server, disabled=True, expires_days=-5)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    assert calls == [user.id], "the renewal must reactivate the account"
    assert response.json["reactivated"] is True
    db.session.refresh(user)
    assert user.is_disabled is False


def test_renewing_an_active_member_does_not_touch_the_media_server(
    client, api_headers, jellyfin_server, monkeypatch
):
    """An early renewal has nothing to reactivate; skip the round-trip."""
    calls = []
    _stub_enable(monkeypatch, recorder=calls)
    user = _member(jellyfin_server, disabled=False, expires_days=10)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    assert calls == []
    assert response.json["reactivated"] is False


def test_a_failed_reactivation_returns_502_and_leaves_the_expiry_alone(
    client, api_headers, jellyfin_server, monkeypatch
):
    """The retry-safety property.

    `days` is ADDED to the stored date, so a call that moved the expiry and then
    failed to reactivate would grant the time twice when the caller retried.
    Failing before the write is what makes a retry safe.
    """
    _stub_enable(monkeypatch, succeeds=False)
    user = _member(jellyfin_server, disabled=True, expires_days=-5)
    before = _expires_aware(user)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 502
    assert _expires_aware(user) == before, "expiry must not move on a failed renewal"
    db.session.refresh(user)
    assert user.is_disabled is True


def test_a_failed_reactivation_says_so_instead_of_reporting_success(
    client, api_headers, jellyfin_server, monkeypatch
):
    """The body must not be marshalled into a success shape.

    `@api.marshal_with` would turn an error dict into
    {"message": null, "new_expiry": null} — an HTTP 502 with a body that reads
    like a delivered renewal.
    """
    _stub_enable(monkeypatch, succeeds=False)
    user = _member(jellyfin_server, disabled=True, expires_days=-5)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    body = response.get_data(as_text=True)
    assert "NOT extended" in body
    assert response.json.get("new_expiry") is None


# ── Extending from the right base date ──────────────────────────────────────


def test_renewing_a_long_lapsed_member_lands_in_the_future(
    client, api_headers, jellyfin_server, monkeypatch
):
    """Expired 60 days ago, renewed for 30: must NOT land 30 days in the past."""
    _stub_enable(monkeypatch)
    user = _member(jellyfin_server, disabled=True, expires_days=-60)

    response = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )

    assert response.status_code == 200
    now = datetime.datetime.now(datetime.UTC)
    new_expiry = _expires_aware(user)
    assert new_expiry > now, "a paid renewal must not expire immediately"
    assert new_expiry > now + datetime.timedelta(days=29)


def test_renewing_early_credits_the_time_still_unused(
    client, api_headers, jellyfin_server, monkeypatch
):
    """The behaviour worth keeping: 10 days left plus 30 renewed is 40, not 30."""
    _stub_enable(monkeypatch)
    user = _member(jellyfin_server, disabled=False, expires_days=10)

    client.post(f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers)

    now = datetime.datetime.now(datetime.UTC)
    new_expiry = _expires_aware(user)
    assert new_expiry > now + datetime.timedelta(days=39)
    assert new_expiry < now + datetime.timedelta(days=41)


def test_renewing_a_member_without_an_expiry_starts_from_today(
    client, api_headers, jellyfin_server, monkeypatch
):
    _stub_enable(monkeypatch)
    user = _member(jellyfin_server, disabled=False, expires_days=None)

    client.post(f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers)

    now = datetime.datetime.now(datetime.UTC)
    new_expiry = _expires_aware(user)
    assert (
        now + datetime.timedelta(days=29)
        < new_expiry
        < now + datetime.timedelta(days=31)
    )


def test_the_old_two_call_sequence_still_works(
    client, api_headers, jellyfin_server, monkeypatch
):
    """Backward compatibility.

    A caller written against the previous contract does `/extend` then
    `/enable`. The enable is now redundant, but it must not fail: it is a
    read-modify-write that sets IsDisabled to a value it already has.
    """
    _stub_enable(monkeypatch)
    user = _member(jellyfin_server, disabled=True, expires_days=-5)

    extend = client.post(
        f"/api/users/{user.id}/extend", json={"days": 30}, headers=api_headers
    )
    assert extend.status_code == 200

    # The point of the test: by here the account is ALREADY on, so the enable
    # below is genuinely redundant rather than doing the work itself.
    db.session.refresh(user)
    assert user.is_disabled is False

    enable = client.post(f"/api/users/{user.id}/enable", headers=api_headers)
    assert enable.status_code == 200, "a redundant enable must stay harmless"

    assert _expires_aware(user) > datetime.datetime.now(datetime.UTC)
