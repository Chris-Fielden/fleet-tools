import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from fleet import dispatch as dp
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main
from fleet.ticket_cli import main as ticket_main

GIT_ENV = ["-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(["git", *GIT_ENV, "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def projects(env, monkeypatch):
    d = env / "projects"
    d.mkdir()
    monkeypatch.setenv("FLEET_HOME", str(env / "fleet"))
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"projects_dir": str(d)}))
    return d


def make_repo(projects, name="app", scripts=True):
    repo = projects / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("x")
    if scripts:
        (repo / "scripts").mkdir()
        (repo / "scripts" / "new-task.sh").write_text("#!/bin/sh\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def merge_branch(repo, branch):
    git(repo, "checkout", "-q", "-b", branch)
    (repo / f"{branch.split('/')[1]}.txt").write_text("w")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)


def mk(**over):
    f = {"type": "build", "repo": "app", "branch": "task/demo", "title": "T", "body": "B"}
    f.update(over)
    return tk.create_ticket(f)


def claim(t, agent="claude"):
    return tk.set_status(t["id"], "claimed", allowed_from=("open",), assignee=agent)


def dry(capsys):
    assert dispatch_main(["--dry-run"]) == 0
    return capsys.readouterr().out


# --- ticket close ------------------------------------------------------------

@pytest.mark.parametrize("flag, status", [("--done", "done"), ("--failed", "failed"),
                                          ("--cancelled", "cancelled")])
@pytest.mark.parametrize("start", ["open", "claimed"])
def test_close_outcomes(flag, status, start):
    t = mk()
    if start == "claimed":
        claim(t)
    assert ticket_main(["close", t["id"], flag, "--note", "why"]) == 0
    got = tk.load_ticket(t["id"])
    assert got["status"] == status and got["close_note"] == "why"


def test_close_without_note_has_no_field():
    t = mk()
    assert ticket_main(["close", t["id"], "--done"]) == 0
    assert "close_note" not in tk.load_ticket(t["id"])


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_close_human_only(monkeypatch, capsys, var):
    t = mk()
    monkeypatch.setenv(var, "1")
    assert ticket_main(["close", t["id"], "--done"]) == 1
    assert "only a human" in capsys.readouterr().err
    assert tk.load_ticket(t["id"])["status"] == "open"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_close_refused_from_other_statuses(status, capsys):
    t = mk()
    tk.set_status(t["id"], status, allowed_from=("open",))
    assert ticket_main(["close", t["id"], "--done"]) == 1
    assert "can only change from" in capsys.readouterr().err
    assert tk.load_ticket(t["id"])["status"] == status


def test_close_refused_from_proposed(monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    t = mk()
    monkeypatch.delenv("CLAUDECODE")
    assert t["status"] == "proposed"
    assert ticket_main(["close", t["id"], "--cancelled"]) == 1


def test_close_note_length(capsys):
    t = mk()
    assert ticket_main(["close", t["id"], "--done", "--note", "x" * 501]) == 2
    assert "500" in capsys.readouterr().err
    assert ticket_main(["close", t["id"], "--done", "--note", ""]) == 2
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert ticket_main(["close", t["id"], "--done", "--note", "x" * 500]) == 0


def test_close_note_gitleaks_checked(env, monkeypatch, capsys):
    t = mk()
    from conftest import _fake
    monkeypatch.setenv("FLEET_GITLEAKS", str(_fake(env / "gl-leak", 1)))
    assert ticket_main(["close", t["id"], "--done", "--note", "secret"]) == 2
    assert "secret" in capsys.readouterr().err
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_stored_close_note_validated(env):
    t = mk()
    path = tk._ticket_path(t["id"])
    data = json.loads(path.read_text())
    data["close_note"] = ""
    path.write_text(json.dumps(data))
    with pytest.raises(tk.TicketError, match="close_note"):
        tk.load_ticket(t["id"])


# --- settings ----------------------------------------------------------------

def test_settings_defaults(env):
    s = dp.load_settings()
    assert s["builder"] == "claude" and s["models"] == {"claude": "sonnet", "codex": None}
    assert s["max_running"] == 1 and s["herdr_workspace"] == "fleet"
    assert s["projects_dir"] == Path("~/projects").expanduser()


def test_settings_override(env):
    (env / "fleet").mkdir()
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"builder": "codex", "models": {"codex": "gpt-x"}, "max_running": 3,
         "projects_dir": "/srv/p", "herdr_workspace": "w"}))
    s = dp.load_settings()
    assert s["builder"] == "codex" and s["models"] == {"claude": "sonnet", "codex": "gpt-x"}
    assert s["max_running"] == 3 and s["projects_dir"] == Path("/srv/p") and s["herdr_workspace"] == "w"


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"bogus": 1}', '{"builder": "grok"}', '{"max_running": 0}',
    '{"max_running": true}', '{"max_running": "2"}', '{"models": {"grok": "x"}}',
    '{"models": {"claude": 5}}', '{"models": []}', '{"projects_dir": ""}',
    '{"herdr_workspace": null}',
])
def test_settings_invalid(env, capsys, content):
    (env / "fleet").mkdir()
    (env / "fleet" / "dispatch.json").write_text(content)
    with pytest.raises(dp.SettingsError):
        dp.load_settings()
    assert dispatch_main(["--dry-run"]) == 2
    assert "dispatch:" in capsys.readouterr().err


