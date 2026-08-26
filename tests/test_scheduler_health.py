"""The scheduled Stripe sync must never be able to die quietly again.

It already did: for two days the Eventos tab showed "Enable scheduled sync"
ticked, a valid key and a 15 minute interval, while the last sync was 48 hours
old. Nothing logged above debug, nothing alerted, and the app served every other
request normally. The dispute queue lives in that tab, and an unanswered dispute
is a lost dispute, so two blind days is enough to lose money.

These tests pin the three states that mean "nothing is going to run" and the
behaviour on top of them: repair it, then say so out loud, once.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.services import scheduler_health
from app.services.scheduler_health import (
    STRIPE_SYNC_JOB_ID,
    check_stripe_sync_health,
    ensure_stripe_sync_job,
    watchdog_tick,
)


class _FakeTrigger:
    """Just enough IntervalTrigger to answer "how often is this registered?"."""

    def __init__(self, minutes):
        self.interval = timedelta(minutes=minutes)


class _FakeJob:
    _LIVE = object()

    def __init__(self, next_run_time=_LIVE, minutes=15):
        self.id = STRIPE_SYNC_JOB_ID
        # Default to a live, scheduled job. `None` is APScheduler for *paused*,
        # so defaulting to it would have every fixture quietly describe a job
        # that is registered and never going to run — pass None on purpose.
        self.next_run_time = (
            datetime.now(UTC) + timedelta(minutes=minutes)
            if next_run_time is _FakeJob._LIVE
            else next_run_time
        )
        self.trigger = _FakeTrigger(minutes)


class _FakeScheduler:
    """Stand-in for Flask-APScheduler with the surface the watchdog touches."""

    def __init__(self, *, running=True, job=None, start_raises=False):
        self.running = running
        self.jobs = {STRIPE_SYNC_JOB_ID: job} if job else {}
        self.start_raises = start_raises
        self.started = False
        self.added = []
        self.removed = []

    def start(self):
        if self.start_raises:
            raise RuntimeError("cannot start")
        self.running = True
        self.started = True

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(
        self,
        *,
        id,  # noqa: A002
        func,
        trigger,
        minutes,
        replace_existing,
        next_run_time=None,
        **kwargs,
    ):
        self.added.append(
            {"id": id, "minutes": minutes, "next_run_time": next_run_time}
        )
        # Mirrors the real APScheduler: without an explicit `next_run_time` the
        # fresh interval trigger anchors `start_date` at now + interval, so
        # re-adding an existing job pushes its fire time a whole interval away.
        #
        # The previous double set it to `now` — "fires immediately" — the exact
        # inverse. That is why 22 tests could watch the watchdog starve the job
        # it was built to rescue and report nothing but green.
        self.jobs[id] = _FakeJob(
            next_run_time=next_run_time
            or (datetime.now(UTC) + timedelta(minutes=minutes)),
            minutes=minutes,
        )

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


def _configure_sync(*, enabled=True, key="sk_test_123", interval=15, last_sync=None):
    from app.extensions import db
    from app.services.stripe_events import set_setting

    set_setting("stripe_api_key", key)
    set_setting("stripe_sync_enabled", "true" if enabled else "false")
    set_setting("stripe_sync_interval_minutes", str(interval))
    set_setting("stripe_last_sync_at", last_sync.isoformat() if last_sync else None)
    set_setting("stripe_sync_stall_alert_at", None)
    db.session.commit()


@pytest.fixture
def fake_scheduler(monkeypatch):
    """Install a fake scheduler; returns a factory so each test picks its state."""

    def _install(**kwargs):
        fake = _FakeScheduler(**kwargs)
        monkeypatch.setattr("app.extensions.scheduler", fake)
        return fake

    return _install


@pytest.fixture
def captured_alerts(monkeypatch):
    sent = []

    def _fake_notify(title, message, tags, event_type="user_joined", **kwargs):
        sent.append({"title": title, "message": message, "event_type": event_type})

    monkeypatch.setattr("app.services.notifications.notify", _fake_notify)
    return sent


# ─── check_stripe_sync_health ───────────────────────────────────────────────


def test_recent_sync_with_a_live_job_is_healthy(app, session, fake_scheduler):
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC) - timedelta(minutes=5))

        health = check_stripe_sync_health()

    assert health["stalled"] is False
    assert health["reason"] is None


def test_a_stopped_scheduler_is_stalled(app, session, fake_scheduler):
    fake_scheduler(running=False, job=_FakeJob())
    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC))

        health = check_stripe_sync_health()

    assert health["stalled"] is True
    assert health["reason"] == "scheduler_not_running"


def test_a_missing_job_is_stalled(app, session, fake_scheduler):
    fake_scheduler(running=True, job=None)
    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC))

        health = check_stripe_sync_health()

    assert health["stalled"] is True
    assert health["reason"] == "job_missing"


def test_a_sync_older_than_three_intervals_is_stalled(app, session, fake_scheduler):
    """The observed failure: everything looks configured, nothing has run."""
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=48))

        health = check_stripe_sync_health()

    assert health["stalled"] is True
    assert health["reason"] == "no_recent_sync"
    assert health["minutes_since_sync"] > 2000


def test_one_missed_tick_is_not_yet_stalled(app, session, fake_scheduler):
    """A single slow or skipped run must not page anyone."""
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(
            interval=15, last_sync=datetime.now(UTC) - timedelta(minutes=20)
        )

        health = check_stripe_sync_health()

    assert health["stalled"] is False


def test_disabled_sync_is_not_stalled(app, session, fake_scheduler):
    """Switched off on purpose is not a fault, however old the last sync is."""
    fake_scheduler(running=False, job=None)
    with app.app_context():
        _configure_sync(enabled=False, last_sync=datetime.now(UTC) - timedelta(days=30))

        health = check_stripe_sync_health()

    assert health["stalled"] is False
    assert health["enabled"] is False


def test_no_api_key_is_not_stalled(app, session, fake_scheduler):
    fake_scheduler(running=True, job=None)
    with app.app_context():
        _configure_sync(key=None)

        health = check_stripe_sync_health()

    assert health["stalled"] is False


# ─── ensure_stripe_sync_job ─────────────────────────────────────────────────


def test_ensure_registers_the_job_on_a_stopped_scheduler(app, session, fake_scheduler):
    """The silent give-up that kept the fault alive.

    The settings screen used to return early whenever the scheduler was not
    running, so an admin could save a valid key, read "Settings saved", and get
    no sync at all until someone restarted the container.
    """
    fake = fake_scheduler(running=False, job=None)
    with app.app_context():
        _configure_sync()

        registered = ensure_stripe_sync_job(app)

    assert registered is True
    assert fake.started is True
    assert fake.jobs.get(STRIPE_SYNC_JOB_ID) is not None


def test_ensure_uses_the_saved_interval(app, session, fake_scheduler):
    fake = fake_scheduler(running=True, job=None)
    with app.app_context():
        _configure_sync(interval=7)

        ensure_stripe_sync_job(app)

    assert fake.added[-1]["minutes"] == 7


def test_ensure_drops_the_job_when_the_key_is_gone(app, session, fake_scheduler):
    fake = fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(key=None)

        registered = ensure_stripe_sync_job(app)

    assert registered is False
    assert STRIPE_SYNC_JOB_ID in fake.removed


def test_ensure_reports_failure_instead_of_swallowing_it(
    app, session, fake_scheduler, caplog
):
    fake = fake_scheduler(running=True, job=None)

    def _boom(**kwargs):
        raise RuntimeError("jobstore exploded")

    fake.add_job = _boom
    with app.app_context():
        _configure_sync()

        registered = ensure_stripe_sync_job(app)

    assert registered is False
    assert any(
        record.levelname in ("ERROR", "CRITICAL") for record in caplog.records
    ), "a job that failed to register must be logged above debug"


def test_ensure_leaves_a_correctly_registered_job_alone(app, session, fake_scheduler):
    """Re-registering a healthy job is the bug, not the fix.

    Every ``add_job`` recomputes the fire time as now + interval, so touching a
    job that is already correct can only ever delay it.
    """
    scheduled_for = datetime.now(UTC) + timedelta(minutes=3)
    fake = fake_scheduler(running=True, job=_FakeJob(scheduled_for, minutes=15))
    with app.app_context():
        _configure_sync(interval=15)

        registered = ensure_stripe_sync_job(app)

    assert registered is True
    assert fake.added == [], "a correctly registered job must not be re-added"
    assert fake.jobs[STRIPE_SYNC_JOB_ID].next_run_time == scheduled_for


def test_ensure_repairs_a_paused_job(app, session, fake_scheduler):
    """`next_run_time is None` means paused: registered, and never going to run.

    Letting the guard above skip one of those would reopen the starvation bug
    through a narrower door — the tab would report "Job: registered" forever.
    """
    fake = fake_scheduler(running=True, job=_FakeJob(next_run_time=None, minutes=15))
    with app.app_context():
        _configure_sync(interval=15)

        ensure_stripe_sync_job(app)

    assert fake.added, "a paused job must be re-registered, not left alone"
    assert fake.jobs[STRIPE_SYNC_JOB_ID].next_run_time is not None


def test_ensure_reregisters_when_the_saved_interval_changed(
    app, session, fake_scheduler
):
    """The guard above must not swallow an interval change from the UI."""
    fake = fake_scheduler(running=True, job=_FakeJob(minutes=15))
    with app.app_context():
        _configure_sync(interval=5)

        ensure_stripe_sync_job(app)

    assert fake.added[-1]["minutes"] == 5


def test_a_repaired_job_runs_now_instead_of_an_interval_later(
    app, session, fake_scheduler
):
    """A job that had gone missing is already behind; it must not wait 15 more."""
    fake = fake_scheduler(running=True, job=None)
    with app.app_context():
        _configure_sync(interval=15)

        ensure_stripe_sync_job(app)

    requested = fake.added[-1]["next_run_time"]
    assert requested is not None, "a re-registered job must be given a fire time"
    assert requested <= datetime.now(UTC) + timedelta(seconds=1)


def test_the_watchdog_cannot_starve_the_job_it_is_watching(
    app, session, fake_scheduler, captured_alerts
):
    """The production failure, in one test.

    The watchdog ran every 5 minutes against a 15 minute job. Each tick re-added
    the job, each re-add pushed the fire time 15 minutes out, and the fire time
    therefore receded faster than the clock advanced: `minutes_since_sync` was
    observed climbing 186 → 191 → 196 → 201 → ... one step per tick, forever,
    while the tab reported "Job: registered · Next run: <15 minutes out>".

    Ten ticks span far more than one interval. If any of them moves the fire
    time, the job can never run.
    """
    scheduled_for = datetime.now(UTC) + timedelta(minutes=4)
    fake = fake_scheduler(running=True, job=_FakeJob(scheduled_for, minutes=15))
    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=3))

        for _ in range(10):
            watchdog_tick(app, force=True)

    assert fake.added == [], "the watchdog must not re-register a healthy job"
    assert fake.jobs[STRIPE_SYNC_JOB_ID].next_run_time == scheduled_for


def test_a_stalled_run_reports_the_error_instead_of_promising_a_repair(
    app, session, fake_scheduler, captured_alerts
):
    """ "Re-registered automatically" was a promise this path cannot keep.

    With the job registered on the right interval there is nothing to repair, so
    the alert has to say what is actually known — including the last error the
    runs reported, which is the field that separates "nothing runs" from "it
    runs and Stripe turns it away".
    """
    from app.extensions import db
    from app.services.stripe_events import set_setting

    fake_scheduler(running=True, job=_FakeJob(minutes=15))
    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=3))
        set_setting("stripe_sync_last_error", "Stripe rejected the API key (401)")
        db.session.commit()

        health = watchdog_tick(app, force=True)

    assert health is not None
    assert health["last_error"] == "Stripe rejected the API key (401)"
    message = captured_alerts[0]["message"]
    assert "re-registered automatically" not in message
    assert "401" in message


def test_scheduler_jobs_are_registered_after_the_database_is_bound():
    """Source-order invariant, because no runtime test can reach this.

    ``init_extensions`` used to register the scheduled jobs ~50 lines ABOVE
    ``db.init_app(app)``. Every job whose interval or enabled flag lives in the
    database therefore raised "The current Flask app is not registered with this
    'SQLAlchemy' instance" at boot, and the Stripe sync was never registered on
    a single container start — it only ever existed because saving the settings
    screen registered it from a request context, and it died at every restart.

    Pytest sets ``should_skip_scheduler``, so the whole block is skipped here and
    no amount of runtime testing can catch a regression. Reading the source can.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "extensions.py"
    text = source.read_text()

    db_bound = text.index("\n    db.init_app(app)")
    scheduler_bound = text.index("\n        scheduler.init_app(app)")

    assert db_bound < scheduler_bound, (
        "scheduler jobs must be registered after db.init_app(app): registering "
        "them earlier makes every settings read at boot raise"
    )


