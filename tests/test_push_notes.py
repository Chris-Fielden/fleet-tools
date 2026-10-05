"""Human notes pushed into a running agent's tab by the watch loop (fake herdr, temp FLEET_HOME)."""
import json

import pytest

from fleet import push_notes
from fleet import ticket as tk
from test_dispatch import calls, claim, herdr_log, mk, projects  # noqa: F401
from test_watch import FakeClock, watch


@pytest.fixture
def world(env, herdr_log, monkeypatch):
    path = env / "world.json"
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(path))

    def set_state(name, state):
        path.write_text(json.dumps({"workspaces": [{"workspace_id": "w1", "label": "fleet"}], "tabs": [],
                                    "agents": [{"name": name, "agent_status": state, "tab_id": "w1:t1"}]}))
    return set_state


def prompts(log):
    return [c for c in calls(log) if c[:2] == ["agent", "prompt"]] if log.exists() else []


def run_push(ticket_id=None):
    out, warns = [], []
    push_notes.push(tk.list_tickets(), out.append, warns.append)
    return out, warns


def human_note(ticket_id, text, monkeypatch, who="human"):
    if who != "human":
        monkeypatch.setenv("CLAUDECODE", "1")
    tk.add_note(ticket_id, text)
    monkeypatch.delenv("CLAUDECODE", raising=False)


def test_sends_a_new_human_note_once(world, herdr_log):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")
    assert run_push() == ([], [])        # first sight: nothing to send
    tk.add_note(t["id"], "please also update the docs")
    out, _ = run_push()
    assert out and prompts(herdr_log) == [
        ["agent", "prompt", name, "New note from the human on your ticket:\nplease also update the docs"]]
    run_push()
    run_push()
    assert len(prompts(herdr_log)) == 1   # never resent


def test_never_resent_after_a_restart(world, herdr_log):
    t = claim(mk())
    world(t["id"].lower(), "idle")
    run_push()
    tk.add_note(t["id"], "one")
    run_push()
    assert len(prompts(herdr_log)) == 1
    run_push()   # a new watcher reads the same state file
    assert len(prompts(herdr_log)) == 1


def test_old_notes_are_not_sent(world, herdr_log):
    t = claim(mk())
    tk.add_note(t["id"], "written before the agent started")
    world(t["id"].lower(), "idle")
    run_push()
    run_push()
    assert prompts(herdr_log) == []


@pytest.mark.parametrize("state", ["working", "blocked", "unknown", "done"])
def test_waits_until_the_agent_is_idle(world, herdr_log, state):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")
    run_push()
    tk.add_note(t["id"], "mind the tests")
    world(name, state)
    run_push()
    assert prompts(herdr_log) == []
    world(name, "idle")
    run_push()
    assert len(prompts(herdr_log)) == 1


def test_notes_by_agents_are_not_sent(world, herdr_log, monkeypatch):
    t = claim(mk())
    world(t["id"].lower(), "idle")
    run_push()
    human_note(t["id"], "my own summary", monkeypatch, who="claude")
    run_push()
    assert prompts(herdr_log) == []


def test_several_notes_go_one_per_idle_period(world, herdr_log):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")
    run_push()
    tk.add_note(t["id"], "first")
    tk.add_note(t["id"], "second")
    run_push()
    assert [p[3].split("\n")[1] for p in prompts(herdr_log)] == ["first"]
    world(name, "working")
    run_push()
    assert len(prompts(herdr_log)) == 1
    world(name, "idle")
    run_push()
    assert [p[3].split("\n")[1] for p in prompts(herdr_log)] == ["first", "second"]


def test_only_claimed_tickets_with_a_listed_agent(world, herdr_log):
    open_t = mk()
    claimed = claim(mk(branch="task/other"))
    world("someone-else", "idle")
    run_push()
    tk.add_note(open_t["id"], "for an open ticket")
    tk.add_note(claimed["id"], "agent not listed")
    run_push()
    assert prompts(herdr_log) == []


