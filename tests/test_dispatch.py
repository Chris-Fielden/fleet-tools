import fcntl
import json
import subprocess
import sys
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
    t = mk(type="investigate")
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
    a = mk(branch="task/aa")
    b = mk(branch="task/bb")
    out = dry(capsys)
    assert f"{a['id']}  would launch" in out
    assert f"{b['id']}  skip: only one ticket is started per run" in out


# --- dry run changes nothing -------------------------------------------------

def snapshot(repo):
    return git(repo, "status", "--porcelain"), git(repo, "branch", "-a"), git(repo, "log", "--oneline")


def test_dry_run_changes_nothing(projects, capsys):
    repo = make_repo(projects)
    a, b = mk(branch="task/a"), mk(branch="task/b", type="question", parent=None)
    before = ({p.name: p.read_text() for p in tk.tickets_dir().glob("*")}, snapshot(repo))
    dry(capsys)
    after = ({p.name: p.read_text() for p in tk.tickets_dir().glob("*")}, snapshot(repo))
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


# --- reconcile: reused branch, exact whitespace, settings --------------------

def test_claim_records_main_commit_and_reconcile_ignores_older_merge(projects):
    repo = make_repo(projects)
    old = claim(mk(branch="task/again"))
    merge_branch(repo, "task/again")  # an earlier ticket's work, merged long ago
    tk.set_status(old["id"], "done", allowed_from=("claimed",))
    base = git(repo, "rev-parse", "main").strip()
    reused = tk.set_status(mk(branch="task/again", type="fix", parent=old["id"])["id"],
                           "claimed", allowed_from=("open",), assignee="claude", base_commit=base)
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(reused["id"])["status"] == "claimed"  # old merge doesn't count
    git(repo, "checkout", "-q", "task/again")
    (repo / "again2.txt").write_text("more")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge task/again", "task/again")
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(reused["id"])["status"] == "done"


def test_base_commit_is_validated():
    t = mk()
    with pytest.raises(tk.TicketError, match="base_commit"):
        tk.set_status(t["id"], "claimed", allowed_from=("open",), base_commit="nope")
    data = dict(tk.load_ticket(t["id"]), base_commit="nope")
    assert any("base_commit" in e for e in tk.validate_stored(data))


@pytest.mark.parametrize("message", [" Merge task/sp", "Merge task/sp ", "Merge  task/sp"])
def test_reconcile_whitespace_must_match(projects, message):
    repo = make_repo(projects)
    t = claim(mk(branch="task/sp"))
    git(repo, "checkout", "-q", "-b", "task/sp")
    (repo / "s.txt").write_text("s")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "--cleanup=verbatim", "-m", message, "task/sp")
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_settings_unknown_home_folder(env):
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "dispatch.json").write_text(
        json.dumps({"projects_dir": "~no-such-user-xyz/projects"}))
    with pytest.raises(dp.SettingsError, match="home folder"):
        dp.load_settings()
    assert dispatch_main(["--dry-run"]) == 2


# --- dispatch --once (fake herdr) --------------------------------------------

REAL_NEW_TASK = Path(__file__).resolve().parent.parent / "scripts" / "new-task.sh"


