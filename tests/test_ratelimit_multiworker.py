"""Guards for the per-process rate-limit storage gap (F-02 residual).

``memory://`` storage is per process, so N worker processes multiply every
declared limit by N. The deployment closes that by running a *single* gthread
worker: threads share the process memory, so the counters stay exact without a
shared backend. SQLite (``sqlite:///.../database.db``) already rules out running
several replicas, so the multi-process case this guarded against is not a shape
this app can take.

Three guards keep it that way, and keep the fallback honest for anyone who
raises GUNICORN_WORKERS anyway:

* the two GUNICORN_WORKERS defaults are pinned to each other, so scaled_limit
  never divides by a worker count the deployment does not have;
* ``scaled_limit`` divides declared limits by the worker count so the
  aggregate stays close to what was intended;
* startup logs an explicit error naming the effective multiplier.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

GUNICORN_CONF = Path("gunicorn.conf.py")

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


# ── The deployment shape the limits assume ─────────────────────────────────


def test_worker_count_default_matches_gunicorn_config(monkeypatch):
    """The two GUNICORN_WORKERS defaults must never drift apart.

    gunicorn.conf.py decides how many processes actually run; ``_worker_count``
    decides how far ``scaled_limit`` divides. Change one without the other and
    the limits stop matching the deployment *silently* -- dividing by 4 on a
    single process turns the declared "10 per minute" login limit into 3.
    """
    from app.extensions import _worker_count

    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)

    conf = GUNICORN_CONF.read_text(encoding="utf-8")
    declared = re.search(
        r'workers\s*=\s*int\(\s*os\.getenv\(\s*"GUNICORN_WORKERS"\s*,\s*"(\d+)"', conf
    )
    assert declared, (
        "gunicorn.conf.py no longer reads GUNICORN_WORKERS in the expected shape; "
        "this test can no longer verify the two defaults agree"
    )

    assert _worker_count() == int(declared.group(1)), (
        "app/extensions.py:_worker_count() and gunicorn.conf.py disagree on the "
        "default worker count, so scaled_limit divides by the wrong number"
    )


def test_single_worker_serves_requests_concurrently():
    """One worker is only acceptable because it is threaded.

    A single ``sync`` worker serves one request at a time, so a slow Jellyfin
    call would block login, invites and static assets until the 120s timeout.
    Threads are what make the exact-counters trade affordable.
    """
    conf = GUNICORN_CONF.read_text(encoding="utf-8")

    if re.search(r'"GUNICORN_WORKERS"\s*,\s*"1"', conf) is None:
        pytest.skip("multi-worker deployment; threading is not load-bearing here")

    assert 'worker_class = "gthread"' in conf, (
        "single-worker deployment without gthread: requests would be serialised"
    )
    threads = re.search(
        r'threads\s*=\s*int\(\s*os\.getenv\(\s*"GUNICORN_THREADS"\s*,\s*"(\d+)"', conf
    )
    assert threads and int(threads.group(1)) > 1, (
        "gthread without a thread count above 1 still serialises requests"
    )


def test_counters_stay_exact_under_concurrent_requests(monkeypatch):
    """The whole premise: threads share counters, so limits are exact.

    This is what makes one gthread worker a valid substitute for Redis. If
    MemoryStorage's per-key RLock did not hold, concurrent hits would race and
    let more than the declared number through. 11 simultaneous requests against
    a "10 per minute" limit must yield exactly one 429 -- not zero (racy
    counter) and not eight (limit scaled down as if there were 4 processes).
    """
    from concurrent.futures import ThreadPoolExecutor

    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)

    from app.extensions import scaled_limit

    app = Flask(__name__)
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[],
        storage_uri="memory://",
        enabled=True,
    )
    limiter.init_app(app)

    @app.route("/guarded")
    @limiter.limit(scaled_limit("10 per minute"))
    def guarded():
        return "ok"

    client = app.test_client()

    def hit(_):
        return client.get("/guarded", environ_base={"REMOTE_ADDR": "203.0.113.7"})

    with ThreadPoolExecutor(max_workers=11) as pool:
        codes = [r.status_code for r in pool.map(hit, range(11))]

    assert codes.count(429) == 1, (
        f"expected exactly one rejection out of 11 against a 10/minute limit, "
        f"got {codes.count(429)} (200s: {codes.count(200)}); "
        "more than one means the limit was scaled down as if several processes "
        "were running, zero means the shared counter raced"
    )


# ── The decorators actually use it ─────────────────────────────────────────


def test_auth_routes_use_scaled_limits():
    """The login limiter must go through scaled_limit, not a bare string."""
    from pathlib import Path

    src = Path("app/blueprints/auth/routes.py").read_text(encoding="utf-8")

    assert "scaled_limit(" in src, (
        "auth routes still declare raw limit strings; the 4x worker "
        "multiplier would apply unmitigated"
    )
