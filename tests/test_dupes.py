"""Duplicate guard: ticket new / idea / alert, ticket dupes, the daily watch check, shared branches."""
import json

import pytest

from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import dupes
from fleet import shortcuts_cli
from fleet import ticket as tk

from conftest import new_args, run
from test_dispatch import claim, dry, make_repo, mk, projects  # noqa: F401

def human_env(monkeypatch):
    for k in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"):
        monkeypatch.delenv(k, raising=False)


def agent_env(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")


def new(*over, **kw):
    return run("new", *new_args(**kw), *over)


def test_similarity_ignores_case_and_punctuation():
    assert dupes.similarity("Fix the Login, bug!", "fix the login bug") == 1.0
    assert dupes.similarity("", "!!") == 0.0
    assert dupes.similarity("Add a cache", "Rewrite the scheduler") < 0.8


@pytest.mark.parametrize("status", ["open", "claimed", "review", "proposed"])
def test_branch_refused_naming_ticket(env, monkeypatch, status):
    human_env(monkeypatch)
    t = mk(branch="task/shared", title="One", **({"type": "investigate"} if status == "proposed" else {}))
    if status in ("claimed", "review"):
        tk.set_status(t["id"], "claimed", allowed_from=("open",), assignee="claude")
    if status == "review":
        tk.set_status(t["id"], "review", allowed_from=("claimed",))
    r = new(repo="app", branch="task/shared", title="Totally different words")
    assert r.returncode == 2
    assert t["id"] in r.stderr and "task/shared" in r.stderr
    assert len(tk.list_tickets()) == 1


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_branch_free_once_ticket_finished(env, monkeypatch, outcome):
    human_env(monkeypatch)
    t = mk(branch="task/shared", title="One")
    tk.close_ticket(t["id"], outcome)
    assert new(repo="app", branch="task/shared", title="Other words").returncode == 0


def test_branch_in_another_repo_is_fine(env, monkeypatch):
    human_env(monkeypatch)
    mk(branch="task/shared", repo="other")
    assert new(repo="app", branch="task/shared", title="Other words").returncode == 0


def test_force_does_not_override_a_branch(env, monkeypatch):
    human_env(monkeypatch)
    mk(branch="task/shared")
    r = new("--force", repo="app", branch="task/shared", title="Other words")
    assert r.returncode == 2


def test_fix_may_share_its_parents_branch(env, monkeypatch):
    human_env(monkeypatch)
    parent = mk(branch="task/shared", title="Parent work")
    r = new(repo="app", type="fix", parent=parent["id"], branch="task/shared", title="Parent work")
    assert r.returncode == 0, r.stderr


def test_similar_title_refused_without_a_terminal(env, monkeypatch):
    human_env(monkeypatch)
    t = mk(title="Catch duplicate tickets!", branch="task/a")
    r = new(repo="app", branch="task/b", title="catch duplicate tickets")
    assert r.returncode == 2
    assert t["id"] in r.stderr and "--force" in r.stderr
    assert len(tk.list_tickets()) == 1


def test_similar_title_force_files_it(env, monkeypatch):
    human_env(monkeypatch)
    mk(title="Catch duplicate tickets", branch="task/a")
    assert new("--force", repo="app", branch="task/b", title="Catch duplicate ticket").returncode == 0
    assert len(tk.list_tickets()) == 2


def test_agent_gets_refusal_with_id_and_no_force(env, monkeypatch):
    agent_env(monkeypatch)
    t = mk(title="Catch duplicate tickets", branch="task/a")
    r = new("--force", repo="app", branch="task/b", title="Catch duplicate tickets")
    assert r.returncode == 2
    assert t["id"] in r.stderr and "note" in r.stderr
    assert len(tk.list_tickets()) == 1


def test_dissimilar_title_and_other_repo_pass(env, monkeypatch):
    human_env(monkeypatch)
    mk(title="Catch duplicate tickets", branch="task/a")
    assert new(repo="app", branch="task/b", title="Rework the web page").returncode == 0
    assert new(repo="other", branch="task/c", title="Catch duplicate tickets").returncode == 0


def test_finished_tickets_do_not_count_for_titles(env, monkeypatch):
    human_env(monkeypatch)
    t = mk(title="Catch duplicate tickets", branch="task/a")
    tk.close_ticket(t["id"], "done")
    assert new(repo="app", branch="task/b", title="Catch duplicate tickets").returncode == 0


def test_interactive_human_is_asked(env, monkeypatch, capsys):
    from fleet import ticket_cli
    human_env(monkeypatch)
    mk(title="Catch duplicate tickets", branch="task/a")
    fields = {"type": "build", "repo": "app", "branch": "task/b", "title": "Catch duplicate tickets", "body": "x"}

    class Tty:
        def __init__(self, answer): self.answer = answer
        def isatty(self): return True
        def readline(self): return self.answer

    monkeypatch.setattr("sys.stdin", Tty("n\n"))
    with pytest.raises(tk.TicketInvalid):
        ticket_cli.guard_duplicates(fields)
    assert "very similar" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", Tty("y\n"))
    ticket_cli.guard_duplicates(fields)


def test_idea_and_alert_are_guarded(env, monkeypatch, capsys):
    human_env(monkeypatch)
    assert shortcuts_cli.idea_main(["Look into the flaky web tests"]) == 0
    capsys.readouterr()
    assert shortcuts_cli.idea_main(["look into the flaky web tests."]) == 2
    assert "very similar" in capsys.readouterr().err
    assert shortcuts_cli.alert_main(["app", "Disk is full on the box"]) == 0
    assert shortcuts_cli.alert_main(["app", "Disk is full on the box"]) == 2
    assert len(tk.list_tickets()) == 2


def test_dupes_command_lists_pairs_and_shared_branches(env, monkeypatch):
    human_env(monkeypatch)
    a = mk(branch="task/tracker", title="Tracker interface")
    b = mk(branch="task/tracker", title="Totally unrelated")  # shares a branch (filed directly)
    c = mk(branch="task/c", title="Tracker interface!")
    d = mk(branch="task/d", title="Something else")
    mk(branch="task/e", title="Tracker interface", repo="other")
    done = mk(branch="task/f", title="Tracker interface")
    tk.close_ticket(done["id"], "done")
    out = run("dupes").stdout.splitlines()
    assert len(out) == 2
    assert any(a["id"] in l and b["id"] in l and "same branch task/tracker" in l for l in out)
    assert any(a["id"] in l and c["id"] in l and "title 1.00" in l for l in out)
    assert not any(b["id"] in l and c["id"] in l for l in out)
    assert d["id"] not in run("dupes").stdout


def test_dupes_command_none(env):
    assert "no likely duplicates" in run("dupes").stdout


def test_three_tickets_on_one_branch_all_pair_up(env):
    ids = [mk(branch="task/tracker-interface", title=f"Work item number {n * 7}")["id"] for n in range(3)]
    found = dupes.pairs(tk.list_tickets())
    assert {(p["a"], p["b"]) for p in found} == {(ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[2])}


# --- dispatch ----------------------------------------------------------------

@pytest.mark.parametrize("holder_status", ["claimed", "review"])
def test_dispatch_never_launches_a_branch_held_by_another(projects, capsys, holder_status):
    make_repo(projects)
    (projects.parent / "fleet" / "dispatch.json").write_text(
        json.dumps({"projects_dir": str(projects), "max_running": 5}))
    held = claim(mk(branch="task/tracker-interface"))
    if holder_status == "review":
        tk.set_status(held["id"], "review", allowed_from=("claimed",))
    others = [mk(branch="task/tracker-interface", title=f"Dupe {n}") for n in range(3)]
    out = dry(capsys)
    assert f"in use by claimed ticket {held['id']}" in out
    assert "would launch" not in out
    assert all(o["id"] in out for o in others)


def test_daily_watch_check_logs_once_a_day(env):
    mk(branch="task/tracker", title="Tracker interface")
    mk(branch="task/tracker", title="Tracker interface two")
    lines = []
    dispatch_cli.check_dupes_daily("2026-10-04", lines.append, lines.append)
    assert len(lines) == 1 and "ticket dupes" in lines[0]
    log = dp.log_path().read_text()
    assert log.count("DUPES_FOUND") == 1
    dispatch_cli.check_dupes_daily("2026-10-04", lines.append, lines.append)
    assert dp.log_path().read_text().count("DUPES_FOUND") == 1
    dispatch_cli.check_dupes_daily("2026-10-05", lines.append, lines.append)
    assert dp.log_path().read_text().count("DUPES_FOUND") == 2


def test_daily_watch_check_quiet_without_dupes(env):
    mk(title="One thing", branch="task/a")
    lines = []
    dispatch_cli.check_dupes_daily("2026-10-04", lines.append, lines.append)
    assert lines == [] and not dp.log_path().exists()


def test_underscores_count_as_punctuation():
    assert dupes.similarity("fix_the_bug", "Fix the bug") == 1.0


def test_a_chain_of_fixes_may_share_the_branch(env, monkeypatch):
    human_env(monkeypatch)
    root = mk(branch="task/x", title="Root job")
    fix1 = mk(type="fix", parent=root["id"], branch="task/x", title="Root job")
    r = new(repo="app", type="fix", parent=fix1["id"], branch="task/x", title="Root job")
    assert r.returncode == 0, r.stderr
    assert dupes.pairs(tk.list_tickets()) == []
