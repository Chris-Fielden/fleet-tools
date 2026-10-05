"""dispatch --watch, tab cleanup after reconcile, lost agents (fake herdr, fake clock)."""
import json
import sys
from pathlib import Path
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
    assert out.count("daily launch limit reached (") == 1
    assert out.count("launched claude") == 1
    assert tk.load_ticket(two["id"])["status"] == "open"
    assert [x[:2] for x in calls(herdr_log)].count(["tab", "create"]) == 1
    # Next UTC day: the count starts again, and the limit is announced again.
    assert watch(over, FakeClock(2, start=datetime(2026, 10, 3, 0, 5, tzinfo=timezone.utc))) == 0
    out = capsys.readouterr().out
    assert out.count("launched claude") == 1
    assert out.count("daily launch limit reached (") == 1
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
    assert "error: cannot read" in capsys.readouterr().out


def test_watch_counts_a_launch_on_the_day_it_starts_when_the_cycle_crosses_midnight(projects, herdr_log, monkeypatch):
    real_repo(projects)
    mk(branch="task/one")
    clock = FakeClock(1, start=datetime(2026, 10, 2, 23, 59, 50, tzinfo=timezone.utc))

    def slow_step(tickets, warn):
        clock.t += timedelta(seconds=30)   # reconcile and friends take a while
        return []
    monkeypatch.setattr(dispatch_cli.lc, "finish_investigations", slow_step)
    assert watch({"max_launches_per_day": 1}, clock) == 0
    assert dp.launches_today("2026-10-03") == 1 and dp.launches_today("2026-10-02") == 0


def test_watch_survives_log_failures(projects, herdr_log, monkeypatch, capsys):
    monkeypatch.setattr(dp, "log", lambda *a, **k: (_ for _ in ()).throw(OSError("log disk full")))
    assert watch({"max_launches_per_day": 0}, FakeClock(2)) == 0
    assert "watch stopped" in capsys.readouterr().out


def test_watch_counts_a_launch_before_it_starts_even_if_the_cycle_then_fails(projects, herdr_log, monkeypatch):
    real_repo(projects)
    mk(branch="task/one")

    def explode(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(dispatch_cli.lc, "launch", explode)   # interrupted mid-launch
    assert watch({}, FakeClock(5)) == 0
    assert dp.launches_today("2026-10-02") == 1


def test_watch_launches_nothing_if_the_count_cannot_be_saved(projects, herdr_log, monkeypatch, capsys):
    real_repo(projects)
    t = mk(branch="task/one")

    def no_write(*a, **k):
        raise OSError("read-only disk")
    monkeypatch.setattr(dp, "add_launches", no_write)
    assert watch({}, FakeClock(1)) == 0
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not herdr_log.exists()   # herdr was never called
    assert "error: read-only disk" in capsys.readouterr().out


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
    ("idle", True, "focused"), ("working", True, "working")])
def test_reconcile_keeps_a_busy_or_focused_tab(projects, herdr_log, env, monkeypatch, status, focused, reason):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], status=status, tab_focused=focused)
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    log = dp.log_path().read_text()
    assert "TAB_KEPT" in log and reason in log and "TAB_CLOSED" not in log


def add_tab(path, label="dispatch", focused=False):
    data = json.loads(path.read_text())
    data["tabs"].append({"tab_id": "w3:t9", "label": label, "workspace_id": "w3",
                         "focused": focused, "agent_status": "idle"})
    path.write_text(json.dumps(data))


def test_reconcile_closes_a_focused_tab_after_moving_focus(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    add_tab(w)
    assert dispatch_main(["--reconcile"]) == 0
    c = calls(herdr_log)
    assert ["tab", "focus", "w3:t9"] in c and ["tab", "close", "w3:t4"] in c
    assert c.index(["tab", "focus", "w3:t9"]) < c.index(["tab", "close", "w3:t4"])
    data = json.loads(w.read_text())
    assert [x["tab_id"] for x in data["tabs"]] == ["w3:t9"] and data["tabs"][0]["focused"]
    assert "TAB_CLOSED" in dp.log_path().read_text()


def test_reconcile_never_moves_focus_or_closes_a_working_focused_tab(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], status="working", tab_focused=True)
    add_tab(w)
    assert dispatch_main(["--reconcile"]) == 0
    assert not any(x[:2] in (["tab", "close"], ["tab", "focus"]) for x in calls(herdr_log))
    assert "TAB_KEPT" in dp.log_path().read_text()


