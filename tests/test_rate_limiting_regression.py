"""Regression tests for globally disabled rate limiting (F-02).

``Limiter(..., enabled=False)`` silently turned every ``@limiter.limit``
decorator in the codebase into a no-op, including the ``10 per minute`` guard
on ``/login``. Brute forcing an admin password was unbounded.

Rate limiting stays off during the rest of the suite (``RATELIMIT_ENABLED`` is
False in TestConfig) so unrelated tests can hammer endpoints freely; these
tests flip it on explicitly.
"""

import pytest

from app.extensions import limiter


@pytest.fixture
def rate_limited(app):
    """Turn the limiter on for a single test and reset its counters."""
    previous = limiter.enabled
    limiter.enabled = True
    with app.app_context():
        limiter.reset()
    yield
    with app.app_context():
        limiter.reset()
    limiter.enabled = previous


def test_limiter_is_enabled_by_default():
    """The limiter object itself must not ship disabled.

    This is the actual regression: a disabled limiter makes every
    ``@limiter.limit`` decorator in the app decorative. Test config turns it
    off deliberately, so assert on the constructor default instead.
    """
    from app.config import BaseConfig

    assert getattr(BaseConfig, "RATELIMIT_ENABLED", True) is True, (
        "Rate limiting must be enabled in the base configuration"
    )


def test_login_is_rate_limited(client, rate_limited):
    """/login must start rejecting after its declared 10 per minute."""
    statuses = [
        client.post(
            "/login", data={"username": "nobody", "password": f"wrong-{i}"}
        ).status_code
        for i in range(12)
    ]

    assert 429 in statuses, (
        f"No 429 after 12 login attempts; rate limiting is not enforced. "
        f"Got: {statuses}"
    )
    # The limit is 10/min, so the first few must still be served normally.
    assert statuses[0] != 429


def test_invitation_processing_is_rate_limited(client, rate_limited):
    """Invite code guessing must be bounded too (chains with F-15)."""
    statuses = [
        client.post("/invitation/process", data={"code": f"ABC{i:03d}"}).status_code
        for i in range(25)
    ]

    assert 429 in statuses, (
        "No 429 after 25 invite submissions; code enumeration is unbounded"
    )
