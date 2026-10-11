"""EM-26 (K1): after a PASS no note is pushed to the agent; a merge stops that ticket's own
agent and closes its tab. Fake herdr, throwaway repos, a temporary FLEET_HOME.

Threat model: stopping an agent and closing a tab touches ONE ticket's agent and tab, never
others sharing the workspace (tests: shared tab, label-only tab, other workspace, other ticket)."""
import json

import pytest

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import prep
from fleet import push_notes
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import calls, claim, git, herdr_log, make_repo, mk, projects  # noqa: F401

REVIEW = "Reviewer: {who}\nCommit: {sha}\n\nVERDICT: PASS\nMUST FIX: none\nSHOULD FIX: none\nSUMMARY: ok\n"
LEFT = "SHOULD FIX leftovers from the Codex review of {sha7} (PASS):\n1. a.py: rename x\n2. b.py: add a test"


@pytest.fixture
def world(env, herdr_log, monkeypatch):
    path = env / "world.json"
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(path))

    def put(agents, tabs=()):
        path.write_text(json.dumps({"workspaces": [{"workspace_id": "w1", "label": "fleet"}],
                                    "tabs": [{"workspace_id": "w1", "focused": False, **t} for t in tabs],
                                    "agents": agents}))

    def read():
        return json.loads(path.read_text())
    put.read = read
    return put


def prompts(log):
    return [c for c in calls(log) if c[:2] == ["agent", "prompt"]] if log.exists() else []


def push(settings=True):
    out, warns = [], []
    push_notes.push(tk.list_tickets(), out.append, warns.append, dp.load_settings() if settings else None)
    return out, warns


@pytest.fixture
def repo_ticket(projects):
    repo = make_repo(projects)
    git(repo, "checkout", "-q", "-b", "task/demo")
    (repo / "w.txt").write_text("w")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    t = claim(mk())
    return repo, t


def tip_of(repo):
    return git(repo, "rev-parse", "task/demo").strip()


def review(repo, who="Codex", sha=None, name=None):
    d = repo / ".worktrees" / "demo" / ".reviews"
    d.mkdir(parents=True, exist_ok=True)
    (d / (name or f"{who.split()[0].lower()}.md")).write_text(REVIEW.format(who=who, sha=sha or tip_of(repo)))


def settle(repo, t):
    """PASS from every reviewer the (medium) gate needs on the exact tip, recorded on the ticket."""
    review(repo)
    tk.record_verdict("task/demo", "app", "PASS", tip_of(repo), "codex")


