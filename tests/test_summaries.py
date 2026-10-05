"""BD-16: agent summaries and review verdicts on the ticket card.

Everything uses a temporary FLEET_HOME and throwaway git repos."""

import json
import os
import subprocess

import pytest

from conftest import BIN, ROOT, new_args, run
from fake_kaneo import FakeKaneo
from fleet import kaneo, launch
from fleet import ticket as tk
from test_kaneo import API_KEY, edit_ticket, fleet_home, new_ticket, only_task, write_config
from test_risk import commit_in, fake_tool, git, repo, run_review, task, write  # noqa: F401

REVIEW = "Reviewer: {who}\nCommit: {sha}\n\nVERDICT: {verdict}\nSUMMARY: fine\n"


def review_file(env, name, sha, who="Codex", verdict="PASS"):
    p = env / name
    p.write_text(REVIEW.format(who=who, sha=sha, verdict=verdict))
    return p


def record(branch="task/demo", repo_name="fleet-tools", file=None):
    return run("review-result", "--branch", branch, "--repo", repo_name, "--file", str(file))


def stored(tid):
    return json.loads((fleet_home() / "tickets" / f"{tid}.json").read_text())


SHA = "a" * 40
SHA2 = "b" * 40


# --- 1. the dispatcher's starting prompt ---------------------------------------

def test_prompt_asks_for_a_saved_summary_note():
    text = launch.PROMPT.format(id="BD-9")
    assert 'ticket note BD-9 "SUMMARY"' in text
    assert "Do not\nstop until that note is saved" in text
    # the note comes after the review step
    assert text.index("scripts/review.sh") < text.index("ticket note BD-9")


# --- 2. review.sh records the verdict ------------------------------------------

def test_review_result_records_verdict(env):
    tid = new_ticket()
    r = record(file=review_file(env, "r.md", SHA, "Claude (sonnet)", "FAIL"))
    assert r.returncode == 0, r.stderr
    assert f"{tid} verdict FAIL recorded" in r.stdout
    v = stored(tid)["verdict"]
    assert (v["result"], v["commit"], v["reviewer"]) == ("FAIL", SHA, "Claude (sonnet)")
    assert tk._is_time(v["at"])
    assert tk.validate_stored(stored(tid)) == []


def test_two_reviewers_of_one_commit_combine_and_fail_wins(env):
    tid = new_ticket()
    record(file=review_file(env, "a.md", SHA, "Codex", "PASS"))
    record(file=review_file(env, "b.md", SHA, "Claude (opus)", "FAIL"))
    v = stored(tid)["verdict"]
    assert v["result"] == "FAIL"
    assert {k: r["result"] for k, r in v["reviews"].items()} == {"codex": "PASS", "claude": "FAIL"}
    assert "Codex: PASS, Claude (opus): FAIL" in tk.verdict_line(v)
    record(file=review_file(env, "c.md", SHA, "Claude (opus)", "PASS"))  # fixed and re-reviewed
    assert stored(tid)["verdict"]["result"] == "PASS"


def test_a_review_of_a_new_commit_replaces_the_verdict(env):
    tid = new_ticket()
    record(file=review_file(env, "a.md", SHA, "Codex", "FAIL"))
    record(file=review_file(env, "b.md", SHA2, "Codex", "PASS"))
    v = stored(tid)["verdict"]
    assert (v["result"], v["commit"], list(v["reviews"])) == ("PASS", SHA2, ["codex"])


def test_only_open_or_claimed_tickets_on_that_branch_and_repo_get_it(env):
    other_branch = new_ticket(branch="task/other")
    other_repo = new_ticket(repo="other-repo")
    closed = new_ticket(title="Closed job")
    edit_ticket(closed, status="done")
    r = record(file=review_file(env, "a.md", SHA))
    assert "nothing recorded" in r.stdout and r.returncode == 0
    for tid in (other_branch, other_repo, closed):
        assert stored(tid)["verdict"] is None


@pytest.mark.parametrize("text", ["Reviewer: Codex\nCommit: " + SHA + "\n\nno verdict here\n",
                                  "Reviewer: Codex\n\nVERDICT: PASS\n",
                                  "Reviewer: Someone\nCommit: " + SHA + "\n\nVERDICT: PASS\n"])
def test_unusable_review_files_record_nothing(env, text):
    tid = new_ticket()
    f = env / "bad.md"
    f.write_text(text)
    assert record(file=f).returncode != 0
    assert stored(tid)["verdict"] is None


def test_missing_review_file_is_refused(env):
    new_ticket()
    assert record(file=env / "nope.md").returncode == 2


