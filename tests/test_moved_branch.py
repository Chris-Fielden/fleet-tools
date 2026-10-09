"""EM-24: a commit after a PASS is re-reviewed on the exact new tip (review_watch), `ship` explains a
moved branch and offers the reviews (ship_cli), and a prep or reconcile agent that changes the main
checkout has its result refused (prep, ID-7 item K). Real git, fake herdr, fake ntfy, temp FLEET_HOME."""

import os
import stat
from datetime import timedelta

import pytest

from conftest import ROOT
from fleet import prep
from fleet import review_watch as rw
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import git
from test_prep import (  # noqa: F401
    FAKE_REVIEW, commit, finish_prep, in_review, log_lines, q, set_agent_status, start_prep,
)
from test_review_watch import Rig, no_dialog, ntfy, rig, sent  # noqa: F401
from test_dispatch import herdr_log, projects  # noqa: F401

# A review.sh whose outcome the test picks: it logs the commit it was asked to review.
REVIEW_MODES = """#!/bin/bash
echo "$(git rev-parse HEAD)" >> "$FAKE_REVIEW_LOG"
case "${FAKE_REVIEW_MODE:-pass}" in
  none) exit 1 ;;
  move) echo late > late.txt; git add -A; git -c user.name=T -c user.email=t@example.com commit -q -m late ;;
esac
result=PASS
[[ "${FAKE_REVIEW_MODE:-pass}" != fail ]] || result=FAIL
PYTHONPATH="$FAKE_ROOT" python3 -c "
import sys
from fleet import ticket as tk
for who in ('Codex', 'Claude (opus)'):
    tk.record_verdict(sys.argv[1], 'app', sys.argv[3], sys.argv[2], who)" "$(git branch --show-current)" "$(git rev-parse HEAD)" "$result"
"""


@pytest.fixture(autouse=True)
def _clean_rereviews():
    rw.wait_rereviews()
    rw._REREVIEWS.clear()
    yield
    rw.wait_rereviews()
    rw._REREVIEWS.clear()


@pytest.fixture
def scripted(rig, env, monkeypatch):
    """The review rig with fake check.sh / review.sh committed on its branch."""
    (rig.wt / "scripts").mkdir(exist_ok=True)
    (rig.wt / "scripts" / "check.sh").write_text("#!/bin/bash\nexit 0\n")
    (rig.wt / "scripts" / "review.sh").write_text(REVIEW_MODES)
    for name in ("check.sh", "review.sh"):
        os.chmod(rig.wt / "scripts" / name, os.stat(rig.wt / "scripts" / name).st_mode | stat.S_IXUSR)
    git(rig.wt, "add", "-A")
    git(rig.wt, "commit", "-q", "-m", "scripts")
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    monkeypatch.setenv("FAKE_REVIEW_LOG", str(env / "review.log"))
    rig.passes = lambda: (rig.verdict("PASS", "Codex"), rig.verdict("PASS", "Claude (opus)"))   # scripts/ is high risk
    return rig


def reviewed(env):
    path = env / "review.log"
    return path.read_text().split() if path.exists() else []


