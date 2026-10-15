"""BD-105: one deduped ping per persistent warn-only condition (a ticket stuck in
reconcile; a ready ticket nobody has answered)."""

from datetime import datetime, timedelta, timezone

import pytest

from fleet import dispatch_cli as dcli
from fleet import notify
from fleet import review_watch as rw

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    out = []

    def fake(ticket, event, warn=None, detail=None, log_unconfigured=False):
        out.append((ticket["id"], event))
        return True
    monkeypatch.setattr(notify, "notify_quietly", fake)
    dcli._SKIP_RUNS.clear()
    return out


def test_new_events_are_known_and_low():
    for event in ("ready_reminder", "reconcile_stuck"):
        assert event in notify.EVENTS
        assert notify.PRIORITIES[event] == "low"


def test_stuck_ping_waits_then_fires_once(sent):
    t = [{"id": "BD-1"}]
    for _ in range(dcli.STUCK_CYCLES - 1):
        dcli._stuck_pings(t, {"BD-1"}, print)
    assert sent == []
    for _ in range(5):
        dcli._stuck_pings(t, {"BD-1"}, print)
    assert sent == [("BD-1", "reconcile_stuck")]


def test_stuck_ping_clears_and_returns(sent):
    t = [{"id": "BD-1"}]
    for _ in range(dcli.STUCK_CYCLES):
        dcli._stuck_pings(t, {"BD-1"}, print)
    dcli._stuck_pings(t, set(), print)
    for _ in range(dcli.STUCK_CYCLES):
        dcli._stuck_pings(t, {"BD-1"}, print)
    assert sent == [("BD-1", "reconcile_stuck")] * 2


def test_stuck_ping_retries_when_ntfy_is_down(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "notify_quietly", lambda *a, **k: calls.append(1) or False)
    dcli._SKIP_RUNS.clear()
    for _ in range(dcli.STUCK_CYCLES + 2):
        dcli._stuck_pings([{"id": "BD-1"}], {"BD-1"}, print)   # never raises
    assert len(calls) == 3


def test_ready_reminder_after_the_wait_only_once(sent):
    state = {"sent": ["BD-2:ship:abc123"], "ready_since": {}}
    t = {"id": "BD-2", "status": "review"}
    rw._ready_reminders([t], state, NOW, print)
    assert sent == []
    later = NOW + timedelta(seconds=rw.READY_REMINDER_AFTER)
    rw._ready_reminders([t], state, later, print)
    rw._ready_reminders([t], state, later + timedelta(hours=1), print)
    assert sent == [("BD-2", "ready_reminder")]
    # leaves the ready state, comes back: a new wait and a new ping
    rw._ready_reminders([], state, later, print)
    rw._ready_reminders([t], state, later, print)
    rw._ready_reminders([t], state, later + timedelta(seconds=rw.READY_REMINDER_AFTER), print)
    assert sent == [("BD-2", "ready_reminder")] * 2