@pytest.fixture
def herdr_log(env, monkeypatch):
    log = env / "herdr.log"
    monkeypatch.setenv("FLEET_HERDR", f"{sys.executable} {Path(__file__).parent / 'fake_herdr.py'}")
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.setattr("fleet.launch.POLL_INTERVAL", 0.01)
    monkeypatch.setattr("fleet.launch.START_TIMEOUT", 0.3)
    return log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def real_repo(projects, name="app"):
    repo = make_repo(projects, name)
    (repo / "scripts" / "new-task.sh").write_text(REAL_NEW_TASK.read_text())
    (repo / ".gitignore").write_text(".worktrees/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "real new-task")
    return repo


def test_once_full_run(projects, herdr_log, capsys):
    repo = real_repo(projects)
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(t["id"])
    assert got["status"] == "claimed" and got["assignee"] == "claude"
    assert got["base_commit"] == git(repo, "rev-parse", "main").strip()
    wt = repo / ".worktrees" / "job"
    assert wt.is_dir()
    gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").strip())
    assert (gitdir / "fleet-writer").read_text().split() == ["claude", t["id"]]
    assert "fleet-writer" not in git(wt, "status", "--porcelain")
    c = calls(herdr_log)
    name = t["id"].lower()
    assert ["workspace", "list"] in c
    assert ["tab", "create", "--workspace", "w1", "--cwd", str(wt),
            "--label", t["id"], "--no-focus"] in c
    start = next(x for x in c if x[:2] == ["agent", "start"])
    assert start == ["agent", "start", name, "--kind", "claude", "--pane", "w1:p7",
                     "--", "--model", "sonnet"]
    prompt = next(x for x in c if x[:2] == ["agent", "prompt"])
    assert prompt[2] == name and f"You are working ticket {t['id']}" in prompt[3]
    assert ["tab", "close", "w1:t7"] not in c
    assert "launched claude (sonnet) in tab w1:t7" in capsys.readouterr().out


def test_once_launch_failure_closes_tab_and_reopens(projects, herdr_log, monkeypatch, capsys):
    real_repo(projects)
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 1
    got = tk.load_ticket(t["id"])
    assert got["status"] == "open" and got["assignee"] is None
    c = calls(herdr_log)
    assert [x for x in c if x[:2] == ["tab", "close"]] == [["tab", "close", "w1:t7"]]
    assert "LAUNCH_FAILED" in (env_log := dp.log_path().read_text()) and t["id"] in env_log


def test_once_unknown_workspace_fails_without_closing_anything(projects, herdr_log, monkeypatch):
    real_repo(projects)
    monkeypatch.setenv("FAKE_HERDR_WORKSPACE", "other")
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not any(x[:2] == ["tab", "close"] for x in calls(herdr_log))


def test_once_never_passes_bypass_flags(projects, herdr_log):
    real_repo(projects)
    mk(branch="task/job")
    assert dispatch_main(["--once"]) == 0
    flags = [a for c in calls(herdr_log) for a in c if a.startswith("-")]
    assert flags and set(flags) <= {"--workspace", "--cwd", "--label", "--no-focus", "--kind",
                                    "--pane", "--", "--model"}


def test_once_fix_goes_to_parent_agent_and_reuses_folder(projects, herdr_log):
    repo = real_repo(projects)
    parent = mk(branch="task/job")
    tk.set_status(parent["id"], "done", allowed_from=("open",), assignee="codex")
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "job"), "-b", "task/job")
    fix = mk(type="fix", branch="task/job", parent=parent["id"])
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(fix["id"])["assignee"] == "codex"
    start = next(x for x in calls(herdr_log) if x[:2] == ["agent", "start"])
    assert start[3:5] == ["--kind", "codex"] and "--model" not in start
    assert (repo / ".worktrees" / "job").is_dir()


def test_once_respects_capacity(projects, herdr_log, capsys):
    real_repo(projects)
    mk(branch="task/one")
    second = mk(branch="task/two")
    assert dispatch_main(["--once"]) == 0
    n = len(calls(herdr_log))
    capsys.readouterr()
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(second["id"])["status"] == "open"
    assert len(calls(herdr_log)) == n
    assert "at capacity" in capsys.readouterr().out


def test_once_with_nothing_to_do(projects, herdr_log, capsys):
    assert dispatch_main(["--once"]) == 0
    assert "no open tickets" in capsys.readouterr().out


def test_once_refuses_inside_agent(projects, herdr_log, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert dispatch_main(["--once"]) == 1
    assert not herdr_log.exists()


# --- review fixes: retry, folder checks, rollback ----------------------------

def test_once_retry_after_failed_launch(projects, herdr_log, monkeypatch):
    repo = real_repo(projects)
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 1
    assert not (repo / ".worktrees" / "job").exists()  # cleaned up
    assert git(repo, "branch", "--list", "task/job").strip() == ""
    monkeypatch.setenv("FAKE_HERDR_STATE", "working")
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_failed_launch_keeps_a_branch_that_has_work(projects, herdr_log, monkeypatch):
    repo = real_repo(projects)
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    mk(branch="task/job")
    real_start = __import__("fleet.launch", fromlist=["x"]).start_agent

    def start_and_commit(d, folder, workspace, warn):
        (folder / "w.txt").write_text("w")
        git(folder, "add", "-A")
        git(folder, "commit", "-q", "-m", "work")
        return real_start(d, folder, workspace, warn)
    monkeypatch.setattr("fleet.launch.start_agent", start_and_commit)
    assert dispatch_main(["--once"]) == 1
    assert (repo / ".worktrees" / "job").is_dir()
    assert "CLEANUP_SKIPPED" in dp.log_path().read_text()


def _fix_setup(projects):
    repo = real_repo(projects)
    parent = mk(branch="task/job")
    tk.set_status(parent["id"], "done", allowed_from=("open",), assignee="claude")
    fix = mk(type="fix", branch="task/job", parent=parent["id"])
    return repo, fix


def test_fix_refuses_plain_folder(projects, herdr_log):
    repo, fix = _fix_setup(projects)
    git(repo, "branch", "task/job")
    (repo / ".worktrees" / "job").mkdir(parents=True)
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(fix["id"])["status"] == "open"
    assert not herdr_log.exists()


def test_fix_refuses_symlink_and_wrong_branch(projects, herdr_log):
    repo, fix = _fix_setup(projects)
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "other"), "-b", "task/other")
    (repo / ".worktrees" / "job").symlink_to(repo / ".worktrees" / "other")
    git(repo, "branch", "task/job")
    assert dispatch_main(["--once"]) == 1
    (repo / ".worktrees" / "job").unlink()
    git(repo, "worktree", "add", "-q", str(repo / ".worktrees" / "job"), "task/job")
    git(repo / ".worktrees" / "job", "checkout", "-q", "-b", "task/elsewhere")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(fix["id"])["status"] == "open"
    assert not herdr_log.exists()


