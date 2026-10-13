"""EM-8 step G (BD-84): `ship ID --accept-leftovers --reason TEXT`.

The human's merge of ONE commit despite a Codex FAIL: only on the supervisor's mark for that exact
commit, a saved Claude PASS, never at high risk; terminal only; one use; a permanent note.
Throwaway repo, fake finish-task, temporary FLEET_HOME.
"""

import os
import subprocess
import sys

import pytest

from conftest import ROOT
from fleet import dispatch as dp
from fleet import risk as rk
from fleet import supervisor_run as sr
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import claim, git, make_repo, mk, projects  # noqa: F401
from test_ship import FAKE

LOGGING_FAKE = FAKE.replace('echo "$PWD|$*" >> "$FAKE_LOG"',
                            'echo "$PWD|$*|accept=${FLEET_ACCEPT_LEFTOVERS_COMMIT:-unset}" >> "$FAKE_LOG"')
REASON = "the remaining nits are filed, the fix is needed today"


class Tty:
    def isatty(self):
        return True


@pytest.fixture
def acc(projects, env, monkeypatch):
    """A repo with a task worktree (with .reviews/), a claimed ticket, a Codex FAIL and a Claude PASS
    on the tip, both recorded, and the supervisor's mark on that tip. Typed answers come from
    `typed` (default: the right commit)."""
    repo = make_repo(projects)
    (repo / "scripts" / "finish-task.sh").write_text(LOGGING_FAKE)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake finish")
    wt = repo / ".worktrees" / "demo"
    git(repo, "worktree", "add", "-q", "-b", "task/demo", str(wt))
    (wt / "work.txt").write_text("w")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "work")
    log = env / "finish.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MERGE", "1")
    t = mk()
    claim(t)
    monkeypatch.setattr(sys, "stdin", Tty())

    class Rig:
        pass

    r = Rig()
    r.repo, r.wt, r.t, r.log, r.id = repo, wt, t, log, t["id"]
    r.typed = None

    def tip():
        return git(wt, "rev-parse", "HEAD").strip()
    r.tip = tip

    def review(kind, verdict, commit=None):
        commit = commit or tip()
        (wt / ".reviews").mkdir(exist_ok=True)
        who = "Codex" if kind == "codex" else "Claude (opus)"
        (wt / ".reviews" / f"task-demo-{commit[:7]}-{kind}.md").write_text(
            f"Reviewer: {who}\nCommit: {commit}\n\nVERDICT: {verdict}\nMUST FIX:\nnone\nSUMMARY: s\n")
    r.review = review

    def verdict(kind, result, commit=None):
        who = "Codex" if kind == "codex" else "Claude (opus)"
        tk.record_verdict("task/demo", repo.name, result, commit or tip(), who)
    r.verdict = verdict

    def mark(commit=None, tid=None):
        state = sr.load_state()
        state["leftovers"][tid or r.id] = {"commit": commit or tip(), "reason": "only nits are left",
                                           "ticket": None, "at": "2026-10-12T10:00:00Z"}
        sr.save_state(state)
    r.mark = mark

    def ready():
        r.review("codex", "FAIL")
        r.review("claude", "PASS")
        r.verdict("codex", "FAIL")
        r.verdict("claude", "PASS")
        r.mark()
    r.ready = ready

    def answers(typed):
        monkeypatch.setattr("builtins.input", lambda prompt="": typed() if callable(typed) else typed)
    r.answers = answers
    answers(lambda: r.tip()[:7])

    def run(*extra, reason=REASON):
        args = [r.id, "--accept-leftovers", *(["--reason", reason] if reason is not None else []), *extra]
        return ship_main(args)
    r.run = run

    def nothing_happened(err=None):
        assert not log.exists(), "the merge script ran"
        assert tk.load_ticket(r.id)["status"] == "claimed"
        assert not [n for n in tk.load_ticket(r.id).get("notes", []) if "LEFTOVERS ACCEPTED" in n["text"]]
    r.nothing_happened = nothing_happened
    return r


# --- the one place that tolerates a failed review -------------------------------------------------

def rev(kind, verdict, commit="a" * 40, who=None):
    return {"kind": kind, "verdict": verdict, "commit": commit, "file": f"{kind}.md",
            "reviewer": who or ("Codex" if kind == "codex" else "Claude (opus)")}


def test_check_reviews_tolerates_only_a_codex_fail_on_the_named_commit():
    c = "a" * 40
    both = {"codex": rev("codex", "FAIL"), "claude": rev("claude", "PASS")}
    assert rk.check_reviews(both, "medium") != []                       # without the commit: blocked as before
    assert rk.check_reviews(both, "medium", c) == []
    assert rk.check_reviews(both, "low", c) == []


