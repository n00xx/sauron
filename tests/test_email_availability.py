"""The join form tells a buyer up front when their email already has an account.

A paid checkout never asks for an email, so the join form is the first place it
is typed — and until now a taken address was only reported after the whole form
had been filled in and submitted.

``POST /j/<code>/email-available`` mirrors the username check next to it and
keeps the same two limits:

* it answers for a VALID invite only, so it is no more of an email oracle than
  the join form's own "User or e-mail already exists." already is;
* it stays out of the free-trial path entirely. That invitation arrives bound to
  an inbox the storefront already verified; the field is read-only there and
  there is nothing to check.
"""

from unittest.mock import patch

import pytest

from app.models import Invitation, MediaServer, User


@pytest.fixture
def jellyfin(session):
    server = MediaServer(
        name="Jelly",
        server_type="jellyfin",
        url="http://jellyfin.example.com",
        api_key="k",
        verified=True,
    )
    session.add(server)
    session.commit()
    return server


def _invite(session, server, code="PAID01", bound_email=None):
    invitation = Invitation(
        code=code, used=False, unlimited=False, bound_email=bound_email
    )
    session.add(invitation)
    session.flush()
    invitation.servers.append(server)
    session.commit()
    return invitation


def _user(session, server, email, username="juanperez1"):
    session.add(
        User(username=username, email=email, token="t", code="X", server_id=server.id)
    )
    session.commit()


def _check(client, email, code="PAID01"):
    return client.post(f"/j/{code}/email-available", json={"email": email})


# ─── The endpoint ───────────────────────────────────────────────────────────


def test_a_free_email_is_available(client, session, jellyfin):
    _invite(session, jellyfin)

    res = _check(client, "nuevo@example.com")

    assert res.status_code == 200
    assert res.get_json() == {"available": True, "reason": None}
    assert res.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "typed", ["juan@example.com", "Juan@Example.com", "  juan@example.com  "]
)
def test_an_email_with_an_account_is_taken_whatever_the_case(
    client, session, jellyfin, typed
):
    _invite(session, jellyfin)
    _user(session, jellyfin, "juan@example.com")

    assert _check(client, typed).get_json() == {"available": False, "reason": "taken"}


def test_a_stored_email_in_mixed_case_still_matches(client, session, jellyfin):
    _invite(session, jellyfin)
    _user(session, jellyfin, "Juan@Example.com")

    assert _check(client, "juan@example.com").get_json()["reason"] == "taken"


def test_an_email_on_another_server_does_not_count(client, session, jellyfin):
    """Same scope as the join: it refuses a duplicate per server, not globally."""
    other = MediaServer(
        name="Other",
        server_type="jellyfin",
        url="http://other.example.com",
        api_key="k2",
        verified=True,
    )
    session.add(other)
    session.commit()
    _invite(session, jellyfin)
    _user(session, other, "juan@example.com")

    assert _check(client, "juan@example.com").get_json()["available"] is True


@pytest.mark.parametrize(
    "bad",
    ["", "sin-arroba", "a@b", "juan@", "juan@example.com'; --", "x" * 250 + "@a.mx"],
)
def test_something_that_is_not_an_address_is_reported_as_invalid(
    client, session, jellyfin, bad
):
    _invite(session, jellyfin)

    assert _check(client, bad).get_json() == {"available": False, "reason": "invalid"}


def test_the_address_never_travels_in_the_url(client, session, jellyfin):
    """A query string is what a reverse proxy writes to its access log."""
    _invite(session, jellyfin)

    res = client.get(
        "/j/PAID01/email-available", query_string={"email": "juan@example.com"}
    )

    assert res.status_code == 405


@pytest.mark.parametrize(
    "body", [None, {}, [], ["juan@example.com"], {"email": None}, {"email": ["a@b.mx"]}]
)
def test_a_body_without_a_usable_email_is_invalid_not_an_error(
    client, session, jellyfin, body
):
    _invite(session, jellyfin)

    res = client.post("/j/PAID01/email-available", json=body)

    assert res.status_code == 200
    assert res.get_json() == {"available": False, "reason": "invalid"}


def test_without_a_valid_invite_it_answers_nothing(client, session, jellyfin):
    """No invite, no oracle: a stranger cannot probe which emails have accounts."""
    _user(session, jellyfin, "juan@example.com")

    assert _check(client, "juan@example.com", code="NOPE99").status_code == 404

    used = _invite(session, jellyfin, code="USED99")
    used.used = True
    session.commit()
    assert _check(client, "juan@example.com", code="USED99").status_code == 404


def test_a_trial_invitation_gets_no_answer(client, session, jellyfin):
    """The storefront already verified that inbox; this check is not for it."""
    _invite(session, jellyfin, code="TRIAL1", bound_email="juan@example.com")
    _user(session, jellyfin, "juan@example.com")

    res = _check(client, "juan@example.com", code="TRIAL1")

    assert res.status_code == 404
    assert "available" not in res.get_json()


# ─── The join form ──────────────────────────────────────────────────────────


def test_join_page_wires_the_email_check_in_spanish(client, session, jellyfin):
    _invite(session, jellyfin)

    html = client.get("/j/PAID01").get_data(as_text=True)

    assert 'data-email-check-url="/j/PAID01/email-available"' in html
    assert 'id="email-availability"' in html
    assert "Ese correo ya tiene una cuenta. Usa otro." in html
    assert "Correo disponible." in html
    assert "That email already has an account" not in html


def test_join_page_for_a_trial_does_not_wire_the_email_check(client, session, jellyfin):
    _invite(session, jellyfin, code="TRIAL1", bound_email="juan@example.com")

    html = client.get("/j/TRIAL1").get_data(as_text=True)

    # As an attribute: the script names it too, and finds nothing to attach to.
    assert 'data-email-check-url="' not in html
    assert 'id="email-availability"' not in html
    # The username check is unaffected by the invitation being a trial.
    assert 'data-username-check-url="/j/TRIAL1/username-available"' in html


# ─── The join itself agrees with the hint ───────────────────────────────────


def test_join_refuses_a_case_variant_of_an_existing_email(app, session, jellyfin):
    """The hint compares without case, so the gate has to as well — otherwise
    the form would warn about an address the server then happily accepts."""
    from app.services.media.jellyfin import JellyfinClient

    inv = _invite(session, jellyfin)
    _user(session, jellyfin, "juan@example.com")

    with app.test_request_context():
        jf = JellyfinClient(media_server=jellyfin)
        with patch.object(JellyfinClient, "create_user") as create:
            ok, msg = jf._do_join(
                "otrousuario1", "ValidPass1", "ValidPass1", "Juan@Example.com", inv.code
            )

    assert ok is False
    assert "already exists" in str(msg)
    create.assert_not_called()