def test_writer_file_error_rolls_back(projects, herdr_log, monkeypatch):
    real_repo(projects)
    t = mk(branch="task/job")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("fleet.launch.Path.write_text", boom)
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert "LAUNCH_FAILED" in dp.log_path().read_text()


def test_herdr_reply_that_is_not_an_object(projects, herdr_log, monkeypatch, env):
    real_repo(projects)
    t = mk(branch="task/job")
    stub = env / "null-herdr"
    stub.write_text("#!/bin/sh\necho null\n")
    stub.chmod(0o755)
    monkeypatch.setenv("FLEET_HERDR", str(stub))
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_fix_with_branch_but_no_folder_gets_a_checkout_and_keeps_branch_on_failure(
        projects, herdr_log, monkeypatch):
    repo, fix = _fix_setup(projects)
    git(repo, "branch", "task/job")
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 1
    assert not (repo / ".worktrees" / "job").exists()
    assert git(repo, "branch", "--list", "task/job").strip() == "task/job"  # not ours to delete
    monkeypatch.setenv("FAKE_HERDR_STATE", "working")
    assert dispatch_main(["--once"]) == 0
    assert (repo / ".worktrees" / "job").is_dir()
    assert tk.load_ticket(fix["id"])["status"] == "claimed"


def test_cleanup_uses_the_folders_own_starting_commit(projects, herdr_log, monkeypatch):
    repo = real_repo(projects)
    t = mk(branch="task/job")
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    real = __import__("fleet.launch", fromlist=["x"]).make_worktree

    def advancing(d):  # main moves while new-task.sh runs (e.g. a fetch)
        git(repo, "commit", "-q", "--allow-empty", "-m", "moved")
        return real(d)
    monkeypatch.setattr("fleet.launch.make_worktree", advancing)
    assert dispatch_main(["--once"]) == 1
    assert not (repo / ".worktrees" / "job").exists()
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_tab_that_wont_close_keeps_the_claim(projects, herdr_log, monkeypatch, env):
    repo = real_repo(projects)
    t = mk(branch="task/job")
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    wrapper = env / "no-close-herdr"
    wrapper.write_text(f"#!/bin/sh\n[ \"$1 $2\" = \"tab close\" ] && exit 1\n"
                       f"exec {sys.executable} {Path(__file__).parent / 'fake_herdr.py'} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FLEET_HERDR", str(wrapper))
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    assert (repo / ".worktrees" / "job").is_dir()


def test_bad_fleet_herdr_value_rolls_back(projects, herdr_log, monkeypatch):
    real_repo(projects)
    t = mk(branch="task/job")
    monkeypatch.setenv("FLEET_HERDR", "'unclosed")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"


@pytest.mark.parametrize("branch, ok", [("task/ab", True), ("task/a", False), ("task/-ab", False),
                                        ("task/" + "a" * 41, True), ("task/" + "a" * 42, False)])
def test_branch_name_must_fit_new_task_script(projects, capsys, branch, ok):
    real_repo(projects)
    mk(branch=branch)
    out = dry(capsys)
    assert ("would launch" in out) is ok
    assert ("new-task.sh" in out) is (not ok)