def test_stored_verdict_validation():
    base = {"result": "PASS", "commit": SHA, "reviewer": "Codex", "at": "2026-01-01T00:00:00Z",
            "reviews": {"codex": {"result": "PASS", "reviewer": "Codex"}}}
    assert tk._check_verdict(base) == []
    for bad in ({**base, "result": "MAYBE"}, {**base, "commit": "abc"}, {**base, "reviews": {}}, {**base, "reviews": {"codex": "PASS"}},
                {**base, "reviews": {"grok": {"result": "PASS", "reviewer": "Grok"}}}, {**base, "at": "yesterday"}, "PASS",
                {**base, "reviews": {"codex": {"result": "FAIL", "reviewer": "Codex"}}},  # aggregate PASS hides a FAIL
                {k: v for k, v in base.items() if k != "at"}):
        assert tk._check_verdict(bad)


def test_review_sh_records_on_the_ticket(task, env):
    tid = new_ticket(repo="repo")  # the fixture repo's folder is named "repo"
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    wrapper = env / "ticket-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec "{os.sys.executable}" "{BIN}" "$@"\n')
    wrapper.chmod(0o755)
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET=str(wrapper))
    assert r.returncode == 0, r.stdout + r.stderr
    v = stored(tid)["verdict"]
    assert (v["result"], v["commit"], v["reviewer"]) == ("PASS", full, "Codex")


def test_review_sh_survives_a_failing_ticket_command(task, env):
    full = commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    broken = env / "ticket-broken"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET=str(broken))
    assert r.returncode == 0 and "could not be recorded" in r.stdout
    assert (task / ".reviews" / f"task-demo-{full[:7]}-codex.md").exists()


def test_review_sh_survives_a_missing_ticket_command(task, env):
    commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET="no-such-ticket-cmd")
    assert r.returncode == 0 and "no ticket command" in r.stdout


# --- 3. the Kaneo card -----------------------------------------------------------

@pytest.fixture
def board(env):
    """A fake Kaneo, plus a projects folder holding a real fleet-tools repo
    whose branch task/demo has one commit. Returns (fake, repo, tip)."""
    fake = FakeKaneo(API_KEY).start()
    write_config(fake.url)
    projects = env / "projects"
    r = projects / "fleet-tools"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    write(r, "README.md")
    git(r, "add", "-A"); git(r, "commit", "-qm", "base")
    git(r, "checkout", "-q", "-b", "task/demo")
    tip = commit_in(r, "fleet/x.py")
    git(r, "checkout", "-q", "main")
    (fleet_home() / "dispatch.json").write_text(json.dumps({"projects_dir": str(projects)}))
    yield fake, r, tip
    fake.stop()


def labels(fake):
    return fake.label_names(next(iter(fake.tasks)))


def test_card_shows_verdict_and_latest_note_at_the_top(board, env):
    fake, _, tip = board
    tid = new_ticket()
    assert run("note", tid, "first remark").returncode == 0
    assert run("note", tid, "the final summary").returncode == 0
    record(file=review_file(env, "a.md", tip))
    desc = only_task(fake)["description"]
    assert desc.startswith(f"**Review verdict:** PASS (Codex: PASS) on commit {tip}")
    top = desc.split("---")[0]
    assert "the final summary" in top and "first remark" not in top
    assert "first remark" in desc  # earlier notes stay lower down
    assert desc.index("the final summary") < desc.index("Details.")


def test_card_without_verdict_or_notes_is_unchanged_shape(board):
    fake, _, _ = board
    new_ticket()
    assert only_task(fake)["description"].startswith("Details.")


def test_the_label_waits_for_the_review_status(board, env):
    """A recorded PASS alone is not enough: only a ticket the dispatcher moved
    to review carries the label."""
    fake, _, tip = board
    tid = new_ticket()
    edit_ticket(tid, status="claimed")
    record(file=review_file(env, "a.md", tip))
    assert "Ready to ship" not in labels(fake)
    edit_ticket(tid, status="review")
    assert run("sync").returncode == 0
    assert "Ready to ship" in labels(fake)
    edit_ticket(tid, status="claimed")  # moved back: the label goes
    assert run("sync").returncode == 0
    assert "Ready to ship" not in labels(fake)