def long_ago(rig):
    """Everything on the ticket so far happened an hour ago (stamps have one-second resolution)."""
    data = tk.load_ticket(rig.id)
    stamp = (rig.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["verdict"]["at"] = stamp
    for n in data["notes"]:
        n["at"] = stamp
    rig.edit(verdict=data["verdict"], notes=data["notes"])


def settle(rig, minutes=1):
    """The cycle that starts the re-review, the wait for its thread, then the cycle that sees it."""
    rig.cycle(minutes)
    rw.wait_rereviews()
    rig.cycle(minutes + 1)


# --- 1. review_watch: a commit after the PASS -------------------------------------------------

def test_commit_after_pass_is_re_reviewed_on_the_new_tip_and_pings_ready(scripted, ntfy, env):
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    assert scripted.get()["status"] == "review" and len(ntfy.requests) == 1
    old = scripted.tip()
    new = scripted.commit("after the pass")        # e.g. leftovers committed after the review
    scripted.cycle(1)
    # the ready state is gone at once, and the review is under way on the new commit
    assert scripted.get()["status"] == "claimed"
    rw.wait_rereviews()
    assert reviewed(env) == [new] and old != new   # exactly the new tip, never the old one
    scripted.cycle(2)
    t = scripted.get()
    assert t["verdict"]["commit"] == new and t["status"] == "review"
    assert any(n["text"].startswith(f"{prep.PASS_NOTE_PREFIX} {new[:8]}") for n in t["notes"])
    assert [m[0] for m in sent(ntfy)] == [f"{scripted.id} ready to ship"] * 2   # one per tip, never twice
    scripted.cycle(3)
    assert len(ntfy.requests) == 2 and reviewed(env) == [new]    # and it is not reviewed again


def test_ticket_claimed_with_a_pass_on_an_older_commit_is_re_reviewed_too(scripted, ntfy, env):
    scripted.passes()
    scripted.summary()
    new = scripted.commit("later")
    scripted.set_agent("idle")
    settle(scripted)
    assert reviewed(env) == [new]
    assert scripted.get()["status"] == "review"
    assert [m[0] for m in sent(ntfy)] == [f"{scripted.id} ready to ship"]


def test_a_working_agent_is_left_to_review_its_own_commit(scripted, ntfy, env):
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    scripted.commit("more")
    scripted.set_agent("working")
    scripted.cycle(1)
    rw.wait_rereviews()
    assert reviewed(env) == [] and scripted.get()["status"] == "claimed"


def test_a_ticket_with_no_agent_is_re_reviewed_too(scripted, ntfy, env):
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    new = scripted.commit("after the pass")
    scripted.no_agent()
    settle(scripted)
    assert reviewed(env) == [new] and scripted.get()["status"] == "review"
    assert len(ntfy.requests) == 2


def test_a_failing_re_review_is_the_usual_fail_not_a_ready_ping(scripted, ntfy, env, monkeypatch):
    monkeypatch.setenv("FAKE_REVIEW_MODE", "fail")
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    scripted.commit("breaks it")
    settle(scripted)
    t = scripted.get()
    assert t["verdict"]["result"] == "FAIL" and t["status"] == "claimed"
    assert len(ntfy.requests) == 1                  # only the first ready ping: a FAIL round never pings
    scripted.cycle(10)                              # agent idle on the failed tip: the usual failure
    assert scripted.get()["status"] == "failed"
    assert any("failed" in m[0] for m in no_dialog(ntfy)[1:])


def test_a_failing_re_review_with_no_agent_starts_one(scripted, ntfy, env, monkeypatch):
    monkeypatch.setenv("FAKE_REVIEW_MODE", "fail")
    relaunched = []
    monkeypatch.setattr(prep, "relaunch_sent_back", lambda t, s, emit, warn: relaunched.append(t["id"]))
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    scripted.commit("breaks it")
    scripted.no_agent()
    settle(scripted)
    assert relaunched == [scripted.id]


def test_a_re_review_that_records_nothing_notes_and_pings_once(scripted, ntfy, env, monkeypatch):
    monkeypatch.setenv("FAKE_REVIEW_MODE", "none")
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    new = scripted.commit("after the pass")
    settle(scripted)
    for minutes in (3, 4):
        scripted.cycle(minutes)
    t = scripted.get()
    assert t["status"] == "claimed" and t["verdict"]["result"] == "PASS" and t["verdict"]["commit"] != new
    assert sum(1 for n in t["notes"] if n["text"].startswith("Needs a human: the re-review of")) == 1
    pings = [m[0] for m in no_dialog(ntfy)]
    assert pings == [f"{scripted.id} ready to ship", f"{scripted.id} needs a human, see notes"]
    assert reviewed(env) == [new]                   # one attempt per tip, not one per cycle


def test_the_branch_moving_during_the_re_review_never_counts(scripted, ntfy, env, monkeypatch):
    monkeypatch.setenv("FAKE_REVIEW_MODE", "move")  # review.sh itself lands another commit, then passes
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    long_ago(scripted)
    first = scripted.commit("after the pass")
    settle(scripted)
    t = scripted.get()
    assert scripted.tip() != first
    assert t["status"] == "claimed"                 # the PASS is of a commit that is not the one asked for
    assert "moved while" in " ".join(n["text"] for n in t["notes"])
    assert [m[0] for m in no_dialog(ntfy)][-1] == f"{scripted.id} needs a human, see notes"


def test_a_re_review_that_cannot_run_pings_instead_of_sitting(rig, ntfy):
    rig.finish()
    rig.cycle()
    rig.commit("after the pass")                    # this rig's folder has no scripts/check.sh
    settle(rig)
    assert rig.get()["status"] == "claimed"
    assert [m[0] for m in no_dialog(ntfy)] == [f"{rig.id} ready to ship", f"{rig.id} needs a human, see notes"]


def test_nothing_is_started_when_the_pass_is_on_the_tip(scripted, ntfy, env):
    scripted.passes()
    scripted.summary()
    scripted.set_agent("idle")
    scripted.cycle()
    scripted.cycle(1)
    rw.wait_rereviews()
    assert reviewed(env) == []


# --- 2. ship: a moved branch is explained and the reviews offered -------------------------------------

def answers(monkeypatch, *replies):
    given = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(given))
    monkeypatch.setattr("fleet.ship_cli.sys.stdin", type("TTY", (), {"isatty": lambda self: True})())


