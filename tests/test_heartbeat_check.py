import json
import time
from datetime import datetime, timezone

import pytest

from fleet import heartbeat_check as hc
from fleet import notify
from fleet import service as sv
from fleet import usage_cli

NOW = 1_800_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def rig(env, monkeypatch):
    (env / "fleet").mkdir()
    sent = []
    monkeypatch.setattr(hc.notify, "send_alert", lambda *a: sent.append(a) or True)
    monkeypatch.setattr(sv, "dispatcher_pid", lambda: 4242)
    monkeypatch.setattr(hc, "_work_owed", lambda: False)

    def beat(**over):
        b = {"pid": 4242, "started_at": iso(NOW - 3600), "cycle_finished": iso(NOW - 30),
             "consecutive_errors": 0, "last_error": None, "progress_at": iso(NOW - 60)}
        b.update(over)
        (env / "fleet" / "heartbeat.json").write_text(json.dumps(b))
    beat.sent = sent
    return beat


def test_healthy_is_silent(rig):
    rig()
    assert hc.run(NOW) == 0 and rig.sent == []


def test_missing_file(rig):
    assert hc.run(NOW) == 1
    assert "missing" in rig.sent[0][2]


def test_unreadable_file(rig, env):
    (env / "fleet" / "heartbeat.json").write_text("{nope")
    assert hc.run(NOW) == 1 and "unreadable" in rig.sent[0][2]


def test_dead_pid(rig, monkeypatch):
    rig()
    monkeypatch.setattr(sv, "dispatcher_pid", lambda: None)
    assert hc.run(NOW) == 1 and "not running" in rig.sent[0][2]


def test_pid_of_something_else(rig, monkeypatch):
    rig()
    monkeypatch.setattr(sv, "dispatcher_pid", lambda: 1)
    assert hc.run(NOW) == 1


def test_stale_cycle(rig):
    rig(cycle_finished=iso(NOW - 400))
    assert hc.run(NOW) == 1 and "no cycle has finished" in rig.sent[0][2]


def test_cycle_just_inside_the_limit_is_fine(rig):
    rig(cycle_finished=iso(NOW - 360))
    assert hc.run(NOW) == 0


def _set_interval(env, seconds):
    (env / "fleet" / "settings.json").write_text(json.dumps({"watch_interval_seconds": seconds}))


def test_stale_limit_follows_a_slower_watch_interval(rig, env):
    _set_interval(env, 600)
    rig(cycle_finished=iso(NOW - 1500))      # healthy at a 600 s interval (limit 1800 s)
    assert hc.run(NOW) == 0 and rig.sent == []
    rig(cycle_finished=iso(NOW - 1900))
    assert hc.run(NOW) == 1 and "limit 1800s" in rig.sent[0][2]


def test_stale_limit_follows_a_faster_watch_interval(rig, env):
    _set_interval(env, 30)
    rig(cycle_finished=iso(NOW - 120))       # limit is 90 s
    assert hc.run(NOW) == 1 and "limit 90s" in rig.sent[0][2]


def test_bad_settings_fall_back_to_the_default_limit(rig, env):
    (env / "fleet" / "settings.json").write_text("{nope")
    rig(cycle_finished=iso(NOW - 360))
    assert hc.run(NOW) == 0
    rig(cycle_finished=iso(NOW - 400))
    assert hc.run(NOW) == 1


def test_error_streak(rig):
    rig(consecutive_errors=3, last_error="OSError")
    assert hc.run(NOW) == 1 and "3 cycles in a row" in rig.sent[0][2]
    rig(consecutive_errors=2)
    rig.sent.clear()
    assert hc.run(NOW + 1) == 0


def test_stalled_progress_needs_owed_work(rig, monkeypatch):
    rig(progress_at=iso(NOW - 31 * 60))
    assert hc.run(NOW) == 0 and rig.sent == []
    monkeypatch.setattr(hc, "_work_owed", lambda: True)
    assert hc.run(NOW) == 1 and "nothing has changed" in rig.sent[0][2]