# --- rules -------------------------------------------------------------------

def test_rule_type(projects, capsys):
    make_repo(projects)
    b = mk()
    r = mk(type="review", parent=b["id"], commit="abc1234", branch="task/rev")
    q = mk(type="question", parent=b["id"], branch="task/qq")
    out = dry(capsys)
    assert f"{b['id']}  would launch claude (sonnet) in app/.worktrees/demo" in out
    assert f"{r['id']}  skip: handled in phase 4" in out
    assert f"{q['id']}  skip: handled in phase 4" in out


def test_rule_capacity(projects, capsys):
    make_repo(projects)
    claim(mk(branch="task/one"))
    t = mk(branch="task/two")
    assert f"{t['id']}  skip: at capacity" in dry(capsys)
    (projects.parent / "fleet" / "dispatch.json").write_text(
        json.dumps({"projects_dir": str(projects), "max_running": 2}))
    assert "would launch" in dry(capsys)


def test_rule_repo(projects, capsys):
    t = mk()
    assert "does not exist" in dry(capsys)
    plain = projects / "app"
    plain.mkdir()
    assert "is not a git repo" in dry(capsys)
    plain.rmdir()
    repo = make_repo(projects, scripts=False)
    assert "has no scripts/new-task.sh" in dry(capsys)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "new-task.sh").write_text("#!/bin/sh\n")
    assert "uncommitted changes" in dry(capsys)  # untracked scripts/
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "scripts")
    assert "would launch" in dry(capsys)
    git(repo, "checkout", "-q", "-b", "other")
    assert "is on other, not main or master" in dry(capsys)
    git(repo, "checkout", "-q", "main")
    (repo / "README").write_text("changed")
    assert "uncommitted changes" in dry(capsys)
    assert t["id"] in dry(capsys)


def test_rule_repo_master_ok(projects, capsys):
    repo = make_repo(projects)
    git(repo, "branch", "-m", "main", "master")
    mk()
    assert "would launch" in dry(capsys)


def test_rule_repo_dotdot_rejected(projects, capsys):
    make_repo(projects)
    mk(repo="..")
    assert "is not directly inside" in dry(capsys)


def test_rule_branch_existing(projects, capsys):
    repo = make_repo(projects)
    git(repo, "branch", "task/demo")
    mk()
    assert "branch task/demo already exists" in dry(capsys)


def test_rule_branch_claimed_elsewhere(projects, capsys):
    make_repo(projects)
    (projects.parent / "fleet" / "dispatch.json").write_text(
        json.dumps({"projects_dir": str(projects), "max_running": 5}))
    c = claim(mk())
    mk(repo="other")  # different repo, same branch name: still claimed -> blocked
    t2 = mk()
    out = dry(capsys)
    assert f"in use by claimed ticket {c['id']}" in out
    assert t2["id"] in out


def test_rule_fix_may_reuse_branch_and_follows_parent(projects, capsys):
    repo = make_repo(projects)
    parent = mk()
    claim(parent, "codex")
    tk.set_status(parent["id"], "done", allowed_from=("claimed",))
    git(repo, "branch", "task/demo")
    fix = mk(type="fix", parent=parent["id"])
    assert f"{fix['id']}  would launch codex (default) in app/.worktrees/demo" in dry(capsys)


def test_rule_fix_parent_without_assignee(projects, capsys):
    make_repo(projects)
    parent = mk()
    tk.set_status(parent["id"], "done", allowed_from=("open",))
    fix = mk(type="fix", parent=parent["id"], branch="task/fixit")
    assert f"{fix['id']}  skip: parent ticket {parent['id']} has no assignee" in dry(capsys)