def moved_ticket(q, env):
    """A ticket in review with a PASS recorded on the first commit, then two commits landed."""
    t, wt = in_review(q, "task/one", {"one.txt": "1"})
    old = git(wt, "rev-parse", "HEAD").strip()
    tk.record_verdict("task/one", "app", "PASS", old, "Codex")
    commit(wt, {"two.txt": "2"}, "leftovers fix")
    new = commit(wt, {"three.txt": "3"}, "name the limits")
    return t, wt, old, new


def test_ship_explains_the_added_commits_and_stops_when_the_reviews_are_declined(q, env, monkeypatch, capsys):
    t, wt, old, new = moved_ticket(q, env)
    answers(monkeypatch, "n")
    assert ship_main([t["id"]]) == 1
    out, err = capsys.readouterr()
    assert f"has moved past the commit that passed review ({old[:8]})" in out
    assert "2 commit(s) added" in out and "leftovers fix" in out and "name the limits" in out
    assert "nothing was merged" in err
    assert log_lines(env, "review") == [] and log_lines(env, "finish") == []
    assert tk.load_ticket(t["id"])["status"] == "review"


def test_ship_runs_the_reviews_on_the_new_tip_once_the_human_says_yes(q, env, monkeypatch, capsys):
    t, wt, old, new = moved_ticket(q, env)
    answers(monkeypatch, "y")                       # run the reviews on the new tip
    assert ship_main([t["id"]]) == 1                # the fake review writes no review file: the gate says no
    assert log_lines(env, "review") == [str(wt.resolve())]
    assert tk.load_ticket(t["id"])["verdict"]["commit"] == new
    assert log_lines(env, "finish") == []           # reviewed, but never merged without the gate
    assert "not allowed" in capsys.readouterr().err


def test_web_ship_of_a_moved_branch_keeps_refusing_and_says_which_commits(q, env, ntfy, capsys):
    t, wt, old, new = moved_ticket(q, env)
    assert ship_main([t["id"], "--non-interactive", old]) == 1
    err = capsys.readouterr().err
    assert f"is now at {new[:8]}, not the {old[:8]} that was confirmed" in err
    assert "2 commit(s) added" in err and "nothing was merged" in err
    assert log_lines(env, "review") == [] and log_lines(env, "finish") == []


def test_a_rewritten_branch_is_called_what_it_is(q, env, monkeypatch, capsys):
    t, wt, old, new = moved_ticket(q, env)
    git(wt, "reset", "-q", "--hard", "main")
    rewritten = commit(wt, {"other.txt": "x"}, "different work")
    answers(monkeypatch, "n")
    assert ship_main([t["id"]]) == 1
    assert f"no longer contains {old[:8]}" in capsys.readouterr().out and rewritten != old


# --- 3. ID-7 item K: a prep or reconcile agent must leave the main checkout alone ---------------------