def test_ready_to_ship_only_for_a_pass_of_the_current_last_commit(board, env):
    fake, repo_dir, tip = board
    edit_ticket(new_ticket(), status="review")
    assert "Ready to ship" not in labels(fake)
    record(file=review_file(env, "a.md", tip))
    assert "Ready to ship" in labels(fake)
    # the branch moves on: the old PASS no longer counts once the card is synced
    git(repo_dir, "checkout", "-q", "task/demo")
    commit_in(repo_dir, "fleet/y.py")
    git(repo_dir, "checkout", "-q", "main")
    assert run("sync").returncode == 0
    assert "Ready to ship" not in labels(fake)
    assert any(n.startswith("risk: ") for n in labels(fake))


def test_a_fail_never_gets_the_label(board, env):
    fake, _, tip = board
    edit_ticket(new_ticket(), status="review")
    record(file=review_file(env, "a.md", tip, verdict="FAIL"))
    assert "Ready to ship" not in labels(fake)


def test_label_is_dropped_when_a_second_reviewer_fails(board, env):
    fake, _, tip = board
    edit_ticket(new_ticket(), status="review")
    record(file=review_file(env, "a.md", tip, "Codex", "PASS"))
    assert "Ready to ship" in labels(fake)
    record(file=review_file(env, "b.md", tip, "Claude (opus)", "FAIL"))
    assert "Ready to ship" not in labels(fake)


def test_high_risk_needs_both_reviewers_for_the_label(board, env):
    fake, _, tip = board
    tid = new_ticket()
    edit_ticket(tid, risk="high", status="review")
    record(file=review_file(env, "a.md", tip, "Codex", "PASS"))
    assert "Ready to ship" not in labels(fake)
    record(file=review_file(env, "b.md", tip, "Claude (opus)", "PASS"))
    assert "Ready to ship" in labels(fake)


def test_files_that_make_the_branch_high_risk_need_both_reviewers_and_a_strong_claude(board, env):
    """The ticket says medium, but the branch changes a high-risk file (the
    review script), so the gate would want Codex and Claude opus or fable."""
    fake, repo_dir, _ = board
    git(repo_dir, "checkout", "-q", "task/demo")
    tip = commit_in(repo_dir, "scripts/review.sh")
    git(repo_dir, "checkout", "-q", "main")
    edit_ticket(new_ticket(), status="review")
    record(file=review_file(env, "a.md", tip, "Codex", "PASS"))
    assert "Ready to ship" not in labels(fake)
    record(file=review_file(env, "b.md", tip, "Claude (sonnet)", "PASS"))
    assert "Ready to ship" not in labels(fake)  # sonnet is not strong enough
    record(file=review_file(env, "c.md", tip, "Claude (opus)", "PASS"))
    assert "Ready to ship" in labels(fake)


def test_no_label_when_the_branch_cannot_be_found(board, env):
    fake, _, tip = board
    new_ticket(branch="task/missing")
    record(branch="task/missing", file=review_file(env, "a.md", tip))
    assert "Ready to ship" not in labels(fake)


def test_no_label_when_settings_are_unreadable(board, env):
    fake, _, tip = board
    new_ticket()
    (fleet_home() / "dispatch.json").write_text("{not json")
    record(file=review_file(env, "a.md", tip))
    assert "Ready to ship" not in labels(fake)


# --- 4. ticket show ----------------------------------------------------------------

def test_ticket_show_prints_notes_and_verdict(env):
    tid = new_ticket()
    assert "verdict:" in run("show", tid).stdout and "-" in run("show", tid).stdout
    run("note", tid, "all done, checks pass")
    record(file=review_file(env, "a.md", SHA))
    out = run("show", tid).stdout
    assert f"verdict:     PASS (Codex: PASS) on commit {SHA} at " in out
    assert "all done, checks pass" in out and "--- note by" in out
    assert json.loads(run("show", tid, "--json").stdout)["verdict"]["result"] == "PASS"


# --- the card is information only -----------------------------------------------------

def test_merge_gate_ignores_the_ticket_verdict(task, env):
    """A ticket that says PASS for the exact tip, with no saved review, still blocks."""
    tid = new_ticket(repo="repo")
    full = commit_in(task, "fleet/x.py")
    record(repo_name="repo", file=review_file(env, "a.md", full))
    assert stored(tid)["verdict"]["result"] == "PASS"
    base = ["gate", "--base", "main", "--branch", "task/demo", "--dir", str(task),
            "--repo", "repo", "--worktree", str(task)]
    assert run(*base, "--field", "ok").stdout.strip() == "no"
    assert run(*base).returncode == 1
    # a saved review of that commit is what opens it
    fake_tool(env / "bin", "codex")
    assert run_review(task, env / "bin", REVIEWER="codex").returncode == 0
    assert run(*base, "--field", "ok").stdout.strip() == "yes"
