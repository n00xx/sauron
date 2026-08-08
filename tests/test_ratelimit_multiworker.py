"""Mitigation for the per-process rate-limit storage gap (F-02 residual).

``memory://`` storage is per process. gunicorn.conf.py:16 starts 4 workers by
default, so each keeps its own counters and a declared "10 per minute" is
really 40 per minute across the deployment.

The proper fix is shared storage (Redis via RATELIMIT_STORAGE_URI), which is
an infrastructure decision. Until that is in place two code-level guards keep
the gap from being silent:

* ``scaled_limit`` divides declared limits by the worker count so the
  aggregate stays close to what was intended;
* startup logs an explicit error naming the effective multiplier.
"""

from unittest.mock import patch

import pytest

# ── Limit scaling ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("limit", "workers", "expected"),
    [
        ("10 per minute", 4, "3 per minute"),
        ("20 per minute", 4, "5 per minute"),
        ("50 per minute", 4, "13 per minute"),
        ("10 per minute", 1, "10 per minute"),
        ("10 per minute", 0, "10 per minute"),
        # Never scale below one attempt, or the endpoint becomes unusable.
        ("2 per minute", 8, "1 per minute"),
    ],
)
def test_scaled_limit_divides_by_worker_count(limit, workers, expected, monkeypatch):
    from app.extensions import scaled_limit

    monkeypatch.setenv("GUNICORN_WORKERS", str(workers))
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)

    assert scaled_limit(limit)() == expected


def test_scaled_limit_is_a_noop_with_shared_storage(monkeypatch):
    """Redis counters are already global; scaling would double-penalise."""
    from app.extensions import scaled_limit

    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://localhost:6379")

    assert scaled_limit("10 per minute")() == "10 per minute"


def test_scaling_can_be_disabled_explicitly(monkeypatch):
    from app.extensions import scaled_limit

    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    monkeypatch.setenv("RATELIMIT_SCALE_BY_WORKERS", "false")

    assert scaled_limit("10 per minute")() == "10 per minute"


def test_unparseable_limit_is_returned_unchanged(monkeypatch):
    """Never let a malformed limit string break the decorator."""
    from app.extensions import scaled_limit

    monkeypatch.setenv("GUNICORN_WORKERS", "4")

    assert scaled_limit("whatever/second")() == "whatever/second"


# ── Startup warning ────────────────────────────────────────────────────────


def test_startup_warns_about_per_process_storage(app, monkeypatch):
    from app.extensions import warn_on_unshared_rate_limit_storage

    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)

    with patch.object(app.logger, "error") as logged:
        unshared = warn_on_unshared_rate_limit_storage(app)

    assert unshared is True
    assert logged.called
    message = " ".join(str(a) for a in logged.call_args[0])
    assert "RATELIMIT_STORAGE_URI" in message, (
        f"Warning does not name the setting to fix: {message}"
    )


def test_startup_silent_with_shared_storage(app, monkeypatch):
    from app.extensions import warn_on_unshared_rate_limit_storage

    monkeypatch.setenv("GUNICORN_WORKERS", "4")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://localhost:6379")

    assert warn_on_unshared_rate_limit_storage(app) is False


def test_startup_silent_with_single_worker(app, monkeypatch):
    from app.extensions import warn_on_unshared_rate_limit_storage

    monkeypatch.setenv("GUNICORN_WORKERS", "1")
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)

    assert warn_on_unshared_rate_limit_storage(app) is False


# ── The decorators actually use it ─────────────────────────────────────────


def test_auth_routes_use_scaled_limits():
    """The login limiter must go through scaled_limit, not a bare string."""
    from pathlib import Path

    src = Path("app/blueprints/auth/routes.py").read_text(encoding="utf-8")

    assert "scaled_limit(" in src, (
        "auth routes still declare raw limit strings; the 4x worker "
        "multiplier would apply unmitigated"
    )
