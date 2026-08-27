"""What the collectors record as "duration" while a session is still playing.

Jellyfin and Emby report two different numbers per session: ``RunTimeTicks``,
the length of the title, and ``PositionTicks``, how far the player has actually
got. The collectors mapped the first onto ``duration_ms`` for every event except
``session_end``, so any session still open carried the runtime of the film as
if it had been watched.

That value is not cosmetic: it is summed into the ``access_activity_log`` sent
to a card issuer. Observed on 2026-08-27 as "1h 26m watched" against thirteen
seconds of real playback.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.activity.monitoring.collectors.emby import EmbyCollector
from app.activity.monitoring.collectors.jellyfin import JellyfinCollector

# 87-minute film, stopped 13 seconds in.
FILE_RUNTIME_MS = 5_220_000
POSITION_MS = 13_000


def _session_payload(**overrides):
    payload = {
        "session_id": "sess-1",
        "user_name": "watcher",
        "media_title": "The End of Evangelion",
        "duration_ms": FILE_RUNTIME_MS,
        "position_ms": POSITION_MS,
        "state": "playing",
        "device_name": "MacBookPro18 1",
        "client": "Jellyfin Media Player",
        "ip_address": "172.16.10.1",
    }
    payload.update(overrides)
    return payload


def _collect(collector_cls, event_type, payload):
    """Emit one event through the real collector and return it."""
    server = MagicMock()
    server.id = 1
    collector = collector_cls(server, MagicMock())
    emitted: list = []
    collector._emit_event = emitted.append
    collector._emit_session_event(payload, event_type)
    assert emitted, "collector emitted nothing"
    return emitted[0]


@pytest.mark.parametrize("collector_cls", [JellyfinCollector, EmbyCollector])
@pytest.mark.parametrize(
    "event_type",
    ["session_start", "session_progress", "session_pause", "session_resume"],
)
def test_live_events_record_position_not_file_runtime(collector_cls, event_type):
    event = _collect(collector_cls, event_type, _session_payload())

    assert event.duration_ms == POSITION_MS
    assert event.duration_ms != FILE_RUNTIME_MS


@pytest.mark.parametrize("collector_cls", [JellyfinCollector, EmbyCollector])
def test_session_end_keeps_using_position(collector_cls):
    """The one branch that was already correct stays correct."""
    event = _collect(collector_cls, "session_end", _session_payload())

    assert event.duration_ms == POSITION_MS


@pytest.mark.parametrize("collector_cls", [JellyfinCollector, EmbyCollector])
@pytest.mark.parametrize("event_type", ["session_progress", "session_end"])
def test_missing_position_falls_back_to_runtime(collector_cls, event_type):
    """Without a position there is nothing better; the runtime is kept.

    It is still the only number available, and `position_ms` is preserved
    separately so downstream can tell a measured value from a fallback.
    """
    event = _collect(collector_cls, event_type, _session_payload(position_ms=None))

    assert event.duration_ms == FILE_RUNTIME_MS


@pytest.mark.parametrize("collector_cls", [JellyfinCollector, EmbyCollector])
def test_position_is_always_preserved_on_the_event(collector_cls):
    """`position_ms` feeds the snapshots the evidence packet reads."""
    event = _collect(collector_cls, "session_progress", _session_payload())

    assert event.position_ms == POSITION_MS
