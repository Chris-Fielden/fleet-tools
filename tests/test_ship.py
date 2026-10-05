"""BD-17: `ship ID` merges a ticket's work through the repo's own finish-task
script. Throwaway repos, a fake finish-task, a temporary FLEET_HOME."""

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT
from fleet import dispatch as dp
from fleet import ticket as tk
from fleet import ship_cli
from fleet.ship_cli import main as ship_main

from fleet import risk as rk
from test_dispatch import claim, git, make_repo, mk, projects  # noqa: F401
from test_risk import commit_in, fake_tool, repo, run_review, task  # noqa: F401

FAKE = """#!/bin/bash
# Fake finish-task: records how it was called, optionally merges, exits as told.
echo "$PWD|$*" >> "$FAKE_LOG"
sleep "${FAKE_SLEEP:-0}"
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
    assert log.read_text().strip() == f"{repo.resolve()}|demo --ticket {t['id']}"
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert f"{t['id']} merged and marked done" in capsys.readouterr().out


def test_ships_a_ticket_in_review_and_marks_it_done(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    tk.set_status(t["id"], "review", allowed_from=("claimed",))
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"]]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert f"{t['id']} merged and marked done" in capsys.readouterr().out


def test_cancelled_review_ticket_stays_in_review(shipping, capsys):
    repo, t, log = shipping
    tk.set_status(t["id"], "review", allowed_from=("claimed",))
    assert ship_main([t["id"]]) == 1
    assert tk.load_ticket(t["id"])["status"] == "review"
    assert "still review" in capsys.readouterr().err


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
    assert err == f"ship: {t['id']} is {status}; only a ticket in review (or still claimed) can be shipped"


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


@pytest.fixture
def real_task(task, env, monkeypatch):
    """The real review script in a task worktree, the fake finish script in the
    repo, fake codex and claude on PATH, a claimed ticket, repo under projects_dir."""
    repo = task.parent.parent
    (repo / "scripts" / "finish-task.sh").write_text(FAKE)
    fake_tool(env / "bin", "codex")
    fake_tool(env / "bin", "claude")
    monkeypatch.setenv("PATH", f"{env / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("FLEET_TICKET", str(ROOT / "bin" / "ticket"))
    monkeypatch.setenv("FAKE_LOG", str(env / "finish.log"))
    monkeypatch.setenv("FAKE_MERGE", "1")
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"projects_dir": str(env)}))
    t = mk(repo="repo")
    claim(t)
    return task, t


def reviewers(wt, full):
    return set(rk.latest_reviews(wt / ".reviews", full))


# review.sh picks both reviewers for high risk

def test_review_sh_picks_both_for_high_risk(real_task, env):
    wt, _ = real_task
    full = commit_in(wt, "k8s/app.yaml")
    r = run_review(wt, env / "bin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HIGH risk: running both reviewers" in r.stdout
    assert reviewers(wt, full) == {"codex", "claude"}


def test_review_sh_keeps_one_reviewer_below_high(real_task, env):
    wt, _ = real_task
    full = commit_in(wt, "fleet/x.py")
    r = run_review(wt, env / "bin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert reviewers(wt, full) == {"codex"}


def test_review_sh_explicit_reviewer_is_respected(real_task, env):
    wt, _ = real_task
    full = commit_in(wt, "k8s/app.yaml")
    r = run_review(wt, env / "bin", REVIEWER="codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert reviewers(wt, full) == {"codex"}


def test_review_sh_without_a_ticket_command_stays_single(real_task, env):
    wt, _ = real_task
    full = commit_in(wt, "k8s/app.yaml")
    r = run_review(wt, env / "bin", FLEET_TICKET="no-such-ticket-cmd")
    assert r.returncode == 0, r.stdout + r.stderr
    assert reviewers(wt, full) == {"codex"}


# ship runs both reviewers when the gate needs them

def test_ship_runs_both_reviewers_when_the_gate_needs_them(real_task, capsys):
    wt, t = real_task
    full = commit_in(wt, "k8s/app.yaml")
    assert ship_main([t["id"]]) == 0
    assert reviewers(wt, full) == {"codex", "claude"}
    assert "running both reviewers" in capsys.readouterr().out
    assert tk.load_ticket(t["id"])["status"] == "done"


def answer(monkeypatch, text):
    """Pretend a terminal is attached and the human types `text` (an exception
    class is raised instead). Returns the list of prompts shown."""
    asked = []
    replies = list(text) if isinstance(text, list) else None

    def fake_input(prompt=""):
        asked.append(prompt)
        if isinstance(text, type):
            raise text
        return replies.pop(0) if replies else text

    monkeypatch.setattr("fleet.ship_cli.sys.stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def test_ship_without_a_terminal_does_not_offer_a_review(real_task, capsys):
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    assert ship_main([t["id"]]) == 0  # the fake finish-task has no gate of its own
    assert not (wt / ".reviews").exists()
    assert "running both reviewers" not in capsys.readouterr().out


def test_ship_offers_a_review_when_the_tip_has_none_and_merges(real_task, env, monkeypatch):
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    run_review(wt, env / "bin")
    full = commit_in(wt, "fleet/y.py")  # a small fix after the review
    assert reviewers(wt, full) == set()
    answer(monkeypatch, "y")
    assert ship_main([t["id"]]) == 0
    assert reviewers(wt, full) == {"codex"}
    assert tk.load_ticket(t["id"])["status"] == "done"


def test_ship_declined_offer_still_goes_to_the_gate(real_task, env, monkeypatch):
    wt, t = real_task
    full = commit_in(wt, "fleet/x.py")
    answer(monkeypatch, "n")
    assert ship_main([t["id"]]) == 1  # the gate refuses before any question about the merge
    assert reviewers(wt, full) == set()
    assert not (env / "finish.log").exists()


def test_ship_offer_review_failure_stops_the_merge(real_task, env, monkeypatch, capsys):
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    (env / "bin" / "codex").write_text("#!/bin/sh\nexit 1\n")
    answer(monkeypatch, "y")
    assert ship_main([t["id"]]) == 1
    assert "did not complete" in capsys.readouterr().err
    assert not (env / "finish.log").exists()


def test_ship_does_not_offer_when_a_review_exists_but_failed(real_task, env, monkeypatch):
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    fake_tool(env / "bin", "codex", verdict="FAIL")
    run_review(wt, env / "bin")
    asked = answer(monkeypatch, "y")
    assert ship_main([t["id"]]) == 1  # the gate refuses before any question
    assert asked == []


def test_ship_does_not_rerun_reviews_that_pass(real_task, env, capsys):
    wt, t = real_task
    commit_in(wt, "k8s/app.yaml")
    r = run_review(wt, env / "bin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ship_main([t["id"]]) == 0
    assert "running both reviewers" not in capsys.readouterr().out


def test_ship_stops_when_the_reviews_cannot_run(real_task, env, capsys):
    wt, t = real_task
    commit_in(wt, "k8s/app.yaml")
    (env / "bin" / "codex").write_text("#!/bin/sh\nexit 1\n")
    assert ship_main([t["id"]]) == 1
    err = capsys.readouterr().err.strip()
    assert "did not complete" in err and "nothing was merged" in err
    assert not (env / "finish.log").exists()
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_merged_while_dispatcher_lock_is_held(shipping, monkeypatch, capsys):
    # a running `dispatch --watch` holds the lock, so the reconcile does nothing
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    with dp.single_instance():
        assert ship_main([t["id"]]) == 0
    out = capsys.readouterr()
    assert "merged, but is still claimed; run `dispatch --reconcile`" in out.out
    assert "not merged" not in out.err
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_bin_ship_runs_as_a_command(shipping, monkeypatch):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "ship"), t["id"]],
                       capture_output=True, text=True, env=dict(os.environ))
    assert r.returncode == 0, r.stderr
    assert tk.load_ticket(t["id"])["status"] == "done"


def test_ship_ctrl_c_at_the_offer_declines_without_a_traceback(real_task, monkeypatch, capsys):
    wt, t = real_task
    full = commit_in(wt, "fleet/x.py")
    answer(monkeypatch, KeyboardInterrupt)
    assert ship_main([t["id"]]) == 1  # same as a no; the gate then refuses
    assert reviewers(wt, full) == set()
    assert "Traceback" not in capsys.readouterr().err


def test_ship_high_risk_never_prompts(real_task, monkeypatch, capsys):
    wt, t = real_task
    full = commit_in(wt, "k8s/app.yaml")
    asked = answer(monkeypatch, t["id"].lower())  # the reviews are no question; the ticket id is, any case
    assert ship_main([t["id"]]) == 0
    assert len(asked) == 1 and "HIGH RISK" in asked[0]
    assert reviewers(wt, full) == {"codex", "claude"}
    assert "running both reviewers" in capsys.readouterr().out


@pytest.mark.parametrize("code, stderr", [(3, ""), (1, "boom\n")])
def test_ship_stops_when_the_gate_is_broken(real_task, env, monkeypatch, capsys, code, stderr):
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    answer(monkeypatch, "y")
    real = ship_cli._gate

    def broken(repo, wt, branch, *fields):
        if fields[-1] == "reviews":
            return subprocess.CompletedProcess([], code, "", stderr)
        return real(repo, wt, branch, *fields)

    monkeypatch.setattr(ship_cli, "_gate", broken)
    assert ship_main([t["id"]]) == 1
    assert "ticket gate --field reviews failed" in capsys.readouterr().err
    assert not (env / "finish.log").exists()


def test_ship_accepts_a_lowercase_or_mixed_case_id(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"].swapcase()]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert f"{t['id']} merged and marked done" in capsys.readouterr().out


def test_ship_lowercase_unknown_id_is_still_refused(shipping, capsys):
    assert ship_main(["zz-999"]) == 1
    assert "no such ticket: ZZ-999" in capsys.readouterr().err


def test_ship_passes_its_own_ticket_when_several_share_the_branch(shipping, monkeypatch):
    repo, t, log = shipping
    newer = [mk(title=f"Newer {w}")["id"] for w in ("quartz harbour", "mellow zebra")]
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"]]) == 0
    assert log.read_text().strip().endswith(f"|demo --ticket {t['id']}")
    assert t["id"] not in newer