def test_reconcile_keeps_a_focused_tab_when_focus_cannot_move(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    add_tab(w)
    wrapper = env / "no-focus-herdr"
    wrapper.write_text(f"#!/bin/sh\n[ \"$1 $2\" = \"tab focus\" ] && exit 1\n"
                       f"exec {sys.executable} {Path(__file__).parent / 'fake_herdr.py'} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FLEET_HERDR", str(wrapper))
    assert dispatch_main(["--reconcile"]) == 0
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert "focus could not move" in dp.log_path().read_text()


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


def test_reconcile_ignores_the_state_of_an_agent_in_another_workspace(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    path = world(env, monkeypatch, t["id"], status="working", agent=False)
    data = json.loads(path.read_text())
    # The named agent is idle, but in a tab that is not in the fleet workspace.
    data["agents"] = [{"name": t["id"].lower(), "agent_status": "idle", "tab_id": "w8:t1", "focused": False}]
    path.write_text(json.dumps(data))
    assert dispatch_main(["--reconcile"]) == 0
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert "working" in dp.log_path().read_text()


@pytest.mark.parametrize("other_state, other_focused", [("working", False), ("blocked", False),
                                                        ("unknown", False), ("idle", True)])
def test_reconcile_keeps_a_tab_shared_with_an_unfinished_agent(projects, herdr_log, env, monkeypatch,
                                                               other_state, other_focused):
    t = merged_claimed(projects)
    path = world(env, monkeypatch, t["id"], status="idle")
    data = json.loads(path.read_text())
    data["agents"].append({"name": "someone-else", "agent_status": other_state, "tab_id": "w3:t4",
                           "focused": other_focused})
    path.write_text(json.dumps(data))
    assert dispatch_main(["--reconcile"]) == 0
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert "TAB_KEPT" in dp.log_path().read_text()


def test_launch_count_with_a_bad_day_is_refused(projects):
    dp.launches_path().write_text(json.dumps({"day": None, "count": 3}))
    with pytest.raises(OSError, match="bad day"):
        dp.launches_today("2026-10-02")


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


def test_failed_relaunch_makes_once_exit_nonzero(projects, herdr_log, env, monkeypatch):
    real_repo(projects)
    t = claim(mk(branch="task/job"))   # no task folder to relaunch in
    world(env, monkeypatch, t["id"], agent=False, tabs=0)
    monkeypatch.setattr(dispatch_cli, "_ask_yes_no", lambda q: True)
    assert dispatch_main(["--once"]) == 1


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
    assert dispatch_main(["--once"]) == 1
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


# --- retrying kept tabs on later cycles ----------------------------------------

def test_watch_closes_a_kept_tab_once_it_is_no_longer_focused(projects, herdr_log, env, monkeypatch, capsys):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    assert "TAB_KEPT" in dp.log_path().read_text()

    def unfocus(_clock):
        data = json.loads(w.read_text())
        data["tabs"][0]["focused"] = False
        w.write_text(json.dumps(data))
    clock = FakeClock(2, at={1: unfocus})
    watch({}, clock)
    assert json.loads(w.read_text())["tabs"] == []
    log = dp.log_path().read_text()
    assert f"TAB_CLOSED {t['id']} tab w3:t4 (agent idle)" in log
    assert f"{t['id']}  tab closed" in capsys.readouterr().out


@pytest.mark.parametrize("status, focused", [("working", False), ("idle", True)])
def test_watch_retry_keeps_a_focused_or_busy_tab_quietly(projects, herdr_log, env, monkeypatch, status, focused):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], status=status, tab_focused=focused)
    assert dispatch_main(["--reconcile"]) == 0
    before = dp.log_path().read_text()
    watch({}, FakeClock(2))
    assert ["tab", "close", "w3:t4"] not in calls(herdr_log)
    assert dp.log_path().read_text().count("TAB_KEPT") == before.count("TAB_KEPT")
    assert "TAB_CLOSED" not in dp.log_path().read_text()