# ─── watchdog_tick ──────────────────────────────────────────────────────────


def test_watchdog_repairs_a_missing_job_and_alerts(
    app, session, fake_scheduler, captured_alerts
):
    fake = fake_scheduler(running=True, job=None)
    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC))

        result = watchdog_tick(app, force=True)

    assert result is not None
    assert result["reason"] == "job_missing"
    assert result["repaired"] is True
    assert fake.jobs.get(STRIPE_SYNC_JOB_ID) is not None
    assert len(captured_alerts) == 1
    assert captured_alerts[0]["event_type"] == "stripe_sync_stalled"


def test_watchdog_stays_quiet_when_healthy(
    app, session, fake_scheduler, captured_alerts
):
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC))

        result = watchdog_tick(app, force=True)

    assert result is None
    assert captured_alerts == []


def test_watchdog_alerts_only_once_inside_the_cooldown(
    app, session, fake_scheduler, captured_alerts
):
    """The Docker healthcheck calls this every 30 seconds — one alert, not 2880."""
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=48))

        watchdog_tick(app, force=True)
        watchdog_tick(app, force=True)
        watchdog_tick(app, force=True)

    assert len(captured_alerts) == 1


def test_watchdog_alerts_again_after_the_cooldown(
    app, session, fake_scheduler, captured_alerts
):
    fake_scheduler(running=True, job=_FakeJob())
    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=48))

        watchdog_tick(app, force=True)

        from app.extensions import db
        from app.services.stripe_events import set_setting

        stale_alert = datetime.now(UTC) - timedelta(
            minutes=scheduler_health.ALERT_COOLDOWN_MINUTES + 1
        )
        set_setting("stripe_sync_stall_alert_at", stale_alert.isoformat())
        db.session.commit()

        watchdog_tick(app, force=True)

    assert len(captured_alerts) == 2


