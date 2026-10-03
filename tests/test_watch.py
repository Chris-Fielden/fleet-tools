"""dispatch --watch, tab cleanup after reconcile, lost agents (fake herdr, fake clock)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main

from test_dispatch import calls, claim, git, herdr_log, make_repo, merge_branch, mk, projects, real_repo  # noqa: F401


class FakeClock:
    """Time that only moves when the watcher sleeps. Ctrl+C after `stop` sleeps."""

    def __init__(self, stop, start=datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc), at=None):
        self.t, self.stop, self.sleeps, self.at = start, stop, [], at or {}

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)
        if len(self.sleeps) in self.at:
            self.at[len(self.sleeps)](self)
        if len(self.sleeps) >= self.stop:
            raise KeyboardInterrupt


def watch(settings_over, clock):
    s = dp.load_settings()
    s.update(settings_over)
    return dispatch_cli._cmd_watch(s, clock)


# --- settings ----------------------------------------------------------------

def test_watch_settings_defaults_and_override(projects, env):
    s = dp.load_settings()
    assert s["watch_interval_seconds"] == 120 and s["max_launches_per_day"] == 10
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "watch_interval_seconds": 30, "max_launches_per_day": 0}))
    s = dp.load_settings()
    assert s["watch_interval_seconds"] == 30 and s["max_launches_per_day"] == 0


@pytest.mark.parametrize("key, value", [("watch_interval_seconds", 29), ("watch_interval_seconds", True),
                                        ("watch_interval_seconds", "60"), ("max_launches_per_day", -1),
                                        ("max_launches_per_day", False), ("max_launches_per_day", 1.5)])
def test_watch_settings_invalid(projects, env, key, value):
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"projects_dir": str(projects), key: value}))
    with pytest.raises(dp.SettingsError, match=key):
        dp.load_settings()


# --- the lock ----------------------------------------------------------------

def test_second_instances_refuse_while_one_is_running(projects, capsys):
    with dp.single_instance():
        for mode in ("--watch", "--once"):
            assert dispatch_main([mode]) == 0
            assert "another dispatch is already running" in capsys.readouterr().out


def test_watch_holds_the_lock_between_cycles(projects, herdr_log):
    seen = []

    def during(clock):
        try:
            with dp.single_instance():
                seen.append("free")
        except dp.AlreadyRunning:
            seen.append("held")
    with dp.single_instance():
        watch({}, FakeClock(1, at={1: during}))
    assert seen == ["held"]


# --- the loop ----------------------------------------------------------------

def test_watch_repeats_on_the_interval_and_prints_only_changes(projects, herdr_log, capsys):
    real_repo(projects)
    mk(branch="task/one")
    second = mk(branch="task/two")
    clock = FakeClock(4)
    assert watch({}, clock) == 0
    assert clock.sleeps == [120, 120, 120, 120]
    out = capsys.readouterr().out
    assert out.count("launched claude") == 1
    assert out.count("skip: at capacity") == 1   # true every cycle, shown once
    assert "watch stopped" in out
    assert tk.load_ticket(second["id"])["status"] == "open"


def test_watch_picks_up_work_that_appears_later(projects, herdr_log, capsys):
    repo = real_repo(projects)
    t = mk(branch="task/one")
    mk(branch="task/two")
    claim(t)
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "one"), "-b", "task/one")

    def merged(clock):
        wt = repo / ".worktrees" / "one"
        (wt / "one.txt").write_text("w")
        git(wt, "add", "one.txt")
        git(wt, "commit", "-q", "-m", "work")
        git(repo, "merge", "-q", "--no-ff", "-m", "Merge task/one", "task/one")
    assert watch({}, FakeClock(3, at={1: merged})) == 0
    out = capsys.readouterr().out
    assert f"{t['id']}  done (task/one merged)" in out
    assert out.count("launched claude") == 1   # the second ticket starts once the first is done


def test_watch_cap_stops_launching_says_so_once_and_resets_next_day(projects, herdr_log, capsys):
    real_repo(projects)
    mk(branch="task/one")
    two = mk(branch="task/two")
    three = mk(branch="task/three")
    over = {"max_launches_per_day": 1, "max_running": 5}
    assert watch(over, FakeClock(5)) == 0
    out = capsys.readouterr().out
    assert out.count("daily launch limit reached") == 1
    assert out.count("launched claude") == 1
    assert tk.load_ticket(two["id"])["status"] == "open"
    assert [x[:2] for x in calls(herdr_log)].count(["tab", "create"]) == 1
    # Next UTC day: the count starts again, and the limit is announced again.
    assert watch(over, FakeClock(2, start=datetime(2026, 10, 3, 0, 5, tzinfo=timezone.utc))) == 0
    out = capsys.readouterr().out
    assert out.count("launched claude") == 1
    assert out.count("daily launch limit reached") == 1
    assert tk.load_ticket(two["id"])["status"] == "claimed"
    assert tk.load_ticket(three["id"])["status"] == "open"


def test_watch_cap_survives_a_restart(projects, herdr_log):
    real_repo(projects)
    mk(branch="task/one")
    two = mk(branch="task/two")
    over = {"max_launches_per_day": 1, "max_running": 5}
    assert watch(over, FakeClock(1)) == 0
    assert watch(over, FakeClock(2)) == 0
    assert tk.load_ticket(two["id"])["status"] == "open"


def test_watch_cap_still_reconciles(projects, herdr_log, capsys):
    repo = real_repo(projects)
    t = claim(mk(branch="task/one"))
    mk(branch="task/two")
    merge_branch(repo, "task/one")
    assert watch({"max_launches_per_day": 0}, FakeClock(1)) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert not any(x[:2] == ["tab", "create"] for x in calls(herdr_log))
    assert "daily launch limit reached" in capsys.readouterr().out


def test_watch_survives_an_error_in_a_cycle(projects, herdr_log, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk went away")
    monkeypatch.setattr(dispatch_cli.lc, "finish_investigations", boom)
    assert watch({}, FakeClock(3)) == 0
    out = capsys.readouterr().out
    assert out.count("error: disk went away") == 1 and "watch stopped" in out


def test_watch_unreadable_count_file_launches_nothing(projects, herdr_log, capsys):
    real_repo(projects)
    t = mk(branch="task/one")
    dp.launches_path().write_text("not json")
    assert watch({}, FakeClock(1)) == 0
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert "not launching" in capsys.readouterr().out


def test_watch_never_asks_a_question(projects, herdr_log, monkeypatch):
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: pytest.fail("watch must not prompt"))
    repo = real_repo(projects)
    claim(mk(branch="task/one"))
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "one"), "-b", "task/one")
    assert watch({}, FakeClock(2)) == 0


# --- tab cleanup after reconcile ---------------------------------------------

def world(env, monkeypatch, ticket_id, status="idle", tab_focused=False, agent=True, tabs=1):
    path = env / "world.json"
    path.write_text(json.dumps({
        "workspaces": [{"workspace_id": "w3", "label": "fleet"}],
        "tabs": [{"tab_id": f"w3:t{4 + i}", "label": ticket_id, "workspace_id": "w3",
                  "focused": tab_focused, "agent_status": status} for i in range(tabs)],
        "agents": [{"name": ticket_id.lower(), "agent_status": status, "tab_id": "w3:t4",
                    "focused": False, "workspace_id": "w3"}] if agent else []}))
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(path))
    return path


def merged_claimed(projects):
    repo = make_repo(projects)
    t = claim(mk(branch="task/job"))
    merge_branch(repo, "task/job")
    return t


@pytest.mark.parametrize("state", ["idle", "done"])
def test_reconcile_closes_the_finished_tab(projects, herdr_log, env, monkeypatch, capsys, state):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], status=state)
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert ["tab", "close", "w3:t4"] in calls(herdr_log)
    assert json.loads(w.read_text())["tabs"] == []
    assert f"TAB_CLOSED {t['id']} tab w3:t4 (agent {state})" in dp.log_path().read_text()
    assert "tab closed" in capsys.readouterr().out


@pytest.mark.parametrize("status, focused, reason", [
    ("working", False, "working"), ("blocked", False, "blocked"), ("unknown", False, "unknown"),
    ("idle", True, "focused"), ("working", True, "focused")])
def test_reconcile_keeps_a_busy_or_focused_tab(projects, herdr_log, env, monkeypatch, status, focused, reason):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], status=status, tab_focused=focused)
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    log = dp.log_path().read_text()
    assert "TAB_KEPT" in log and reason in log and "TAB_CLOSED" not in log


def test_reconcile_with_no_tab_logs_tab_kept(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], tabs=0, agent=False)
    assert dispatch_main(["--reconcile"]) == 0
    assert "TAB_KEPT" in dp.log_path().read_text()
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))


def test_reconcile_finds_the_tab_by_label_when_the_agent_is_gone(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], status="idle", agent=False)
    assert dispatch_main(["--reconcile"]) == 0
    assert ["tab", "close", "w3:t4"] in calls(herdr_log)


def test_reconcile_keeps_when_several_tabs_share_the_label(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], agent=False, tabs=2)
    assert dispatch_main(["--reconcile"]) == 0
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert "2 tabs are labelled" in dp.log_path().read_text()


def test_reconcile_with_herdr_unreachable_still_marks_done(projects):
    t = merged_claimed(projects)
    assert dispatch_main(["--reconcile"]) == 0   # conftest points FLEET_HERDR at nothing
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert "TAB_KEPT" in dp.log_path().read_text()


def test_reconcile_only_touches_the_tab_of_the_ticket_it_finished(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    path = world(env, monkeypatch, t["id"], status="idle")
    data = json.loads(path.read_text())
    data["tabs"].append({"tab_id": "w3:t9", "label": "BD-99", "workspace_id": "w3",
                         "focused": False, "agent_status": "idle"})
    path.write_text(json.dumps(data))
    assert dispatch_main(["--reconcile"]) == 0
    assert [x for x in calls(herdr_log) if x[:2] == ["tab", "close"]] == [["tab", "close", "w3:t4"]]


# --- a claimed ticket whose agent is gone ---------------------------------------

def claimed_with_folder(projects):
    repo = real_repo(projects)
    t = claim(mk(branch="task/job"))
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "job"), "-b", "task/job")
    return repo, t


def test_once_warns_about_a_lost_agent_without_a_terminal(projects, herdr_log, env, monkeypatch, capsys):
    _, t = claimed_with_folder(projects)
    world(env, monkeypatch, t["id"], agent=False, tabs=0)
    assert dispatch_main(["--once"]) == 0
    out = capsys.readouterr().out
    assert f"{t['id']}  claimed, but its agent is not running" in out
    assert "AGENT_LOST" in dp.log_path().read_text()
    assert not any(x[:2] in (["tab", "create"], ["agent", "start"]) for x in calls(herdr_log))
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_once_relaunches_in_the_same_worktree_when_the_human_agrees(projects, herdr_log, env, monkeypatch, capsys):
    repo, t = claimed_with_folder(projects)
    world(env, monkeypatch, t["id"], agent=False, tabs=0)
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: True)
    assert dispatch_main(["--once"]) == 0
    c = calls(herdr_log)
    wt = repo / ".worktrees" / "job"
    assert ["tab", "create", "--workspace", "w3", "--cwd", str(wt), "--label", t["id"], "--no-focus"] in c
    start = next(x for x in c if x[:2] == ["agent", "start"])
    assert start[2] == t["id"].lower() and start[3:5] == ["--kind", "claude"]
    assert "relaunched claude" in capsys.readouterr().out
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    assert "RELAUNCHED" in dp.log_path().read_text()


def test_once_declined_relaunch_does_nothing(projects, herdr_log, env, monkeypatch):
    _, t = claimed_with_folder(projects)
    world(env, monkeypatch, t["id"], agent=False, tabs=0)
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: False)
    assert dispatch_main(["--once"]) == 0
    assert not any(x[:2] == ["tab", "create"] for x in calls(herdr_log))


def test_relaunch_with_the_folder_gone_keeps_the_claim(projects, herdr_log, env, monkeypatch, capsys):
    real_repo(projects)
    t = claim(mk(branch="task/job"))
    world(env, monkeypatch, t["id"], agent=False, tabs=0)
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: True)
    assert dispatch_main(["--once"]) == 0
    assert "relaunch failed" in capsys.readouterr().out
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    assert not any(x[:2] == ["tab", "create"] for x in calls(herdr_log))


def test_a_listed_agent_is_not_lost(projects, herdr_log, env, monkeypatch, capsys):
    _, t = claimed_with_folder(projects)
    world(env, monkeypatch, t["id"], status="working")
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: pytest.fail("asked"))
    assert dispatch_main(["--once"]) == 0
    assert "claimed, but its agent" not in capsys.readouterr().out


def test_ask_yes_no_without_a_terminal_is_no(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert dispatch_cli._ask_yes_no("ok?") is False
