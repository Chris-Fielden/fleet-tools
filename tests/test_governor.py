"""EM-20: the usage governor covers every agent and stops running ones at the hard limit.

Fixture plan-usage.json data, a fake herdr and a fake ntfy; temporary FLEET_HOME."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fleet import dispatch as dp
from fleet import dispatch_cli, governor, planusage, prep
from fleet import launch as lc
from fleet import settings as st
from fleet import ticket as tk
from test_dispatch import calls, herdr_log, mk, projects  # noqa: F401
from test_review_watch import Rig, ntfy, sent  # noqa: F401


def usage(pct, resets_in=timedelta(days=2), five=10):
    """Save what the status line would: weekly at `pct`, resetting `resets_in` from now."""
    now = datetime.now(timezone.utc)
    assert planusage.record({"rate_limits": {
        "five_hour": {"used_percentage": five, "resets_at": int((now + timedelta(hours=2)).timestamp())},
        "seven_day": {"used_percentage": pct, "resets_at": int((now + resets_in).timestamp())}}}, now)


def log_text():
    try:
        return dp.log_path().read_text()
    except FileNotFoundError:
        return ""


def starts(herdr_log):
    return [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]] if herdr_log.exists() else []


def cycle(settings=None):
    lines, warns = [], []
    dispatch_cli._cycle(settings or dp.load_settings(), lines.append, warns.append)
    return lines, warns


def test_defaults_and_validation(env):
    s = dp.load_settings()
    assert s["pause_weekly_pct"] == 90 and s["hard_stop_weekly_pct"] == 95
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "settings.json").write_text(json.dumps({"hard_stop_weekly_pct": 101}))
    with pytest.raises(st.SettingsError):
        dp.load_settings()


def test_a_reconcile_launch_is_refused_at_91(projects, herdr_log):
    usage(91)
    t = mk()
    settings = dp.load_settings()
    with pytest.raises(lc.LaunchError, match="paused"):
        prep._start_job("reconcile", [t], settings, lambda m: None)
    d = dp.Decision(ticket={"id": "X-1"}, agent="claude", model=None, prompt="p")
    with pytest.raises(lc.LaunchError, match="paused"):
        lc.start_agent(d, projects, "w1", lambda m: None)   # the backstop under every launch
    assert starts(herdr_log) == []
    usage(60)
    assert governor.refusal(settings) is None


def test_every_launch_kind_is_refused(projects, herdr_log):
    usage(92)
    settings = dp.load_settings()
    t = mk()
    d = dp.Decision(ticket=t, name="demo", repo_path=projects / "app", agent="claude", model=None)
    for fn in (lc.launch, lc.launch_oncall, lc.launch_research):
        code, message = fn(d, settings, lambda m: None)
        assert code == 0 and "paused" in message
    assert tk.load_ticket(t["id"])["status"] == "open"   # nothing was claimed or rolled back
    assert starts(herdr_log) == []


def test_a_crossing_with_an_empty_queue_logs_and_pings_once(projects, herdr_log, ntfy):
    usage(91)
    for _ in range(3):
        cycle()
    assert log_text().count("LAUNCH_PAUSED") == 1
    [(title, _, priority)] = sent(ntfy)
    assert title == "Launches paused" and priority == "low"
    assert governor.status()["state"] == "paused"
    # back under the limit (a new week): launches are allowed again, and a later crossing pings again
    usage(5)
    cycle()
    assert "LAUNCH_RESUMED" in log_text() and governor.status()["state"] == "ok"
    usage(92)
    cycle()
    assert len(sent(ntfy)) == 2


def test_a_paused_cycle_does_not_use_up_the_daily_launches(projects, herdr_log):
    usage(91)
    mk()
    calls_made = []
    lines, warns = [], []
    dispatch_cli._cycle(dp.load_settings(), lines.append, warns.append, reserve=lambda: calls_made.append(1) or True)
    assert calls_made == [] and starts(herdr_log) == []
    assert any("paused" in line for line in lines)


def test_at_95_running_agents_are_stopped_once_and_resumed_after_the_reset(projects, herdr_log, ntfy, env, monkeypatch):
    rig = Rig(projects, env, monkeypatch)
    usage(96, resets_in=timedelta(hours=3))
    for _ in range(3):
        cycle(rig.settings)
    keys = [c for c in calls(herdr_log) if c[:2] == ["agent", "send-keys"]]
    assert keys == [["agent", "send-keys", rig.id.lower(), "esc"]]        # once
    assert [t for t, _, p in sent(ntfy) if p == "high"] == ["Agents stopped"]   # one high ping
    assert "AGENT_STOPPED" in log_text() and log_text().count("LAUNCH_PAUSED") == 1
    t = rig.get()
    assert t["status"] == "claimed" and "Usage governor:" in t["notes"][-1]["text"]
    assert governor.held_ids() == {rig.id}
    assert governor.status()["state"] == "stopped" and governor.status()["resumes_at"]
    # the stopped agent is not judged failed or lost while held, however long it sits
    rig.no_agent()
    rig.cycle(120)
    assert rig.get()["status"] == "claimed" and starts(herdr_log) == []
    # the week resets: the figure is for a new window; the agent gets a new session
    usage(2)
    future = datetime.now(timezone.utc) + timedelta(hours=4)
    lines, warns = [], []
    governor.evaluate(rig.settings, future, lines.append, warns.append)
    assert [c[2] for c in starts(herdr_log)] == [rig.id.lower()], (lines, warns)
    assert governor.held_ids() == frozenset() and governor.status()["state"] == "ok"
    assert "USAGE_RESUMED" in log_text() and "LAUNCH_RESUMED" in log_text()
    assert len([c for c in calls(herdr_log) if c[:2] == ["agent", "send-keys"]]) == 1   # not stopped again


def test_fleet_status_says_it_resumes(projects, env, monkeypatch):
    from fleet import service
    usage(96, resets_in=timedelta(hours=3))
    governor.evaluate(dp.load_settings(), datetime.now(timezone.utc), lambda m: None, lambda m: None)
    monkeypatch.setattr(service, "herdr_running", lambda: True)
    [row] = [r for r in service.status_rows() if r[1] == "usage"]
    assert row[0] == "warn" and "resumes automatically after the reset" in row[2]


def test_no_figure_means_no_pause(projects, herdr_log):
    assert governor.refusal(dp.load_settings()) is None
    cycle()
    assert "LAUNCH_PAUSED" not in log_text()