def test_watchdog_survives_a_broken_notifier(app, session, fake_scheduler, monkeypatch):
    """An unreachable Telegram must not turn a warning into a crash."""
    fake_scheduler(running=True, job=_FakeJob())

    def _explode(*args, **kwargs):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr("app.services.notifications.notify", _explode)

    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=48))

        result = watchdog_tick(app, force=True)

    assert result is not None
    assert result["reason"] == "no_recent_sync"


def test_watchdog_is_throttled_between_checks(
    app, session, fake_scheduler, captured_alerts, monkeypatch
):
    """Unforced ticks must not hit the database on every healthcheck ping."""
    fake_scheduler(running=True, job=_FakeJob())
    monkeypatch.setattr(scheduler_health, "_last_check_monotonic", None)

    with app.app_context():
        _configure_sync(interval=15, last_sync=datetime.now(UTC) - timedelta(hours=48))

        first = watchdog_tick(app)
        second = watchdog_tick(app)

    assert first is not None, "the first unforced tick must actually run"
    assert second is None, "the immediate second tick must be throttled away"


def test_health_endpoint_stays_ok_when_the_watchdog_explodes(app, client, monkeypatch):
    """/health is the container healthcheck. A watchdog bug must never kill it."""

    def _explode(*args, **kwargs):
        raise RuntimeError("watchdog bug")

    monkeypatch.setattr("app.services.scheduler_health.watchdog_tick", _explode)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_health_endpoint_runs_the_watchdog(app, client, monkeypatch):
    """The probe is the only thing that runs on its own — it must do the check."""
    calls = []

    monkeypatch.setattr(
        "app.services.scheduler_health.watchdog_tick",
        lambda *args, **kwargs: calls.append(args),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert len(calls) == 1


# ─── The operator's off switch ──────────────────────────────────────────────
# WIZARR_DISABLE_SCHEDULER and FLASK_SKIP_SCHEDULER are supported ways to run
# without a scheduler. The key and the enabled flag live in the database, so on
# such a deployment the sync still reads as "enabled" while nothing is meant to
# run. Without this guard the watchdog would alert every hour forever and try to
# start a scheduler the operator deliberately turned off.


def test_config_disabled_scheduler_is_not_stalled(
    app, session, fake_scheduler, monkeypatch
):
    fake_scheduler(running=False, job=None)
    monkeypatch.setenv("WIZARR_DISABLE_SCHEDULER", "true")

    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC) - timedelta(days=7))

        health = check_stripe_sync_health()

    assert health["stalled"] is False
    assert health["disabled_by_config"] is True


def test_watchdog_neither_alerts_nor_starts_when_disabled_by_config(
    app, session, fake_scheduler, captured_alerts, monkeypatch
):
    fake = fake_scheduler(running=False, job=None)
    monkeypatch.setenv("FLASK_SKIP_SCHEDULER", "true")

    with app.app_context():
        _configure_sync(last_sync=datetime.now(UTC) - timedelta(days=7))

        result = watchdog_tick(app, force=True)

    assert result is None
    assert captured_alerts == []
    assert fake.started is False, "the operator's off switch must not be overridden"


def test_ensure_does_not_start_a_scheduler_the_operator_disabled(
    app, session, fake_scheduler, monkeypatch
):
    fake = fake_scheduler(running=False, job=None)
    monkeypatch.setenv("WIZARR_DISABLE_SCHEDULER", "1")

    with app.app_context():
        _configure_sync()

        registered = ensure_stripe_sync_job(app)

    assert registered is False
    assert fake.started is False
    assert fake.added == []