def test_dedupe_repeat_then_hourly_then_recovery(rig):
    assert hc.run(NOW) == 1 and len(rig.sent) == 1           # first: at once
    hc.run(NOW + 120)
    hc.run(NOW + 29 * 60)
    assert len(rig.sent) == 1                                 # quiet
    hc.run(NOW + 30 * 60)
    assert len(rig.sent) == 2                                 # +30 min
    hc.run(NOW + 60 * 60)
    assert len(rig.sent) == 2                                 # next is hourly, not 30 min
    hc.run(NOW + 90 * 60)
    assert len(rig.sent) == 3
    rig(cycle_finished=iso(NOW + 91 * 60), started_at=iso(NOW))
    assert hc.run(NOW + 92 * 60) == 0
    assert len(rig.sent) == 4 and "recovered" in rig.sent[-1][1]
    hc.run(NOW + 94 * 60)
    assert len(rig.sent) == 4                                 # one recovery ping only


def test_changing_ages_and_counts_do_not_re_alert(rig, monkeypatch):
    monkeypatch.setattr(hc, "_work_owed", lambda: True)
    for i in range(5):
        rig(cycle_finished=iso(NOW - 400), consecutive_errors=3 + i, last_error="OSError",
            progress_at=iso(NOW - 31 * 60))
        hc.run(NOW + i * 120)
    assert len(rig.sent) == 1


def test_a_failed_ping_is_retried_next_run(rig, monkeypatch):
    def boom(*a):
        raise notify.NotifyError("down")
    monkeypatch.setattr(hc.notify, "send_alert", boom)
    with pytest.raises(notify.NotifyError):
        hc.run(NOW)
    monkeypatch.setattr(hc.notify, "send_alert", lambda *a: rig.sent.append(a) or True)
    hc.run(NOW + 120)
    assert len(rig.sent) == 1


def test_checker_self_failure_pings_and_exits_1(rig, monkeypatch, capsys):
    monkeypatch.setattr(hc, "find_problems", lambda now: (_ for _ in ()).throw(RuntimeError("bug")))
    assert hc.main() == 1
    assert "could not run its checks" in capsys.readouterr().err
    assert "failed" in rig.sent[0][1]


def test_refuses_inside_an_agent(rig, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert hc.main() == 1 and rig.sent == []
    assert "only a human" in capsys.readouterr().err


def test_verb_is_wired(rig, monkeypatch):
    monkeypatch.setattr(hc, "main", lambda: 5)
    assert usage_cli.main(["heartbeat-check"]) == 5


def test_timer_units_follow_autostart(env, monkeypatch, capsys):
    for name in ("FLEET_SYSTEMCTL", "FLEET_LOGINCTL"):
        s = env / name
        s.write_text(f"#!/bin/sh\necho \"$@\" >> {env}/{name}.log\n" + ("echo no\n" if "LOGIN" in name else ""))
        s.chmod(0o755)
        monkeypatch.setenv(name, str(s))
    assert usage_cli.main(["autostart", "on"]) == 0
    svc, timer = sv.heartbeat_paths()
    assert "heartbeat-check" in svc.read_text() and "Type=oneshot" in svc.read_text()
    assert "OnUnitActiveSec=2min" in timer.read_text()
    assert "enable --now fleet-heartbeat.timer" in (env / "FLEET_SYSTEMCTL.log").read_text()
    assert usage_cli.main(["autostart", "off"]) == 0
    assert not svc.exists() and not timer.exists()
    assert "disable --now fleet-heartbeat.timer" in (env / "FLEET_SYSTEMCTL.log").read_text()


def test_stale_limit_follows_the_configured_interval(rig, env):
    (env / "fleet" / "settings.json").write_text(json.dumps({"watch_interval_seconds": 600}))
    rig(cycle_finished=iso(NOW - 1700))
    assert hc.run(NOW) == 0
    rig(cycle_finished=iso(NOW - 1900))
    assert hc.run(NOW) == 1 and "limit 1800s" in rig.sent[0][2]


@pytest.mark.parametrize("bad", [
    {"key": "stale", "last_alert": NOW - 10, "alerts": "x"},
    {"key": ["stale"], "last_alert": NOW - 10, "alerts": 1},
    {"key": "stale", "last_alert": "now", "alerts": 1},
    ["not", "a", "dict"],
])
def test_invalid_persisted_state_is_reset_not_fatal(rig, env, bad):
    (env / "fleet" / "heartbeat-check.json").write_text(json.dumps(bad))
    assert hc.run(NOW) == 1
    assert len(rig.sent) == 1 and rig.sent[0][1] == "Fleet dispatcher is unhealthy"
