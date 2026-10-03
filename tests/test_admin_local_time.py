"""The admin panel shows and accepts user expiries in the server's timezone.

Expiries are stored as naive UTC and the scheduler compares them in UTC, which
is right. The panel was not: the Users page printed the stored value as is, so
an expiry of 02:17 UTC read "2:17 AM" to an admin in Monterrey (UTC-6) when the
account had in fact lapsed at 20:17 the evening before. The edit modal had the
mirror problem — whatever the admin typed was stored as UTC, so a time meant as
local cut the account six hours early.

Measured in production on 2026-10-03: the same ``invitation.created`` read
"Sep 2, 2026 at 8:16 PM" on the Invitations page (``local_date``) and
"Sep 3, 2026 at 2:16 AM" on the Users page (``human_date``).
"""

import datetime
from zoneinfo import ZoneInfo

import pytest

from app import jinja_filters
from app.extensions import db
from app.models import AdminAccount, Invitation, MediaServer, User

MONTERREY = ZoneInfo("America/Monterrey")

# isaac92's real values: joined Sep 2 20:16 Monterrey, 30 days.
# Stored naive, as SQLite hands them back.
EXPIRES_UTC = datetime.datetime(2026, 10, 3, 2, 17, tzinfo=datetime.UTC).replace(
    tzinfo=None
)
CREATED_UTC = datetime.datetime(2026, 9, 3, 2, 16, tzinfo=datetime.UTC).replace(
    tzinfo=None
)


@pytest.fixture(autouse=True)
def _monterrey(monkeypatch):
    monkeypatch.setattr(jinja_filters, "_LOCAL_TIMEZONE", MONTERREY)


@pytest.fixture
def admin_client(client, session):
    admin = AdminAccount(username="tz-admin")
    admin.set_password("irrelevant")
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    return client


@pytest.fixture
def member(session):
    server = MediaServer(
        name="Neexy", server_type="jellyfin", url="http://jelly.local", api_key="k"
    )
    db.session.add(server)
    db.session.commit()
    db.session.add(
        Invitation(code="TZCODE0001", created=CREATED_UTC, duration="30", used=True)
    )
    user = User(
        token="jf-tz",
        username="isaac-tz",
        email="tz@example.com",
        code="TZCODE0001",
        server_id=server.id,
        expires=EXPIRES_UTC,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_human_date_shows_a_utc_value_in_local_time():
    assert jinja_filters.human_date(EXPIRES_UTC) == "Oct 2, 2026 at 8:17 PM"


def test_human_date_converts_an_aware_value_too():
    aware = EXPIRES_UTC.replace(tzinfo=datetime.UTC)
    assert jinja_filters.human_date(aware) == "Oct 2, 2026 at 8:17 PM"


def test_parse_local_datetime_reads_a_naive_value_as_local():
    parsed = jinja_filters.parse_local_datetime("2026-10-02T20:17")
    assert parsed == EXPIRES_UTC.replace(tzinfo=datetime.UTC)
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_parse_local_datetime_keeps_an_explicit_offset():
    parsed = jinja_filters.parse_local_datetime("2026-10-03T02:17+00:00")
    assert parsed == EXPIRES_UTC.replace(tzinfo=datetime.UTC)


def test_users_page_shows_expiry_and_invite_date_in_local_time(admin_client, member):
    body = admin_client.get("/users/table").data.decode()

    assert "Oct 2, 2026 at 8:17 PM" in body
    assert "Sep 2, 2026 at 8:16 PM" in body
    assert "2:17 AM" not in body


def test_edit_modal_prefills_the_local_time(admin_client, member):
    body = admin_client.get(f"/user/{member.id}").data.decode()

    assert 'value="2026-10-02T20:17"' in body
    assert "Oct 2, 2026 at 8:17 PM" in body


def test_saving_the_modal_stores_utc(admin_client, member):
    response = admin_client.post(
        f"/user/{member.id}", data={"expires": "2026-10-16T01:00"}
    )
    assert response.status_code == 200

    db.session.expire_all()
    stored = db.session.get(User, member.id).expires
    # SQLite drops tzinfo without converting, so what lands must already be UTC.
    assert stored.replace(tzinfo=None) == datetime.datetime(
        2026, 10, 16, 7, 0, tzinfo=datetime.UTC
    ).replace(tzinfo=None)


def test_expired_users_page_shows_local_time(admin_client, member, session):
    from app.models import ExpiredUser

    db.session.add(
        ExpiredUser(
            original_user_id=member.id,
            username=member.username,
            expired_at=EXPIRES_UTC,
            deleted_at=datetime.datetime(2026, 10, 3, 2, 21, tzinfo=datetime.UTC),
        )
    )
    db.session.commit()

    body = admin_client.get("/expired-users/table").data.decode()

    assert "2026-10-02 20:17" in body
    assert "2026-10-02 20:21" in body
