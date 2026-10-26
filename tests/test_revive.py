"""A failed ticket is revived by the watcher when its branch tip has a new exact-commit
verdict that passes the vote and the gate, and is not delivered elsewhere (BD-125).
The vote: 2 distinct companies PASS, never below the risk level's own rule; a FAIL round
counts only when the majority of voting companies failed."""
import pytest

from fleet import risk as rk
from fleet import ticket as tk
from test_dispatch import git, herdr_log, projects  # noqa: F401
from test_oncall import oncall_tickets, stop_after_fail
from test_review_watch import no_dialog, ntfy, rig  # noqa: F401


def fail_it(rig):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    assert rig.get()["status"] == "failed"


def pass_both(rig, commit=None):
    rig.verdict("PASS", "Codex", commit)
    rig.verdict("PASS", "Claude (opus)", commit)


# --- the vote ----------------------------------------------------------------------

def test_vote_needs_two_distinct_companies_and_no_fail():
    ok = {"codex": {"result": "PASS"}, "claude": {"result": "PASS"}}
    assert rk.vote_passes(ok)
    assert not rk.vote_passes({"codex": {"result": "PASS"}})
    assert not rk.vote_passes({**ok, "claude": {"result": "FAIL"}})


def test_cursor_is_a_third_company_vote():
    assert rk.vote_passes({"codex": {"result": "PASS"}, "cursor": {"result": "PASS"}})
    assert not rk.vote_passes({"codex": {"result": "PASS"}, "cursor": {"result": "FAIL"}})
    assert not rk.majority_failed({"codex": {"result": "PASS"}, "cursor": {"result": "FAIL"}})
    assert rk.majority_failed({"codex": {"result": "FAIL"}, "cursor": {"result": "FAIL"}})


def test_a_round_counts_only_when_the_majority_failed(rig):
    rig.verdict("FAIL", "Codex")
    assert rig.get()["fail_commits"] == [rig.tip()]            # the only vote cast failed
    rig.verdict("PASS", "Claude (opus)")                       # split: the round is taken back
    assert rig.get()["fail_commits"] == []
    rig.commit("n")
    rig.verdict("FAIL", "Codex")
    rig.verdict("FAIL", "Claude (opus)")
    assert rig.get()["fail_commits"] == [rig.tip()]


# --- revival -------------------------------------------------------------------------

def test_a_failed_ticket_whose_new_tip_passes_the_vote_is_revived_and_moves_to_review(rig, ntfy):
    fail_it(rig)
    (oc,) = oncall_tickets()
    rig.commit("the fix")
    pass_both(rig)
    rig.cycle(7)
    t = rig.get()
    assert t["status"] in ("claimed", "review") and "close_note" not in t
    assert any("revived" in n["text"] for n in t["notes"])
    assert tk.load_ticket(oc["id"])["status"] in ("cancelled", "claimed")
    rig.cycle(8)
    assert rig.get()["status"] == "review"


def test_the_superseded_open_on_call_ticket_is_cancelled(rig, ntfy):
    fail_it(rig)
    (oc,) = oncall_tickets()
    tk.set_status(oc["id"], "open", allowed_from=("open", "claimed"))
    rig.commit("the fix")
    pass_both(rig)
    rig.cycle(7)
    assert tk.load_ticket(oc["id"])["status"] == "cancelled"
    assert rig.get()["status"] in ("claimed", "review")


def test_one_pass_is_not_a_vote(rig, ntfy):
    fail_it(rig)
    rig.commit("the fix")
    rig.verdict("PASS", "Codex")
    rig.cycle(7)
    assert rig.get()["status"] == "failed"


def test_a_pass_of_an_older_commit_does_not_revive(rig, ntfy):
    fail_it(rig)
    old = rig.commit("the fix")
    pass_both(rig)
    rig.commit("moved on")
    rig.cycle(7)
    assert rig.get()["status"] == "failed" and old != rig.tip()


def test_a_ticket_a_human_closed_is_not_revived(rig, ntfy):
    rig.commit("x")
    pass_both(rig)
    tk.set_status(rig.id, "failed", allowed_from=("claimed",), close_note="human decided")
    rig.cycle(1)
    assert rig.get()["status"] == "failed"


def test_a_tip_already_in_main_is_not_revived(rig, ntfy):
    fail_it(rig)
    rig.commit("the fix")
    pass_both(rig)
    git(rig.repo, "merge", "-q", "--no-ff", "-m", "Merge task/demo", "task/demo")
    rig.cycle(7)
    assert rig.get()["status"] == "failed"


def test_a_running_on_call_ticket_keeps_the_parent_failed(rig, ntfy):
    fail_it(rig)
    (oc,) = oncall_tickets()
    tk.set_status(oc["id"], "claimed", allowed_from=("open", "claimed"))
    rig.commit("the fix")
    pass_both(rig)
    rig.cycle(7)
    assert rig.get()["status"] == "failed"


def test_an_agent_cannot_revive(rig, monkeypatch):
    fail_it(rig)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(tk.TicketError):
        tk.revive_ticket(rig.id)