@pytest.mark.parametrize("reviews, level, commit", [
    ({"codex": rev("codex", "FAIL"), "claude": rev("claude", "PASS")}, "high", "a" * 40),     # never high
    ({"codex": rev("codex", "FAIL"), "claude": rev("claude", "FAIL")}, "medium", "a" * 40),   # never a Claude FAIL
    ({"codex": rev("codex", "FAIL")}, "medium", "a" * 40),                                    # Claude missing
    ({"codex": rev("codex", "FAIL"), "claude": rev("claude", "PASS")}, "medium", "b" * 40),   # another commit
    ({"codex": rev("codex", "PASS"), "claude": rev("claude", "FAIL")}, "medium", "a" * 40),   # not a Codex FAIL
    ({"codex": rev("codex", None), "claude": rev("claude", "PASS")}, "medium", "a" * 40),     # unreadable
    ({"claude": rev("claude", "PASS", "b" * 40), "codex": rev("codex", "FAIL")}, "medium", "a" * 40),
    ({}, "medium", "a" * 40),
])
def test_check_reviews_refuses_everything_else(reviews, level, commit):
    assert rk.check_reviews(reviews, level, commit) != []


# --- ship --accept-leftovers ----------------------------------------------------------------------

def test_merges_records_who_what_why_and_consumes_the_mark(acc, capsys):
    acc.ready()
    tip = acc.tip()
    assert acc.run() == 0
    # run with the commit in the environment, for this one run
    assert f"|accept={tip}" in acc.log.read_text()
    t = tk.load_ticket(acc.id)
    assert t["status"] == "done"
    (note,) = [n for n in t["notes"] if n["text"].startswith("LEFTOVERS ACCEPTED")]
    text = note["text"]
    assert "(human)" in text and f"merged {tip} despite Codex FAIL" in text
    assert f"task-demo-{tip[:7]}-codex.md" in text
    assert f"human reason: {REASON}" in text and "supervisor reason: only nits are left" in text
    assert acc.id not in sr.load_state()["leftovers"]            # one use
    logged = dp.log_path().read_text()
    assert f"ACCEPT_LEFTOVERS {acc.id} {tip}" in logged and REASON in logged


def test_an_inherited_variable_changes_nothing_for_a_plain_ship(acc, monkeypatch, capsys):
    acc.ready()
    monkeypatch.setenv(rk.ACCEPT_ENV, acc.tip())
    assert ship_main([acc.id]) == 1            # still a Codex FAIL: the variable is dropped, not honoured
    assert not acc.log.exists()
    assert acc.id in sr.load_state()["leftovers"]


@pytest.mark.parametrize("what", ["no_mark", "old_mark", "other_ticket", "no_verdict", "claude_fail",
                                  "no_claude_file", "codex_pass", "no_codex_file", "verdict_only_codex"])
def test_refusals_merge_nothing_note_nothing_and_keep_the_mark(acc, what, capsys):
    acc.ready()
    if what == "no_mark":
        state = sr.load_state()
        state["leftovers"].clear()
        sr.save_state(state)
    elif what == "old_mark":   # the branch moved after the supervisor marked it
        (acc.wt / "more.txt").write_text("m")
        git(acc.wt, "add", "-A")
        git(acc.wt, "commit", "-q", "-m", "more")
        acc.review("codex", "FAIL")
        acc.review("claude", "PASS")
        acc.verdict("codex", "FAIL")
        acc.verdict("claude", "PASS")
    elif what == "other_ticket":
        state = sr.load_state()
        state["leftovers"] = {"BD-999": state["leftovers"].pop(acc.id)}
        sr.save_state(state)
    elif what == "no_verdict":
        t = tk.load_ticket(acc.id)
        t["verdict"] = None
        tk._write_atomic(tk._ticket_path(acc.id), t)
    elif what == "claude_fail":
        acc.review("claude", "FAIL")
        acc.verdict("claude", "FAIL")
    elif what == "no_claude_file":
        next((acc.wt / ".reviews").glob("*-claude.md")).unlink()
    elif what == "codex_pass":
        acc.review("codex", "PASS")
        acc.verdict("codex", "PASS")
    elif what == "no_codex_file":
        next((acc.wt / ".reviews").glob("*-codex.md")).unlink()
    elif what == "verdict_only_codex":
        t = tk.load_ticket(acc.id)
        t["verdict"]["reviews"].pop("claude")
        tk._write_atomic(tk._ticket_path(acc.id), t)
    before = sr.state_path().read_text()
    assert acc.run() == 1
    acc.nothing_happened()
    assert git(acc.repo, "log", "--oneline", "-1", "main").count("Merge") == 0
    assert sr.state_path().read_text() == before or what == "no_mark"
    assert "nothing was merged" in capsys.readouterr().err


def test_never_at_high_risk(acc, capsys):
    acc.ready()
    tk.raise_risk(acc.id, "high")
    assert acc.run() == 1
    acc.nothing_happened()
    assert "high risk" in capsys.readouterr().err
    assert acc.id in sr.load_state()["leftovers"]


def test_a_high_risk_file_in_the_diff_refuses_too(acc, capsys):
    (acc.wt / "CLAUDE.md").write_text("changed\n")
    git(acc.wt, "add", "-A")
    git(acc.wt, "commit", "-q", "-m", "docs rules")
    acc.ready()
    assert acc.run() == 1
    acc.nothing_happened()
    assert "high risk" in capsys.readouterr().err