def test_base_commit_is_refreshed_after_new_task_runs(projects, herdr_log):
    repo = real_repo(projects)
    t = mk(branch="task/job")
    real = __import__("fleet.launch", fromlist=["x"]).make_worktree

    def advancing(d):  # new-task.sh fetched an old "Merge task/job" into main
        merge_branch(repo, "task/job")
        git(repo, "branch", "-D", "task/job")
        return real(d)
    import fleet.launch
    old = fleet.launch.make_worktree
    fleet.launch.make_worktree = advancing
    try:
        assert dispatch_main(["--once"]) == 0
    finally:
        fleet.launch.make_worktree = old
    assert tk.load_ticket(t["id"])["base_commit"] == git(repo, "rev-parse", "main").strip()
    assert dispatch_main(["--reconcile"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_log_failure_after_claim_rolls_back(projects, herdr_log, monkeypatch):
    real_repo(projects)
    t = mk(branch="task/job")

    def boom(kind, message):
        if kind == "CLAIMED":
            raise OSError("disk full")
    monkeypatch.setattr("fleet.launch.dp.log", boom)
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_launch_and_log_failing_together_still_reopens(projects, herdr_log, monkeypatch):
    real_repo(projects)
    t = mk(branch="task/job")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("fleet.launch.Path.write_text", boom)  # writer file fails
    monkeypatch.setattr("fleet.launch.dp.log", boom)           # and so does every log write
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_partial_new_task_is_cleaned_up(projects, herdr_log):
    repo = real_repo(projects)
    script = repo / "scripts" / "new-task.sh"
    script.write_text(script.read_text() + "\nexit 3\n")  # fails after making the folder
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "script fails at the end")
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not (repo / ".worktrees" / "job").exists()
    assert git(repo, "branch", "--list", "task/job").strip() == ""


def test_relative_projects_dir_is_made_absolute(env, monkeypatch):
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"projects_dir": "rel/projects"}))
    monkeypatch.chdir(env)
    assert dp.load_settings()["projects_dir"] == env / "rel" / "projects"


def test_startup_wait_is_bounded_by_the_deadline(monkeypatch):
    import fleet.launch as lc
    seen = []
    monkeypatch.setattr(lc, "START_TIMEOUT", 5.0)
    monkeypatch.setattr(lc, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(lc, "agent_state", lambda n, timeout=120: seen.append(timeout) or "idle")
    with pytest.raises(lc.LaunchError):
        monkeypatch.setattr(lc, "START_TIMEOUT", 0.05)
        lc.wait_until_working("x")
    assert seen and max(seen) <= 0.05


def test_symlinked_worktrees_folder_is_refused(projects, herdr_log, env):
    repo = real_repo(projects)
    outside = env / "outside"
    outside.mkdir()
    (repo / ".gitignore").write_text(".worktrees\n")  # a symlink isn't matched by ".worktrees/"
    git(repo, "commit", "-q", "-am", "ignore")
    (repo / ".worktrees").symlink_to(outside)
    t = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert list(outside.iterdir()) == [] and not herdr_log.exists()


def test_decide_picks_by_seq_when_created_in_the_same_second(projects, monkeypatch):
    monkeypatch.setattr(tk, "_now", lambda: "2025-01-01T00:00:00Z")
    make_repo(projects)
    first, second = mk(branch="task/one"), mk(branch="task/two")
    assert first["created_at"] == second["created_at"]
    # Handed over newest-first, and with ids that sort the other way round.
    first = dict(first, id="ZZ-9")
    second = dict(second, id="AA-1")
    ds = dp.decide([second, first], dp.load_settings())
    assert [d.ticket["id"] for d in ds] == ["ZZ-9", "AA-1"]
    assert ds[0].eligible and not ds[1].eligible


# --- urgent first --------------------------------------------------------------

def test_urgent_open_ticket_is_picked_before_older_normal_ones(projects, capsys, monkeypatch):
    make_repo(projects)
    times = iter(f"2025-01-01T00:00:0{i}Z" for i in range(1, 6))
    monkeypatch.setattr(tk, "_now", lambda: next(times))
    old = mk(branch="task/old")
    urgent1 = mk(branch="task/urg-one", priority="urgent")
    new = mk(branch="task/new")
    urgent2 = mk(branch="task/urg-two", priority="urgent")
    out = dry(capsys).splitlines()
    assert [ln.split()[0] for ln in out] == [urgent1["id"], urgent2["id"], old["id"], new["id"]]
    assert "would launch" in out[0]
    assert all("only one ticket is started per run" in ln for ln in out[1:])


@pytest.mark.parametrize("agent_env", [{}, {"CLAUDECODE": "1"}, {"CODEX_SANDBOX": "x"}])
def test_urgent_wins_whoever_filed_it(projects, capsys, monkeypatch, agent_env):
    make_repo(projects)
    old = mk(branch="task/old")
    for k, v in agent_env.items():
        monkeypatch.setenv(k, v)
    urgent = mk(branch="task/urg", priority="urgent")
    assert urgent["status"] == "open"
    for k in agent_env:
        monkeypatch.delenv(k)
    out = dry(capsys)
    assert f"{urgent['id']}  would launch" in out
    assert f"{old['id']}  skip: only one ticket" in out


def test_ineligible_urgent_does_not_block_normal(projects, capsys):
    make_repo(projects)
    urgent = mk(branch="task/urg", priority="urgent", repo="missing")
    normal = mk(branch="task/norm")
    out = dry(capsys)
    assert f"{urgent['id']}  skip:" in out and f"{normal['id']}  would launch" in out
