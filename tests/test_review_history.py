"""BD-67: review_commits on tickets and the read-only review history helpers (temp FLEET_HOME)."""
import json
import subprocess
import threading

import pytest

from fleet import review_history as rh
from fleet import ticket as tk
from test_dispatch import herdr_log, mk, projects  # noqa: F401
from test_review_watch import rig  # noqa: F401

A, B, C = "a" * 40, "b" * 40, "c" * 40


def rounds(rig):
    return rig.get().get("review_commits")


def test_one_round_per_commit_whoever_reviews_and_whatever_the_result(rig):
    tk.record_verdict("task/demo", "app", "PASS", A, "Codex")
    assert rounds(rig) == [A]
    tk.record_verdict("task/demo", "app", "PASS", A, "Claude")
    tk.record_verdict("task/demo", "app", "FAIL", A, "Codex")
    assert rounds(rig) == [A] and tk.review_rounds(rig.get()) == 1
    tk.record_verdict("task/demo", "app", "PASS", B, "Codex")
    assert rounds(rig) == [A, B]
    assert rig.get()["fail_commits"] == [A]   # fail_commits unchanged: only FAILs


def test_cap_keeps_the_newest_and_an_over_cap_ticket_loads(rig):
    shas = [f"{i:040x}" for i in range(1, tk.REVIEW_COMMITS_KEPT + 11)]
    for sha in shas:
        tk.record_verdict("task/demo", "app", "PASS", sha, "Codex")
    assert rounds(rig) == shas[-tk.REVIEW_COMMITS_KEPT:]
    rig.edit(review_commits=shas)
    assert len(rig.get()["review_commits"]) == len(shas)
    tk.record_verdict("task/demo", "app", "PASS", "f" * 40, "Codex")
    assert rounds(rig) == (shas + ["f" * 40])[-tk.REVIEW_COMMITS_KEPT:]


def test_old_ticket_without_the_field_counts_zero_then_gets_it(rig):
    assert "review_commits" not in rig.get() and tk.review_rounds(rig.get()) == 0
    tk.record_verdict("task/demo", "app", "PASS", A, "Codex")
    assert tk.review_rounds(rig.get()) == 1


@pytest.mark.parametrize("bad", ["x", [A, A], [A[:7]], [A.upper()], ["z" * 40], [5], None])
def test_validate_rejects_bad_review_commits(rig, bad):
    data = rig.get()
    data["review_commits"] = bad
    assert any("review_commits" in e for e in tk.validate_stored(data))


def test_validate_accepts_empty_list(rig):
    data = rig.get()
    data["review_commits"] = []
    assert tk.validate_stored(data) == []


def test_closed_tickets_are_untouched_and_a_bad_commit_writes_nothing(rig):
    with pytest.raises(tk.TicketInvalid):
        tk.record_verdict("task/demo", "app", "PASS", "short", "Codex")
    assert "review_commits" not in rig.get()
    rig.edit(status="done")
    tk.record_verdict("task/demo", "app", "PASS", A, "Codex")
    assert "review_commits" not in rig.get()


def test_racing_verdicts_lose_no_round(rig):
    shas = [f"{i:040x}" for i in range(1, 9)]
    ts = [threading.Thread(target=tk.record_verdict, args=("task/demo", "app", "PASS", s, "Codex"))
          for s in shas]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(rounds(rig)) == shas


# --- helpers ------------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / "a.txt").write_text("a")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {name}")
    return git(repo, "rev-parse", "HEAD").strip()


def review(wt, name, kind, sha, verdict, must="none"):
    d = wt / ".reviews"
    d.mkdir(exist_ok=True)
    (d / name).write_text(f"Reviewer: {kind}\nCommit: {sha}\n\nVERDICT: {verdict}\nMUST FIX: {must}\n")


def test_helpers_on_a_branch(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD").strip()
    c1 = commit(repo, "test_t1.py", "def test_one():\n    pass\n")
    c2 = commit(repo, "test_t2.py", "def test_two():\n    pass\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    assert rh.review_history(repo, wt, base, c2) == []          # no .reviews dir
    (wt / ".reviews").mkdir()
    assert rh.review_history(repo, wt, base, c2) == []          # empty dir
    (wt / ".reviews" / "x.txt").write_text("nope")
    (wt / ".reviews" / "bad.md").write_bytes(b"\xff\xfe\x00")
    (wt / ".reviews" / "nocommit.md").write_text("Reviewer: codex\n\nVERDICT: PASS\n")
    review(wt, "2.md", "codex", c2, "FAIL", "- broken thing")
    review(wt, "1.md", "claude", c1, "PASS")
    h = rh.review_history(repo, wt, base, c2)
    assert [r["commit"] for r in h] == [c1, c2]
    assert h[1]["reviews"]["codex"]["verdict"] == "FAIL"
    assert "broken thing" in rh.latest_must_fix(h)
    assert rh.latest_must_fix([]) == ""
    assert rh.added_tests(repo, base, c2) == ["test_t1.py::test_one", "test_t2.py::test_two"]
    assert len(rh.git_log(repo, base, c2)) == 2
    assert rh.git_log(repo, base, "nonsense") == [] and rh.added_tests(repo, base, "nonsense") == []


def test_symlinked_reviews_dir_is_ignored(repo, tmp_path):
    elsewhere = tmp_path / "else"
    elsewhere.mkdir()
    review(elsewhere.parent, "z.md", "codex", A, "PASS")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".reviews").symlink_to(tmp_path / ".reviews")
    assert rh.review_history(repo, wt, "HEAD", "HEAD") == []