def test_failed_send_is_retried(world, herdr_log, monkeypatch):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")
    run_push()
    tk.add_note(t["id"], "retry me")
    real = push_notes.lc.herdr

    def failing(*a, **k):
        if a[:2] == ("agent", "prompt"):
            raise push_notes.lc.LaunchError("boom")
        return real(*a, **k)
    monkeypatch.setattr(push_notes.lc, "herdr", failing)
    out, warns = run_push()
    assert not out and "could not send" in warns[0]
    monkeypatch.setattr(push_notes.lc, "herdr", real)
    run_push()
    assert len(prompts(herdr_log)) == 1


def test_watch_loop_pushes_notes(world, herdr_log, projects):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")

    def add(clock):
        tk.add_note(t["id"], "from the watcher test")
    watch({}, FakeClock(3, at={1: add}))
    assert len(prompts(herdr_log)) == 1


def test_baseline_at_launch_keeps_a_note_added_before_the_first_cycle(world, herdr_log):
    t = claim(mk())
    tk.add_note(t["id"], "in front of the agent at launch")
    push_notes.baseline(tk.load_ticket(t["id"]))
    tk.add_note(t["id"], "added right after the launch")
    world(t["id"].lower(), "idle")
    run_push()
    assert [p[3].split("\n")[1] for p in prompts(herdr_log)] == ["added right after the launch"]


def test_count_survives_a_ticket_leaving_the_claimed_set(world, herdr_log):
    t = claim(mk())
    name = t["id"].lower()
    world(name, "idle")
    run_push()
    tk.set_status(t["id"], "open", allowed_from=("claimed",), assignee=None)
    tk.add_note(t["id"], "added while released")
    run_push()
    claim(tk.load_ticket(t["id"]))
    run_push()
    assert len(prompts(herdr_log)) == 1


def test_notes_are_pushed_before_the_review_checks(world, herdr_log):
    from fleet import dispatch_cli
    from fleet import dispatch as dp
    t = claim(mk())
    world(t["id"].lower(), "idle")
    run_push()
    tk.add_note(t["id"], "added while it worked")
    seen = []
    dispatch_cli._cycle(dp.load_settings(), lambda m: None, lambda m: None,
                        checks=lambda emit, warn: seen.append(len(prompts(herdr_log))))
    assert seen == [1]


def test_an_idle_investigation_with_a_pending_note_is_prompted_not_finished(world, herdr_log):
    from fleet import dispatch_cli
    from fleet import dispatch as dp
    t = tk.set_status(mk(type="investigate")["id"], "claimed", allowed_from=("open", "proposed"), assignee="claude")
    world(t["id"].lower(), "idle")
    run_push()
    tk.add_note(t["id"], "look at the logs too")
    dispatch_cli._cycle(dp.load_settings(), lambda m: None, lambda m: None, checks=lambda emit, warn: None)
    assert len(prompts(herdr_log)) == 1
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_a_relaunch_counts_every_note_as_read(world, herdr_log, projects, env, monkeypatch):
    monkeypatch.setattr("fleet.launch.START_TIMEOUT", 3)
    from fleet import launch as lc
    from fleet import dispatch as dp
    from test_dispatch import make_repo, git
    repo = make_repo(projects)
    (repo / ".gitignore").write_text(".worktrees/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore")
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "demo"), "-b", "task/demo")
    t = claim(mk(branch="task/demo"))
    world(t["id"].lower(), "idle")
    run_push()
    tk.add_note(t["id"], "written by the watcher while the ticket was in review")
    w = json.loads((env / "world.json").read_text())
    w["agents"] = []   # the old agent is gone
    (env / "world.json").write_text(json.dumps(w))
    code, msg = lc.relaunch(tk.load_ticket(t["id"]), dp.load_settings(), lambda m: None)
    assert code == 0, msg
    w = json.loads((env / "world.json").read_text())
    w["agents"][0]["agent_status"] = "idle"
    (env / "world.json").write_text(json.dumps(w))
    run_push()
    assert not [p for p in prompts(herdr_log) if p[3].startswith(push_notes.PREFIX)]
