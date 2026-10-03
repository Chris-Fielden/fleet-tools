"""BD-17: `ship ID` merges a ticket's work through the repo's own finish-task
script. Throwaway repos, a fake finish-task, a temporary FLEET_HOME."""

import os
import subprocess
import sys

import pytest

from conftest import ROOT
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main

from test_dispatch import claim, git, make_repo, mk, projects  # noqa: F401

FAKE = """#!/bin/bash
# Fake finish-task: records how it was called, optionally merges, exits as told.
echo "$PWD|$*" >> "$FAKE_LOG"
[[ -z "${FAKE_MERGE:-}" ]] || git -c user.name=T -c user.email=t@example.com merge -q --no-ff "task/$1" -m "Merge task/$1"
exit "${FAKE_EXIT:-0}"
"""


@pytest.fixture
def shipping(projects, env, monkeypatch):
    """A repo with a fake finish-task, a task branch with work, a claimed ticket."""
    repo = make_repo(projects)
    (repo / "scripts" / "finish-task.sh").write_text(FAKE)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake finish")
    git(repo, "checkout", "-q", "-b", "task/demo")
    (repo / "work.txt").write_text("w")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "work")
    git(repo, "checkout", "-q", "main")
    log = env / "finish.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    t = mk()
    claim(t)
    return repo, t, log


def test_merges_through_finish_task_and_marks_done(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"]]) == 0
    # run from the repo's main checkout, with the task name and nothing else
    assert log.read_text().strip() == f"{repo.resolve()}|demo"
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert f"{t['id']} merged and marked done" in capsys.readouterr().out


def test_cancelled_at_the_prompt_is_not_done(shipping, capsys):
    # finish-task exits 0 when the human answers no; nothing was merged
    repo, t, log = shipping
    assert ship_main([t["id"]]) == 1
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    assert "was not merged" in capsys.readouterr().err


def test_finish_task_refusal_is_one_plain_line(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_EXIT", "1")
    assert ship_main([t["id"]]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["ship: finish-task refused task/demo (exit 1); nothing was reconciled"]
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_unknown_id(projects, capsys):
    assert ship_main(["BD-99"]) == 1
    assert capsys.readouterr().err.strip() == "ship: no such ticket: BD-99"


def test_not_an_id(projects, capsys):
    assert ship_main(["nonsense"]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("ship: unknown ticket ID") and "\n" not in err


@pytest.mark.parametrize("status", ["open", "done"])
def test_wrong_status(projects, capsys, status):
    t = mk()
    if status == "done":
        claim(t)
        tk.set_status(t["id"], "done", allowed_from=("claimed",))
    assert ship_main([t["id"]]) == 1
    err = capsys.readouterr().err.strip()
    assert err == f"ship: {t['id']} is {status}, not claimed; only a claimed ticket can be shipped"


def test_missing_repo_folder(projects, capsys):
    t = mk(repo="ghost")
    claim(t)
    assert ship_main([t["id"]]) == 1
    err = capsys.readouterr().err.strip()
    assert "repo folder" in err and "not found" in err and "\n" not in err


def test_repo_without_finish_task(projects, capsys):
    make_repo(projects)
    t = mk()
    claim(t)
    assert ship_main([t["id"]]) == 1
    assert "has no scripts/finish-task.sh" in capsys.readouterr().err


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_refuses_inside_an_agent(shipping, monkeypatch, capsys, var):
    repo, t, log = shipping
    monkeypatch.setenv(var, "1")
    assert ship_main([t["id"]]) == 1
    assert "refusing to run inside an agent" in capsys.readouterr().err
    assert not log.exists()


def test_bin_ship_runs_as_a_command(shipping, monkeypatch):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "ship"), t["id"]],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    assert tk.load_ticket(t["id"])["status"] == "done"
