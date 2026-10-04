"""BD-26: review.sh saves a PASS review's SHOULD FIX items on the ticket.

Everything uses a temporary FLEET_HOME, fake reviewers and throwaway repos."""

import sys

from conftest import BIN, new_args, run
from fleet import risk as rk
from test_kaneo import new_ticket
from test_risk import commit_in, fake_tool, repo, run_review, task  # noqa: F401
from test_summaries import SHA, record, stored

REVIEW = """Reviewer: Codex
Commit: {sha}

VERDICT: {verdict}
MUST FIX: {must}
SHOULD FIX: {should}
SUMMARY: fine
"""


def review(env, should, verdict="PASS", must="none", name="r.md"):
    p = env / name
    p.write_text(REVIEW.format(sha=SHA, verdict=verdict, must=must, should=should))
    return p


def leftovers(file, *extra):
    return record(file=file) if not extra else run(
        "review-result", "--branch", "task/demo", "--repo", "fleet-tools", "--file", str(file), *extra)


ITEMS = "\n1. a.py: rename x\n2. b.py: add a test"


def test_should_fix_items_parsing():
    f = rk.should_fix_items
    assert f(REVIEW.format(sha=SHA, verdict="PASS", must="", should=ITEMS)) == "1. a.py: rename x\n2. b.py: add a test"
    assert f("SHOULD FIX: one thing\nSUMMARY: x") == "one thing"
    assert f("**SHOULD FIX:** (none)\nSUMMARY: x") == ""
    assert f("SHOULD FIX: None.\nSUMMARY: x") == ""
    assert f("SHOULD FIX:\nSUMMARY: x") == ""
    assert f("VERDICT: PASS\nSUMMARY: x") == ""
    assert f("SHOULD FIX: late\n") == "late"


def test_pass_saves_a_labelled_note(env):
    tid = new_ticket()
    r = leftovers(review(env, ITEMS), "--leftovers")
    assert r.returncode == 0, r.stderr
    t = stored(tid)
    assert t["verdict"]["result"] == "PASS"
    [note] = t["notes"]
    assert note["text"].startswith("SHOULD FIX leftovers from the Codex review of aaaaaaa (PASS):")
    assert "b.py: add a test" in note["text"]


def test_recording_twice_does_not_duplicate(env):
    tid = new_ticket()
    f = review(env, ITEMS)
    leftovers(f, "--leftovers")
    leftovers(f, "--leftovers")
    assert len(stored(tid)["notes"]) == 1


def test_no_flag_fail_and_empty_save_nothing(env):
    tid = new_ticket()
    leftovers(review(env, ITEMS))
    leftovers(review(env, ITEMS, verdict="FAIL", must="1. broken"), "--leftovers")
    leftovers(review(env, "none"), "--leftovers")
    t = stored(tid)
    assert "notes" not in t or t["notes"] == []
    assert t["verdict"]["result"] == "PASS"  # the last one recorded


def test_long_items_are_truncated_to_fit(env):
    tid = new_ticket()
    r = leftovers(review(env, "x" * 9000), "--leftovers")
    assert r.returncode == 0, r.stderr
    assert len(stored(tid)["notes"][0]["text"]) <= 4000


def test_propose_makes_one_grouped_fix_ticket(env):
    tid = new_ticket()
    f = review(env, ITEMS)
    r = leftovers(f, "--leftovers", "--propose-leftovers")
    assert r.returncode == 0, r.stderr
    leftovers(f, "--leftovers", "--propose-leftovers")  # no second ticket
    out = run("list").stdout
    assert out.count("Leftover review items from") == 1
    fx = [l.split()[0] for l in out.splitlines() if "Leftover review items" in l][0]
    t = stored(fx)
    assert t["parent"] == tid and t["type"] == "fix" and "b.py: add a test" in t["body"]
    assert t["branch"] == f"task/leftovers-{tid.lower()}"


def test_propose_hitting_the_rate_limit_only_warns(env):
    tid = new_ticket()
    for i in range(5):
        r = run("new", *new_args(branch=f"task/n{i}", title=f"t{i}"), extra_env={"CLAUDECODE": "1"})
        assert r.returncode == 0, r.stderr
    r = run("review-result", "--branch", "task/demo", "--repo", "fleet-tools", "--file",
            str(review(env, ITEMS)), "--leftovers", "--propose-leftovers", extra_env={"CLAUDECODE": "1"})
    assert r.returncode == 0
    assert "could not propose" in r.stderr
    assert stored(tid)["verdict"]["result"] == "PASS" and len(stored(tid)["notes"]) == 1


def test_review_sh_saves_leftovers(task, env):
    tid = new_ticket(repo="repo")
    commit_in(task, "fleet/x.py")
    fake = env / "bin" / "codex"
    fake_tool(env / "bin", "codex")
    fake.write_text(fake.read_text().replace('echo "SUMMARY: fake"', 'echo "SHOULD FIX: 1. tidy"\necho "SUMMARY: fake"'))
    wrapper = env / "ticket-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{BIN}" "$@"\n')
    wrapper.chmod(0o755)
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET=str(wrapper))
    assert r.returncode == 0, r.stdout + r.stderr
    t = stored(tid)
    assert t["verdict"]["result"] == "PASS" and "1. tidy" in t["notes"][0]["text"]
    assert len(run("list").stdout.splitlines()) == 1  # no proposal unless asked
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET=str(wrapper), REVIEW_PROPOSE_LEFTOVERS="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Leftover review items" in run("list").stdout


def test_second_reviewers_items_are_added_to_the_existing_ticket(env):
    tid = new_ticket()
    a = review(env, "\n1. a.py: rename x\n2. shared: add a test", name="a.md")
    b = env / "b.md"
    b.write_text(REVIEW.format(sha=SHA, verdict="PASS", must="none",
                               should="\n1. c.py: handle None\n2. shared: add a test").replace("Codex", "Claude (opus)"))
    leftovers(a, "--leftovers", "--propose-leftovers")
    r = leftovers(b, "--leftovers", "--propose-leftovers")
    assert r.returncode == 0, r.stderr
    out = run("list").stdout
    assert out.count("Leftover review items from") == 1
    fx = [l.split()[0] for l in out.splitlines() if "Leftover review items" in l][0]
    body = stored(fx)["body"]
    assert "a.py: rename x" in body and "c.py: handle None" in body
    assert "From the Claude (opus) review of aaaaaaa:" in body
    assert body.count("shared: add a test") == 1  # the duplicate line is not repeated
    # a third recording with nothing new changes nothing
    leftovers(b, "--leftovers", "--propose-leftovers")
    assert stored(fx)["body"] == body
    assert stored(tid)["verdict"]["result"] == "PASS"


def test_review_sh_retries_without_flags_for_an_older_ticket(task, env):
    tid = new_ticket(repo="repo")
    commit_in(task, "fleet/x.py")
    fake_tool(env / "bin", "codex")
    old = env / "ticket-old"
    old.write_text('#!/bin/sh\ncase "$*" in *--leftovers*) echo "unrecognized arguments" >&2; exit 2;; esac\n'
                   f'exec "{sys.executable}" "{BIN}" "$@"\n')
    old.chmod(0o755)
    r = run_review(task, env / "bin", REVIEWER="codex", FLEET_TICKET=str(old))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "retrying without the leftovers flags" in r.stdout
    assert stored(tid)["verdict"]["result"] == "PASS"