def start(t, world, state="idle"):
    world([{"name": t["id"].lower(), "agent_status": state, "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    assert push() == ([], [])   # first sight: what is there now was launched with


# --- item 1 and 2: notes ---------------------------------------------------------------

def test_a_settled_ticket_gets_no_ordinary_note_and_it_is_kept(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    settle(repo, t)
    tk.add_note(t["id"], "one more thing")
    for _ in range(3):
        assert push() == ([], [])
    assert prompts(herdr_log) == []
    assert json.loads(push_notes.state_path().read_text())[t["id"]] == 0   # not counted: kept, not dropped
    # a new commit un-settles the ticket: the kept note is delivered
    git(repo, "checkout", "-q", "task/demo")
    (repo / "more.txt").write_text("m")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "more")
    git(repo, "checkout", "-q", "main")
    out, _ = push()
    assert out and prompts(herdr_log)[0][3].endswith("one more thing")


def test_note_held_is_logged_once_not_per_cycle(repo_ticket, world, env):
    repo, t = repo_ticket
    push_notes._held_logged.clear()
    start(t, world)
    settle(repo, t)
    tk.add_note(t["id"], "later")
    for _ in range(4):
        push()
    log = (env / "fleet" / "dispatch.log")
    held = [ln for ln in log.read_text().splitlines() if "NOTE_HELD" in ln] if log.exists() else []
    assert len(held) == 1 and t["id"] in held[0]


def test_a_review_of_an_older_commit_does_not_settle(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    review(repo, sha="c" * 40)    # a PASS, but of another commit
    tk.add_note(t["id"], "please look")
    out, _ = push()
    assert out and len(prompts(herdr_log)) == 1


def test_high_risk_needs_both_reviewers_to_settle(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    tk.raise_risk(t["id"], "high")
    assert tk.load_ticket(t["id"])["risk"] == "high"
    start(t, world)
    review(repo)                                  # Codex only
    tk.record_verdict("task/demo", "app", "PASS", tip_of(repo), "codex")
    tk.add_note(t["id"], "x")
    out, _ = push()
    assert out and len(prompts(herdr_log)) == 1   # one of two reviewers: not settled, the note goes out
    review(repo, who="Claude Opus 4", name="claude.md")
    tk.record_verdict("task/demo", "app", "PASS", tip_of(repo), "claude opus")
    tk.add_note(t["id"], "y")
    world([{"name": t["id"].lower(), "agent_status": "idle", "tab_id": "w1:t1"}])
    assert push() == ([], [])
    assert len(prompts(herdr_log)) == 1


def test_medium_risk_settles_on_a_sonnet_pass(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    review(repo, who="Claude (sonnet)", name="claude.md")   # the gate accepts this below high risk
    tk.record_verdict("task/demo", "app", "PASS", tip_of(repo), "claude sonnet")
    tk.add_note(t["id"], "one more")
    assert push() == ([], [])
    assert prompts(herdr_log) == []


def test_unreadable_review_file_means_not_settled_so_notes_flow(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    d = repo / ".worktrees" / "demo" / ".reviews"
    d.mkdir(parents=True)
    (d / "codex.md").write_bytes(b"\xff\xfe not utf-8")
    tk.record_verdict("task/demo", "app", "PASS", tip_of(repo), "codex")
    tk.add_note(t["id"], "still please")
    out, _ = push()
    assert out and len(prompts(herdr_log)) == 1   # an error computing "settled" is "not settled"


def test_leftovers_notes_go_to_the_one_leftovers_ticket_not_the_agent(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    settle(repo, t)
    tk.add_note(t["id"], LEFT.format(sha7=tip_of(repo)[:7]))
    assert push() == ([], [])
    assert prompts(herdr_log) == []
    [lo] = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    assert lo["branch"] == "task/leftovers-" + t["id"].lower() and lo["type"] == "fix"
    assert lo["status"] == "proposed" and "b.py: add a test" in lo["body"]
    assert "From the Codex review of " + tip_of(repo)[:7] + ":" in lo["body"]
    assert json.loads(push_notes.state_path().read_text())[t["id"]] == 1   # counted: done with it
    push()
    assert len([x for x in tk.list_tickets() if x.get("parent") == t["id"]]) == 1   # a repeat is a no-op
    # a second review of the same ticket goes into the SAME ticket
    tk.add_note(t["id"], "SHOULD FIX leftovers from the Claude Opus review of "
                f"{tip_of(repo)[:7]} (PASS):\n1. c.py: tidy")
    push()
    [lo2] = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    assert lo2["id"] == lo["id"] and "c.py: tidy" in lo2["body"]
    assert prompts(herdr_log) == []


def test_a_finished_leftovers_ticket_is_not_reused_by_the_push(repo_ticket, world):
    repo, t = repo_ticket
    start(t, world)
    settle(repo, t)
    tk.add_note(t["id"], LEFT.format(sha7=tip_of(repo)[:7]))
    push()
    [first] = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    tk.set_status(first["id"], "cancelled", allowed_from=("proposed",))
    tk.add_note(t["id"], "SHOULD FIX leftovers from the Claude Opus review of "
                f"{tip_of(repo)[:7]} (PASS):\n1. d.py: other")
    push()
    kids = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    assert len(kids) == 2 and len([k for k in kids if k["status"] == "proposed"]) == 1


def test_leftovers_behind_an_ordinary_held_note_are_filed_anyway(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    settle(repo, t)
    tk.add_note(t["id"], "ordinary")
    tk.add_note(t["id"], LEFT.format(sha7=tip_of(repo)[:7]))
    push()
    push()
    assert json.loads(push_notes.state_path().read_text())[t["id"]] == 0   # the ordinary note is kept
    assert prompts(herdr_log) == []
    [lo] = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    assert "b.py: add a test" in lo["body"]


@pytest.mark.parametrize("who", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_leftovers_saved_by_an_agent_are_filed_too(repo_ticket, world, herdr_log, monkeypatch, who):
    repo, t = repo_ticket     # review.sh runs inside the agent's environment: the note is not "human"
    start(t, world)
    settle(repo, t)
    monkeypatch.setenv(who, "1")
    tk.add_note(t["id"], LEFT.format(sha7=tip_of(repo)[:7]))
    monkeypatch.delenv(who)
    assert tk.load_ticket(t["id"])["notes"][-1]["by"] != "human"
    push()
    [lo] = [x for x in tk.list_tickets() if x.get("parent") == t["id"]]
    assert "a.py: rename x" in lo["body"] and prompts(herdr_log) == []


def test_a_ship_run_holds_the_notes_and_unreadable_state_holds_too(repo_ticket, world, herdr_log, env):
    repo, t = repo_ticket
    start(t, world)
    tk.add_note(t["id"], "n1")
    assert prep.reserve_shipping(t["id"]) is None
    assert push() == ([], [])
    assert prompts(herdr_log) == []
    prep.release_shipping(t["id"])
    (env / "fleet" / "prep.json").write_text("{not json")   # unreadable: fail closed
    assert push() == ([], [])
    assert prompts(herdr_log) == []
    (env / "fleet" / "prep.json").unlink()
    out, _ = push()
    assert out and len(prompts(herdr_log)) == 1


def test_a_merged_branch_holds_the_notes(repo_ticket, world, herdr_log):
    repo, t = repo_ticket
    start(t, world)
    git(repo, "-c", "user.name=T", "-c", "user.email=t@e.x", "merge", "-q", "--no-ff", "task/demo", "-m", "Merge task/demo")
    tk.add_note(t["id"], "too late")
    assert push() == ([], [])
    assert prompts(herdr_log) == []


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "review"])
def test_never_pushed_when_the_ticket_is_not_claimed_any_more(repo_ticket, world, herdr_log, status, monkeypatch):
    repo, t = repo_ticket
    start(t, world)
    tk.add_note(t["id"], "n")
    real = lc.list_agents

    def flip(*a, **k):   # the ticket ends between the start of the cycle and the send
        tk.set_status(t["id"], status, allowed_from=("claimed",))
        return real(*a, **k)
    monkeypatch.setattr(lc, "list_agents", flip)
    assert push() == ([], [])
    assert prompts(herdr_log) == []
    assert push_notes.hold_reason(t) == f"the ticket is {status}"


# --- item 3: stop the agent, close its tab ---------------------------------------------

def sets(world, agents, tabs):
    world(agents, tabs)


def test_quiesce_closes_only_this_tickets_tab(env, world, herdr_log, projects):
    world([{"name": "em-1", "agent_status": "working", "tab_id": "w1:t1"},
           {"name": "em-2", "agent_status": "working", "tab_id": "w1:t2"}],
          [{"tab_id": "w1:t1", "label": "EM-1"}, {"tab_id": "w1:t2", "label": "EM-2"}])
    ok, why = lc.quiesce_agent("EM-1", dp.load_settings())
    assert ok, why
    w = world.read()
    assert [t["tab_id"] for t in w["tabs"]] == ["w1:t2"]
    assert [a["name"] for a in w["agents"]] == ["em-2"]
    c = calls(herdr_log)
    assert ["agent", "send-keys", "em-1", "esc"] in c and ["tab", "close", "w1:t1"] in c
    assert not any("em-2" in x for x in c if x[:2] != ["agent", "list"])
    assert lc.quiesce_agent("EM-1", dp.load_settings()) == (True, "no agent is running")   # idempotent


def test_quiesce_leaves_a_shared_tab_open(world, herdr_log, projects):
    world([{"name": "em-1", "agent_status": "idle", "tab_id": "w1:t1"},
           {"name": "someone-else", "agent_status": "working", "tab_id": "w1:t1"}],
          [{"tab_id": "w1:t1", "label": "EM-1"}])
    ok, why = lc.quiesce_agent("EM-1", dp.load_settings())
    assert not ok and "someone-else" in why
    assert ["tab", "close", "w1:t1"] not in calls(herdr_log)
    assert len(world.read()["tabs"]) == 1


def test_quiesce_never_closes_a_tab_only_labelled_with_the_id(world, herdr_log, projects):
    world([{"name": "other", "agent_status": "idle", "tab_id": "w1:t2"}],
          [{"tab_id": "w1:t1", "label": "EM-1"}, {"tab_id": "w1:t2", "label": "other"}])
    ok, _ = lc.quiesce_agent("EM-1", dp.load_settings())
    assert ok   # no agent of the ticket: nothing to stop
    assert not [c for c in calls(herdr_log) if c[:2] == ["tab", "close"]]


def test_quiesce_leaves_an_agent_of_the_same_name_in_another_workspace(world, herdr_log, projects):
    world([{"name": "em-1", "agent_status": "idle", "tab_id": "w9:t1"}], [{"tab_id": "w1:t5", "label": "x"}])
    ok, why = lc.quiesce_agent("EM-1", dp.load_settings())
    assert not ok and "left alone" in why
    assert not [c for c in calls(herdr_log) if c[:2] in (["tab", "close"], ["agent", "send-keys"])]


def test_quiesce_reports_when_herdr_cannot_be_asked(env, projects, monkeypatch):
    monkeypatch.setenv("FLEET_HERDR", str(env / "no-such-herdr"))
    ok, why = lc.quiesce_agent("EM-1", dp.load_settings())
    assert not ok and "herdr could not be asked" in why


def merged_with_worktree(repo, t):
    git(repo, "-c", "user.name=T", "-c", "user.email=t@e.x", "merge", "-q", "--no-ff", "task/demo", "-m", "Merge task/demo")
    wt = repo / ".worktrees" / "demo"
    (repo / ".gitignore").write_text(".worktrees/\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@e.x", "commit", "-q", "-m", "ignore")
    git(repo, "worktree", "add", "-q", str(wt), "task/demo")
    return wt


def test_ship_quiesce_stops_the_agent_and_saves_leftover_edits(repo_ticket, world, capsys):
    repo, t = repo_ticket
    name = t["id"].lower()
    world([{"name": name, "agent_status": "working", "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    wt = merged_with_worktree(repo, t)
    (wt / "w.txt").write_text("edited after the merge")
    (wt / "new.txt").write_text("new")
    assert ship_main(["--quiesce", t["id"]]) == 0
    assert world.read()["agents"] == [] and world.read()["tabs"] == []
    branch = f"keep/{name}-unmerged"
    assert git(repo, "rev-parse", "--verify", branch).strip()
    assert git(repo, "show", f"{branch}:new.txt") == "new"
    assert git(wt, "status", "--porcelain").strip() == ""   # clean: the removal will not refuse
    assert branch in capsys.readouterr().out
    assert ship_main(["--quiesce", t["id"]]) == 0   # idempotent
    assert git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/keep").split() == [branch]


def test_ship_quiesce_clean_worktree_makes_no_keep_branch(repo_ticket, world):
    repo, t = repo_ticket
    world([{"name": t["id"].lower(), "agent_status": "idle", "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    merged_with_worktree(repo, t)
    assert ship_main(["--quiesce", t["id"]]) == 0
    assert git(repo, "branch", "--list", "keep/*").strip() == ""


def test_ship_quiesce_refuses_an_unmerged_ticket(repo_ticket, world, herdr_log, capsys):
    repo, t = repo_ticket
    world([{"name": t["id"].lower(), "agent_status": "working", "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    assert ship_main(["--quiesce", t["id"]]) == 1
    assert "is not merged" in capsys.readouterr().err
    assert not [c for c in calls(herdr_log) if c[:2] in (["tab", "close"], ["agent", "send-keys"])] if herdr_log.exists() else True
    assert len(world.read()["agents"]) == 1


def test_ship_quiesce_refuses_inside_an_agent(repo_ticket, monkeypatch, capsys):
    repo, t = repo_ticket
    monkeypatch.setenv("CLAUDECODE", "1")
    assert ship_main(["--quiesce", t["id"]]) == 1
    assert "refusing to run inside an agent" in capsys.readouterr().err


def test_finish_task_runs_quiesce_between_push_and_worktree_removal():
    text = (__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts" / "finish-task.sh").read_text()
    assert text.index('git push origin "$base"') < text.index('--quiesce "$ticket"') < text.index('git worktree remove "$wt"')


def test_ship_quiesce_will_not_commit_what_looks_like_a_secret(repo_ticket, world, leaky_gitleaks, capsys):
    repo, t = repo_ticket
    world([{"name": t["id"].lower(), "agent_status": "idle", "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    wt = merged_with_worktree(repo, t)
    (wt / "oops.txt").write_text("hunter2")
    assert ship_main(["--quiesce", t["id"]]) == 1
    assert "secret" in capsys.readouterr().err
    assert git(repo, "for-each-ref", "refs/heads/keep").strip() == ""
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "task/demo"   # as it was found
    assert "oops.txt" in git(wt, "status", "--porcelain")


def test_ship_quiesce_keeps_the_repos_commit_hooks(repo_ticket, world, capsys):
    repo, t = repo_ticket
    world([{"name": t["id"].lower(), "agent_status": "idle", "tab_id": "w1:t1"}], [{"tab_id": "w1:t1", "label": t["id"]}])
    wt = merged_with_worktree(repo, t)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho rejected by hook >&2\nexit 1\n")
    hook.chmod(0o755)
    (wt / "left.txt").write_text("x")
    assert ship_main(["--quiesce", t["id"]]) == 1
    assert "rejected by hook" in capsys.readouterr().err
    assert git(repo, "for-each-ref", "refs/heads/keep").strip() == ""
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "task/demo"