def test_the_typed_commit_must_match(acc, capsys):
    acc.ready()
    acc.answers("0000000")
    assert acc.run() == 1
    acc.nothing_happened()
    assert acc.id in sr.load_state()["leftovers"]
    assert "cancelled" in capsys.readouterr().err


def test_the_branch_moving_while_the_human_types_merges_nothing(acc, capsys):
    acc.ready()
    typed = acc.tip()[:7]

    def move():
        (acc.wt / "late.txt").write_text("l")
        git(acc.wt, "add", "-A")
        git(acc.wt, "commit", "-q", "-m", "late")
        return typed
    acc.answers(move)
    assert acc.run() == 1
    acc.nothing_happened()
    assert "moved" in capsys.readouterr().err
    assert acc.id in sr.load_state()["leftovers"]


@pytest.mark.parametrize("reason", [None, "", "   ", "two\nlines", "x" * 301])
def test_the_reason_is_required_one_line_and_short(acc, reason, capsys):
    acc.ready()
    assert acc.run(reason=reason) == 1
    acc.nothing_happened()


def test_a_secret_in_the_reason_is_refused(acc, monkeypatch, capsys):
    acc.ready()
    monkeypatch.setattr(tk, "check_secrets", lambda title, body: ["looks like a secret"])
    assert acc.run() == 1
    acc.nothing_happened()
    assert "secret" in capsys.readouterr().err


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_an_agent_cannot_trigger_it(acc, monkeypatch, var, capsys):
    acc.ready()
    monkeypatch.setenv(var, "1")
    assert acc.run() == 1
    acc.nothing_happened()
    assert "only a human" in capsys.readouterr().err


def test_it_needs_a_terminal(acc, monkeypatch, capsys):
    acc.ready()
    monkeypatch.setattr(sys, "stdin", type("NoTty", (), {"isatty": lambda self: False})())
    assert acc.run() == 1
    acc.nothing_happened()
    assert "needs a terminal" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--list"], ["--non-interactive", "a" * 40], ["--quiesce", "BD-1"],
                                   ["--status", "BD-1"]])
def test_it_is_for_one_ticket_from_the_terminal_only(acc, extra, capsys):
    acc.ready()
    with pytest.raises(SystemExit):
        acc.run(*extra)
    acc.nothing_happened()


def test_reason_alone_is_refused(acc):
    with pytest.raises(SystemExit):
        ship_main([acc.id, "--reason", "why"])


# --- the gate's own, independent check --------------------------------------------------------------

def gate(acc, **env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CODEX_SANDBOX")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "ticket"), "gate", "--base", "main", "--branch", "task/demo",
         "--dir", str(acc.repo), "--worktree", str(acc.wt), "--repo", acc.repo.name, "--ticket", acc.id,
         "--field", "ok"], capture_output=True, text=True, env=env)


def test_the_gate_agrees_only_with_the_mark_the_commit_and_a_human(acc, monkeypatch):
    acc.ready()
    assert gate(acc).stdout.strip() == "no"                                # no variable: a Codex FAIL blocks
    assert gate(acc, **{rk.ACCEPT_ENV: acc.tip()}).stdout.strip() == "yes"
    assert gate(acc, **{rk.ACCEPT_ENV: "b" * 40}).stdout.strip() == "no"   # another commit
    assert gate(acc, **{rk.ACCEPT_ENV: acc.tip(), "CLAUDECODE": "1"}).stdout.strip() == "no"
    state = sr.load_state()
    state["leftovers"].clear()
    sr.save_state(state)
    assert gate(acc, **{rk.ACCEPT_ENV: acc.tip()}).stdout.strip() == "no"  # no mark: the variable alone is nothing


def test_a_merge_then_a_failed_push_still_records_and_consumes(acc, monkeypatch, capsys):
    acc.ready()
    tip = acc.tip()
    monkeypatch.setenv("FAKE_EXIT", "1")   # the fake merges, then exits 1, as a failed push would
    assert acc.run() == 1
    err = capsys.readouterr().err
    assert "the acceptance was recorded" in err
    notes = [n["text"] for n in tk.load_ticket(acc.id)["notes"] if n["text"].startswith("LEFTOVERS ACCEPTED")]
    assert len(notes) == 1 and f"merged {tip} despite" in notes[0]
    assert acc.id not in sr.load_state()["leftovers"]


def test_a_refusal_without_a_merge_records_nothing(acc, monkeypatch):
    acc.ready()
    monkeypatch.setenv("FAKE_EXIT", "1")
    monkeypatch.delenv("FAKE_MERGE")
    assert acc.run() == 1
    assert not [n for n in tk.load_ticket(acc.id).get("notes", []) if "LEFTOVERS ACCEPTED" in n["text"]]
    assert acc.id in sr.load_state()["leftovers"]


def test_a_record_that_needs_the_human_ends_non_zero(acc, monkeypatch, capsys):
    acc.ready()

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(sr, "save_state", broken)
    assert acc.run() == 1
    assert "could not be consumed" in capsys.readouterr().err
    assert any(n["text"].startswith("LEFTOVERS ACCEPTED") for n in tk.load_ticket(acc.id)["notes"])