def finished_prep(q, herdr_log, env, tamper):
    """One ticket in review, a prep job started over it, the agent `tamper`s, then it finishes."""
    t, wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    tamper()
    set_agent_status(env, "prep", "idle")
    out, warns = finish_prep(settings)
    return t, settings, out, warns


@pytest.mark.parametrize("what", ["a commit on main", "a new file in the main checkout", "another branch checked out"])
def test_a_job_that_changed_the_main_checkout_is_refused_with_one_ping(q, herdr_log, env, ntfy, what):
    def tamper():
        if what == "a commit on main":
            commit(q, {"sneaky.txt": "1"}, "agent commit on main")
        elif what == "a new file in the main checkout":
            (q / "loose.txt").write_text("x")
        else:
            git(q, "checkout", "-q", "-b", "elsewhere")
    t, settings, out, warns = finished_prep(q, herdr_log, env, tamper)
    assert not (env / "fleet" / "ship-list.txt").exists()          # nothing listed
    assert prep.locked_ids() == set()                              # and nothing stays locked
    assert tk.load_ticket(t["id"])["status"] == "review"
    assert any("changed the main checkout" in w for w in warns)
    finish_prep(settings)                                          # a later cycle sends nothing more
    assert [m[0] for m in sent(ntfy)] == ["PREP needs a human, see notes"]
    assert any(n["text"].startswith(f"{prep.NOTE_PREFIX} refused") for n in tk.load_ticket(t["id"])["notes"])


def test_an_untouched_main_checkout_finishes_normally(q, herdr_log, env, ntfy):
    t, settings, out, warns = finished_prep(q, herdr_log, env, lambda: None)
    assert (env / "fleet" / "ship-list.txt").exists()
    assert not any("changed the main checkout" in w for w in warns)
    assert all(m[0] != "PREP needs a human, see notes" for m in sent(ntfy))


def test_a_changed_branch_alone_is_not_a_main_checkout_change(q, herdr_log, env, ntfy):
    def tamper():
        commit(q / ".worktrees" / "one", {"extra.txt": "e"}, "agent fix on the task branch")
    t, settings, out, warns = finished_prep(q, herdr_log, env, tamper)
    assert not any("changed the main checkout" in w for w in warns)
    assert (env / "fleet" / "ship-list.txt").exists()


def test_a_reconcile_agent_is_held_to_the_same_rule(q, herdr_log, env, ntfy):
    t, wt = in_review(q, "task/one", {"one.txt": "1"})
    settings = prep.dp.load_settings()
    prep.start_reconcile(t, settings, lambda m: None)
    commit(q, {"sneaky.txt": "1"}, "agent commit on main")
    set_agent_status(env, "prep-" + t["id"].lower(), "idle")
    finish_prep(settings)
    assert prep.locked_ids() == set() and tk.load_ticket(t["id"])["status"] == "review"
    assert [m[0] for m in sent(ntfy)] == ["PREP-" + t["id"] + " needs a human, see notes"]


def test_a_job_saved_before_snapshots_existed_still_finishes(q, herdr_log, env, ntfy):
    t, wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    with prep._state() as st:
        for job in st["jobs"]:
            job.pop("snapshot")
    commit(q, {"later.txt": "1"}, "main moves")
    set_agent_status(env, "prep", "idle")
    out, warns = finish_prep(settings)
    assert not any("changed the main checkout" in w for w in warns)


def test_an_owed_needs_human_ping_survives_a_later_ready_list_ping(q, herdr_log, env, ntfy):
    t, wt = in_review(q, "task/one", {"one.txt": "1"})
    with prep._state() as st:
        st["pings"].append({"kind": "human", "ids": [], "detail": "the prep agent changed the main checkout",
                            "label": "PREP"})
        prep._defer_ping(st, "list", [t["id"]])        # what the next pass's finish does
        assert [p["kind"] for p in st["pings"]] == ["human", "list"]
    prep.flush_pings(prep.dp.load_settings(), prep.datetime.now(prep.timezone.utc), lambda m: None)
    assert "PREP needs a human, see notes" in [m[0] for m in sent(ntfy)]