def test_rule_builder_setting(projects, capsys):
    make_repo(projects)
    (projects.parent / "fleet" / "dispatch.json").write_text(
        json.dumps({"projects_dir": str(projects), "builder": "codex", "models": {"codex": "m1"}}))
    mk()
    assert "would launch codex (m1)" in dry(capsys)


def test_rule_only_first_eligible(projects, capsys):
    make_repo(projects)
    a = mk(branch="task/a")
    b = mk(branch="task/b")
    out = dry(capsys)
    assert f"{a['id']}  would launch" in out
    assert f"{b['id']}  skip: only one ticket is started per run" in out


# --- dry run changes nothing -------------------------------------------------

def snapshot(repo):
    return git(repo, "status", "--porcelain"), git(repo, "branch", "-a"), git(repo, "log", "--oneline")


def test_dry_run_changes_nothing(projects, capsys):
    repo = make_repo(projects)
    a, b = mk(branch="task/a"), mk(branch="task/b", type="question", parent=None)
    before = ({p.name: p.read_text() for p in tk.tickets_dir().glob("T-*.json")}, snapshot(repo))
    dry(capsys)
    after = ({p.name: p.read_text() for p in tk.tickets_dir().glob("T-*.json")}, snapshot(repo))
    assert before == after
    log = (projects.parent / "fleet" / "dispatch.log").read_text().splitlines()
    assert len(log) == 2 and all(line[:4].isdigit() and "DECISION" in line for line in log)
    assert "B" not in "".join(log).split(a["id"], 1)[1].replace("DECISION", "")  # no bodies logged


# --- reconcile ---------------------------------------------------------------

def test_reconcile(projects, capsys):
    repo = make_repo(projects)
    merged = claim(mk(branch="task/merged"))
    unmerged = claim(mk(branch="task/unmerged"))
    merge_branch(repo, "task/merged")
    git(repo, "branch", "task/unmerged")
    assert dispatch_main(["--reconcile"]) == 0
    out = capsys.readouterr().out
    assert merged["id"] in out and unmerged["id"] not in out
    assert tk.load_ticket(merged["id"])["status"] == "done"
    assert tk.load_ticket(unmerged["id"])["status"] == "claimed"


def test_reconcile_needs_exact_message(projects, capsys):
    repo = make_repo(projects)
    t = claim(mk(branch="task/abc"))
    merge_branch(repo, "task/abcd")  # "Merge task/abcd" must not match task/abc
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_reconcile_needs_whole_message(projects):
    repo = make_repo(projects)
    t = claim(mk(branch="task/extra"))
    git(repo, "checkout", "-q", "-b", "task/extra")
    (repo / "e.txt").write_text("e")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge task/extra", "-m", "more text", "task/extra")
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_set_status_rejects_bad_assignee():
    t = mk()
    with pytest.raises(tk.TicketError, match="assignee"):
        tk.set_status(t["id"], "claimed", allowed_from=("open",), assignee="bob")
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_set_status_can_clear_assignee():
    t = claim(mk())
    got = tk.set_status(t["id"], "open", allowed_from=("claimed",), assignee=None)
    assert got["assignee"] is None


def test_reconcile_ignores_open_tickets(projects):
    repo = make_repo(projects)
    t = mk(branch="task/x")
    merge_branch(repo, "task/x")
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "open"


# --- agents and the lock -----------------------------------------------------

@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
@pytest.mark.parametrize("mode", ["--dry-run", "--reconcile"])
def test_refuses_inside_agent(projects, monkeypatch, capsys, var, mode):
    monkeypatch.setenv(var, "1")
    assert dispatch_main([mode]) == 1
    assert "refusing to run inside an agent" in capsys.readouterr().err
    assert not (projects.parent / "fleet" / "dispatch.log").exists()


def test_lock_stops_second_dispatcher(projects, capsys):
    mk()
    with dp.single_instance():
        assert dispatch_main(["--dry-run"]) == 0
        assert "another dispatch is already running" in capsys.readouterr().out
        assert dispatch_main(["--reconcile"]) == 0
    capsys.readouterr()
    assert "another dispatch" not in dry(capsys)


def test_requires_a_mode(capsys):
    with pytest.raises(SystemExit) as e:
        dispatch_main([])
    assert e.value.code == 2