def test_watch_retry_closes_once(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    data = json.loads(w.read_text())
    data["tabs"][0]["focused"] = False
    w.write_text(json.dumps(data))
    watch({}, FakeClock(3))
    assert calls(herdr_log).count(["tab", "close", "w3:t4"]) == 1
    assert dp.log_path().read_text().count("TAB_CLOSED") == 1


def test_watch_retry_leaves_a_tab_that_reconcile_never_kept_alone(projects, herdr_log, env, monkeypatch):
    monkeypatch.setattr(dispatch_cli.lc, "sweep_tabs", lambda *a, **k: [])   # these test the kept-tab retry alone
    t = merged_claimed(projects)
    assert dispatch_main(["--reconcile"]) == 0   # no tab at all: "no tab found"
    world(env, monkeypatch, t["id"])             # a tab appears later (reused by the human)
    watch({}, FakeClock(2))
    assert ["tab", "close", "w3:t4"] not in calls(herdr_log)


def test_watch_retry_logs_a_vanished_tab_once(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    w.write_text(json.dumps({"workspaces": [{"workspace_id": "w3", "label": "fleet"}],
                             "tabs": [], "agents": []}))
    watch({}, FakeClock(3))
    assert dp.log_path().read_text().count("TAB_GONE") == 1


def test_watch_retry_leaves_tabs_of_unfinished_tickets_alone(projects, herdr_log, env, monkeypatch):
    make_repo(projects)
    t = claim(mk(branch="task/job"))
    world(env, monkeypatch, t["id"])
    watch({}, FakeClock(2))
    assert ["tab", "close", "w3:t4"] not in calls(herdr_log)
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_watch_retry_keeps_an_ambiguous_label(projects, herdr_log, env, monkeypatch):
    t = merged_claimed(projects)
    world(env, monkeypatch, t["id"], agent=False, tabs=2, tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    w = json.loads(open(env / "world.json").read())
    for x in w["tabs"]:
        x["focused"] = False
    (env / "world.json").write_text(json.dumps(w))
    watch({}, FakeClock(2))
    assert ["tab", "close", "w3:t4"] not in calls(herdr_log)
    assert ["tab", "close", "w3:t5"] not in calls(herdr_log)


# --- hardened tab retry ---------------------------------------------------------

def test_watch_retry_does_not_fall_back_to_the_label(projects, herdr_log, env, monkeypatch):
    monkeypatch.setattr(dispatch_cli.lc, "sweep_tabs", lambda *a, **k: [])   # these test the kept-tab retry alone
    """Reconcile kept the agent's tab; later the agent is gone and a tab with the
    same label (the human's) must not be closed."""
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    data = json.loads(w.read_text())
    data["agents"], data["tabs"][0]["focused"] = [], False
    w.write_text(json.dumps(data))
    watch({}, FakeClock(3))
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert "TAB_GONE" in dp.log_path().read_text()


def test_watch_gives_up_on_a_tab_kept_for_too_long(projects, herdr_log, env, monkeypatch):
    monkeypatch.setattr(dispatch_cli.lc, "sweep_tabs", lambda *a, **k: [])   # these test the kept-tab retry alone
    t = merged_claimed(projects)
    w = world(env, monkeypatch, t["id"], tab_focused=True)
    assert dispatch_main(["--reconcile"]) == 0
    # make the TAB_KEPT line two days older than the fake clock
    log = dp.log_path()
    log.write_text(log.read_text().replace(
        next(l.split(" ")[0] for l in log.read_text().splitlines() if "TAB_KEPT" in l), "2026-09-30T00:00:00Z"))
    data = json.loads(w.read_text())
    data["tabs"][0]["focused"] = False
    w.write_text(json.dumps(data))
    watch({}, FakeClock(3))
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))
    assert dp.log_path().read_text().count("TAB_GIVEUP") == 1


def test_watch_retry_warns_when_herdr_is_down_and_prints_it_once(projects, env, capsys):
    t = merged_claimed(projects)
    assert dispatch_main(["--reconcile"]) == 0   # herdr unreachable: TAB_KEPT "could not be asked"
    assert "TAB_KEPT" in dp.log_path().read_text()
    capsys.readouterr()
    watch({}, FakeClock(3))
    out = capsys.readouterr().out
    assert out.count("warning: cannot retry closing kept tabs") == 1
    assert t["id"] in dp.log_path().read_text()


def test_kept_tabs_reads_the_log_incrementally(projects, monkeypatch):
    stamp = "2026-10-02T21:00:00Z"
    dp.log_path().write_text(f"{stamp} TAB_KEPT AA-1 tab w3:t4 is focused\n")
    kept = dispatch_cli.lc.KeptTabs(lambda: datetime(2026, 10, 2, 22, tzinfo=timezone.utc))
    assert kept.poll() == ["AA-1"]
    opened = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        if str(path) == str(dp.log_path()):
            orig = fh.seek
            fh.seek = lambda off, *x: (opened.append(off), orig(off, *x))[1]
        return fh
    monkeypatch.setattr("builtins.open", spy)
    with real_open(dp.log_path(), "a") as fh:
        fh.write(f"{stamp} TAB_KEPT AA-2 tab w3:t5 is focused\n{stamp} TAB_CLOSED AA-1 tab w3:t4 (agent idle)\n"
                 f"{stamp} TAB_KEPT AA-3 half")   # no newline yet: not consumed
    assert kept.poll() == ["AA-2"]
    assert opened and opened[-1] > 0               # read from the saved offset, not from 0


# --- restart on new code -----------------------------------------------------

def test_watch_restarts_when_the_checkout_gets_a_new_commit(projects, herdr_log, monkeypatch, capsys):
    heads = iter(["aaaaaaaa1", "aaaaaaaa1", "bbbbbbbb2"])
    execs, lock_free = [], []
    monkeypatch.setattr(dp, "code_head", lambda: next(heads))
    monkeypatch.setattr(dp, "Clock", lambda: FakeClock(10))

    def fake_exec(argv):
        try:
            with dp.single_instance():   # the lock must be free at exec time
                lock_free.append(True)
        except dp.AlreadyRunning:
            lock_free.append(False)
        execs.append(argv)
    monkeypatch.setattr(dispatch_cli, "_exec_self", fake_exec)
    assert dispatch_main(["--watch"]) == 0
    assert execs == [["--watch"]] and lock_free == [True]
    assert "WATCH_RESTART" in dp.log_path().read_text()
    assert "restarting" in capsys.readouterr().out


def test_watch_does_not_restart_when_head_is_unchanged_or_unreadable(projects, herdr_log, monkeypatch):
    monkeypatch.setattr(dispatch_cli, "_exec_self", lambda argv: pytest.fail("must not restart"))
    for heads in (["a"] * 5, [None] * 5, ["a", None, "a", None, "a"]):
        it = iter(heads)
        assert dispatch_cli._cmd_watch(dp.load_settings(), FakeClock(3), head=lambda: next(it)) == 0
