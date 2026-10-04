"""True failure and the on-call agent, as `dispatch --watch` does them (fake herdr,
fake ntfy, temp FLEET_HOME, real git).

A mid-loop FAIL never pings. A ticket truly fails when its agent sat idle on the
failed commit for 5 minutes, it has 3 FAIL verdicts on different commits, its
agent was blocked for 20 minutes, or nothing happened for 45 minutes. It is then
closed as failed and one on-call agent is launched at once in its own slot.
"""
import json

import pytest

from conftest import run
from fake_kaneo import FakeKaneo
from fleet import dispatch as dp
from fleet import notify
from fleet import review_watch as rw
from fleet import settings as st
from fleet import ticket as tk
from test_dispatch import calls, claim, git, herdr_log, make_repo, mk, projects  # noqa: F401
from test_kaneo import API_KEY, write_config as write_kaneo_config
from test_notify import write_config as write_ntfy_config
from test_review_watch import Rig, ntfy, rig, sent  # noqa: F401
from test_watch import FakeClock, watch


def world(rig, *agents):
    rig.world.write_text(json.dumps({"workspaces": [], "tabs": [], "agents": list(agents)}))


def oncall_tickets():
    return sorted((t for t in tk.list_tickets() if t.get("oncall")), key=tk.oldest_first_key)


def agent_starts(herdr_log):
    return [c[2] for c in calls(herdr_log) if c[:2] == ["agent", "start"]]


def go(rig, minutes, stop, **settings):
    """Run the real watch loop on a fake clock that starts now; one cycle every `minutes`."""
    return watch({"watch_interval_seconds": minutes * 60, **settings}, FakeClock(stop, start=rig.now))


def stop_after_fail(rig, reviewer="Codex"):
    rig.verdict("FAIL", reviewer)
    rig.set_agent("idle")


# --- mid-loop FAILs never ping ---------------------------------------------------

def test_two_fail_rounds_then_pass_sends_no_failure_ping_and_launches_nothing(rig, ntfy, herdr_log):
    rig.verdict("FAIL", "Codex")

    def second_round(clock):
        rig.commit("fix1")
        rig.verdict("FAIL", "Claude (opus)")

    def pass_round(clock):
        rig.commit("fix2")
        rig.verdict("PASS", "Codex")
        rig.summary()
        rig.set_agent("idle")
    assert watch({"watch_interval_seconds": 120}, FakeClock(6, start=rig.now, at={1: second_round, 3: pass_round})) == 0
    assert rig.get()["status"] == "review"
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} ready to ship"]   # the one ship ping, no failure ping
    assert oncall_tickets() == [] and agent_starts(herdr_log) == []
    assert len(rig.get()["fail_commits"]) == 2


def test_an_idle_agent_on_a_failed_tip_is_given_five_minutes(rig, ntfy):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(4)
    assert rig.get()["status"] == "claimed" and ntfy.requests == []
    rig.cycle(5)
    t = rig.get()
    assert t["status"] == "failed" and "stopped" in t["close_note"]
    assert "Codex" in t["close_note"] and "FAIL rounds: 1" in t["close_note"]


@pytest.mark.parametrize("state", ["idle", "done"])
def test_idle_and_done_both_count_as_stopped(rig, ntfy, state):
    rig.verdict("FAIL")
    rig.set_agent(state)
    rig.cycle(0)
    rig.cycle(6)
    assert rig.get()["status"] == "failed"


def test_the_five_minutes_must_be_continuous(rig, ntfy):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(4)
    rig.set_agent("working")
    rig.cycle(5)           # busy again: the spell ends
    rig.set_agent("idle")
    rig.cycle(6)           # a new spell starts
    rig.cycle(10)
    assert rig.get()["status"] == "claimed" and ntfy.requests == []
    rig.cycle(12)
    assert rig.get()["status"] == "failed"


def test_a_tip_that_moved_past_the_failed_commit_is_not_a_stop_on_it(rig, ntfy):
    rig.verdict("FAIL")
    rig.commit("fix")
    rig.set_agent("idle")
    rig.cycle(0)
    rig.cycle(10)
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_a_working_agent_after_a_fail_is_never_failed_for_stopping(rig, ntfy):
    rig.verdict("FAIL")
    rig.set_agent("working", revision=1)
    for minutes in (0, 6, 12, 30):
        rig.set_agent("working", revision=minutes)   # herdr keeps reporting changes: not stalled
        rig.cycle(minutes)
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_three_fails_on_different_commits_is_going_in_circles(rig, ntfy):
    rig.verdict("FAIL")
    rig.commit("a")
    rig.verdict("FAIL")
    rig.cycle(0)           # two rounds, agent still working: nothing happens
    assert rig.get()["status"] == "claimed" and ntfy.requests == []
    rig.commit("b")
    rig.verdict("FAIL")
    rig.cycle(1)
    t = rig.get()
    assert t["status"] == "failed" and "going in circles" in t["close_note"]
    assert "FAIL rounds: 3" in t["close_note"] and len(t["fail_commits"]) == 3
    assert [x[0] for x in sent(ntfy)] == [f"{rig.id} failed, on-call OC-1 is on it"]


def test_two_reviewers_failing_one_commit_is_one_round(rig, ntfy):
    for _ in range(2):
        rig.verdict("FAIL", "Codex")
        rig.verdict("FAIL", "Claude (opus)")
        rig.commit(f"n{_}")
    rig.verdict("FAIL")
    rig.cycle()
    assert len(rig.get()["fail_commits"]) == 3 and rig.get()["status"] == "failed"


def test_a_pass_after_two_fails_is_not_going_in_circles(rig, ntfy):
    rig.verdict("FAIL")
    rig.commit("a")
    rig.verdict("FAIL")
    rig.commit("b")
    rig.verdict("PASS")
    rig.summary()
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "review"


# --- a true failure: closed as failed, one on-call agent, one low ping -------------

def test_a_stopped_agent_closes_the_ticket_and_launches_one_on_call_at_once(rig, ntfy, herdr_log):
    other = claim(mk(branch="task/other"))            # a normal ticket is running...
    waiting = mk(branch="task/waiting")               # ...and another waits for the slot
    rig.edit(body="THE FULL SPEC TEXT")
    rig.summary("a note from the agent")
    tip = rig.tip()
    (rig.wt / ".reviews").mkdir()
    (rig.wt / ".reviews" / "codex.md").write_text(
        f"Reviewer: Codex\nCommit: {tip}\n\nVERDICT: FAIL\nMUST FIX:\n1. app.py:3 boom is wrong\n"
        "SHOULD FIX:\n1. tidy\n")
    rig.verdict("FAIL", "Codex")
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"},
          {"name": other["id"].lower(), "agent_status": "working"})
    assert go(rig, 3, 4, max_launches_per_day=0) == 0       # the normal daily cap does not apply

    failed = rig.get()
    assert failed["status"] == "failed"
    assert "stopped" in failed["close_note"] and "Codex" in failed["close_note"] and "FAIL rounds: 1" in failed["close_note"]
    (oc,) = oncall_tickets()
    assert oc["id"] == "OC-1" and oc["type"] == "fix" and oc["parent"] == rig.id and oc["oncall"] is True
    assert oc["status"] == "claimed" and oc["assignee"] == "claude"
    assert "OC-1" in failed["notes"][-1]["text"]                  # the failed ticket points at it
    assert "THE FULL SPEC TEXT" in oc["body"] and "a note from the agent" in oc["body"]
    assert "boom is wrong" in oc["body"] and "tidy" not in oc["body"]   # MUST FIX only
    assert "stopped" in oc["body"] and tip in oc["body"]
    assert agent_starts(herdr_log) == ["oc-1"]
    # its own folder and branch, started from the failed tip
    assert (rig.repo / ".worktrees" / f"oc-{rig.id.lower()}").is_dir()
    assert git(rig.repo, "rev-parse", oc["branch"]).strip() == tip
    # the normal slot is untouched: the other ticket still runs and the waiting one still waits
    assert tk.load_ticket(other["id"])["status"] == "claimed"
    assert tk.load_ticket(waiting["id"])["status"] == "open"   # at capacity: it did not take the on-call slot
    assert [(t[0], t[2]) for t in sent(ntfy)] == [(f"{rig.id} failed, on-call OC-1 is on it", "low")]


@pytest.mark.parametrize("kind, minutes, stop, agent", [
    ("stall", 15, 5, {"agent_status": "working", "revision": 1}),
    ("block", 10, 4, {"agent_status": "blocked"})])
def test_stall_and_block_launch_the_on_call_agent_too(rig, ntfy, herdr_log, kind, minutes, stop, agent):
    other = claim(mk(branch="task/other"))
    world(rig, {"name": rig.id.lower(), **agent}, {"name": other["id"].lower(), "agent_status": "working"})
    assert go(rig, minutes, stop) == 0
    assert rig.get()["status"] == "failed"
    assert {"stall": "stalled", "block": "blocked"}[kind] in rig.get()["close_note"]
    (oc,) = oncall_tickets()
    assert oc["status"] == "claimed" and agent_starts(herdr_log) == [oc["id"].lower()]
    assert [(t[0], t[2]) for t in sent(ntfy)] == [(f"{rig.id} failed, on-call {oc['id']} is on it", "low")]


def test_a_permission_prompt_is_a_failure_not_something_to_answer(rig, ntfy, herdr_log):
    world(rig, {"name": rig.id.lower(), "agent_status": "blocked"})
    go(rig, 10, 4)
    assert not [c for c in calls(herdr_log) if c[:2] in (["agent", "prompt"], ["agent", "send"])
                and c[2] == rig.id.lower()]
    assert rig.get()["status"] == "failed"


def test_no_event_pings_twice_for_the_same_ticket(rig, ntfy, herdr_log):
    stop_after_fail(rig)
    go(rig, 3, 12)
    assert len(ntfy.requests) == 1 and len(oncall_tickets()) == 1
    assert agent_starts(herdr_log) == ["oc-1"]
    state = json.loads((tk.fleet_home() / "review-watch.json").read_text())
    assert state["pending"] == []   # delivered, nothing left to retry


def test_the_card_moves_to_failed_and_the_on_call_card_exists(rig, ntfy):
    kaneo = FakeKaneo(API_KEY).start()
    try:
        write_kaneo_config(kaneo.url)
        stop_after_fail(rig)
        rig.cycle(0)
        rig.cycle(6)
        statuses = sorted(c["status"] for c in kaneo.tasks.values())
        assert "failed" in statuses and len(statuses) == 2
    finally:
        kaneo.stop()


# --- the loop guard: one on-call attempt, three a day ------------------------------

def test_a_failed_on_call_ticket_launches_nothing_more_and_sends_one_needs_a_human_ping(rig, ntfy, herdr_log):
    stop_after_fail(rig)
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 4)
    (oc,) = oncall_tickets()
    assert oc["status"] == "claimed" and len(ntfy.requests) == 1
    # the on-call agent in turn gets a FAIL and stops
    tk.record_verdict(oc["branch"], "app", "FAIL", git(rig.repo, "rev-parse", oc["branch"]).strip(), "Codex")
    world(rig, {"name": oc["id"].lower(), "agent_status": "idle"})
    for minutes in (20, 21, 26, 30, 40):
        rig.cycle(minutes)
    oc_after = tk.load_ticket(oc["id"])
    assert oc_after["status"] == "failed" and "Diagnosis" in oc_after["notes"][-1]["text"]
    assert tk.load_ticket(rig.id)["notes"][-1]["text"].startswith(f"Diagnosis: on-call {oc['id']} could not fix {rig.id}.")
    assert [(t[0], t[2]) for t in sent(ntfy)] == [
        (f"{rig.id} failed, on-call OC-1 is on it", "low"), (f"{rig.id} needs a human, see notes", "low")]
    assert len(oncall_tickets()) == 1 and agent_starts(herdr_log) == ["oc-1"]
    assert dp.decide_oncall(tk.list_tickets(), rig.settings) == []


def test_a_fourth_on_call_need_in_one_day_launches_nothing(rig, ntfy, herdr_log):
    parent = mk(branch="task/old-parent")
    tk.set_status(parent["id"], "cancelled", allowed_from=("open",))
    for n in range(3):
        old = tk.create_ticket({"type": "fix", "repo": "app", "branch": f"task/oc-old{n}", "title": "old",
                                "body": "b", "parent": parent["id"]}, oncall=True)
        tk.set_status(old["id"], "failed", allowed_from=("open",))
    assert len(oncall_tickets()) == 3
    world(rig, {"name": rig.id.lower(), "agent_status": "blocked"})
    go(rig, 10, 4)
    assert rig.get()["status"] == "failed"
    assert len(oncall_tickets()) == 3 and agent_starts(herdr_log) == []
    assert "limit" in rig.get()["notes"][-1]["text"]
    assert [(t[0], t[2]) for t in sent(ntfy)] == [(f"{rig.id} needs a human, see notes", "low")]


def test_the_on_call_cap_is_a_setting(rig, ntfy, herdr_log):
    (tk.fleet_home() / "settings.json").write_text(json.dumps({"max_oncall_per_day": 0}))
    rig.settings = dp.load_settings()
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    assert oncall_tickets() == []
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} needs a human, see notes"]


def test_a_second_attempt_for_the_same_ticket_is_refused(rig, ntfy):
    old = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-first", "title": "old",
                            "body": "b", "parent": rig.id}, oncall=True)
    tk.set_status(old["id"], "failed", allowed_from=("open",))
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    assert len(oncall_tickets()) == 1
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} needs a human, see notes"]


# --- the on-call slot ------------------------------------------------------------------

def test_the_on_call_slot_is_separate_one_at_a_time_and_first_in_first_out(rig):
    tk.set_status(rig.id, "failed", allowed_from=("claimed",))   # frees the normal slot
    first = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-a", "title": "a", "body": "b",
                              "parent": rig.id}, oncall=True)
    second = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-b", "title": "b", "body": "b",
                               "parent": rig.id}, oncall=True)
    normal = mk(branch="task/normal")
    decisions = dp.decide_oncall(tk.list_tickets(), rig.settings)
    assert [(d.ticket["id"], d.eligible) for d in decisions] == [(first["id"], True), (second["id"], False)]
    assert decisions[0].base == rig.tip() and "waiting its turn" in decisions[1].skip
    tk.set_status(first["id"], "claimed", allowed_from=("open",), assignee="claude")
    decisions = dp.decide_oncall(tk.list_tickets(), rig.settings)
    assert [(d.ticket["id"], d.eligible) for d in decisions] == [(second["id"], False)]
    assert "busy" in decisions[0].skip
    # on-call tickets neither fill nor join the normal queue
    normal_decisions = dp.decide(tk.list_tickets(), rig.settings)
    assert [(d.ticket["id"], d.eligible) for d in normal_decisions] == [(normal["id"], True)]
    # the agent has stopped (ticket in review): the slot is free for the next one
    tk.set_status(first["id"], "review", allowed_from=("claimed",))
    assert dp.decide_oncall(tk.list_tickets(), rig.settings)[0].eligible


def test_dry_run_lists_the_on_call_ticket(rig, capsys):
    tk.set_status(rig.id, "failed", allowed_from=("claimed",))
    tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-a", "title": "a", "body": "b",
                      "parent": rig.id}, oncall=True)
    from fleet.dispatch_cli import main
    assert main(["--dry-run"]) == 0
    assert "would launch on-call claude" in capsys.readouterr().out


def test_an_on_call_ticket_follows_the_normal_ready_to_ship_flow(rig, ntfy, herdr_log):
    stop_after_fail(rig)
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 4)
    (oc,) = oncall_tickets()
    wt = rig.repo / ".worktrees" / oc["branch"].removeprefix("task/")
    (wt / "fix.txt").write_text("fixed")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "fix")
    tip = git(wt, "rev-parse", "HEAD").strip()
    tk.record_verdict(oc["branch"], "app", "PASS", tip, "Codex")
    rig.monkeypatch.setenv("CLAUDECODE", "1")
    tk.add_note(oc["id"], "SUMMARY: fixed")
    rig.monkeypatch.delenv("CLAUDECODE")
    world(rig, {"name": oc["id"].lower(), "agent_status": "idle"})
    rig.cycle(20)
    assert tk.load_ticket(oc["id"])["status"] == "review"
    assert [t[0] for t in sent(ntfy)][-1] == f"{oc['id']} ready to ship"


# --- plumbing -----------------------------------------------------------------------------

def test_only_the_dispatcher_can_file_an_on_call_ticket(rig, monkeypatch):
    fields = {"type": "fix", "repo": "app", "branch": "task/oc-x", "title": "x", "body": "b", "parent": rig.id}
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(tk.TicketError, match="only the dispatcher"):
        tk.create_ticket(fields, oncall=True)
    monkeypatch.delenv("CLAUDECODE")
    with pytest.raises(tk.TicketError, match="only the dispatcher"):
        tk.create_ticket({**fields, "type": "build"}, oncall=True)


def test_oncall_prefix_and_cap_settings(env):
    s = dp.load_settings()
    assert s["oncall_prefix"] == "OC" and s["max_oncall_per_day"] == 3
    home = tk.fleet_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"oncall_prefix": "ZZ", "max_oncall_per_day": 1}))
    s = dp.load_settings()
    assert s["oncall_prefix"] == "ZZ" and s["max_oncall_per_day"] == 1
    for bad in ({"oncall_prefix": "oc"}, {"oncall_prefix": "EM"}, {"max_oncall_per_day": -1},
                {"max_oncall_per_day": True}):
        (home / "settings.json").write_text(json.dumps(bad))
        with pytest.raises(st.SettingsError):
            dp.load_settings()


def test_a_stored_ticket_with_bad_failure_fields_is_refused(rig):
    rig.edit(fail_commits=["nope"])
    with pytest.raises(tk.TicketError, match="fail_commits"):
        rig.get()
    rig.edit(fail_commits=[], oncall=False)
    with pytest.raises(tk.TicketError, match="oncall"):
        rig.get()


def test_the_notification_titles(rig):
    t = rig.get()
    assert notify._message(t, "oncall", {"topic": "x", "priorities": notify.PRIORITIES}, "OC-9")["title"] == \
        f"{t['id']} failed, on-call OC-9 is on it"
    assert notify._message(t, "needs_human", {"topic": "x", "priorities": notify.PRIORITIES})["title"] == \
        f"{t['id']} needs a human, see notes"


def test_a_ticket_closed_as_failed_by_a_human_still_pings_at_once(rig, ntfy):
    assert run("close", rig.id, "--failed", "--note", "bad").returncode == 0
    assert [(t[0], t[2]) for t in sent(ntfy)] == [(f"Ticket failed: {rig.id}", "high")]


# --- launch trouble, slot ownership, hostile review files -----------------------------------

def failed_ticket_with_oncall(rig, herdr_log):
    """Fail the rig's ticket by stopping; returns the on-call ticket (launched or not)."""
    stop_after_fail(rig)
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 4)
    (oc,) = oncall_tickets()
    return oc


def test_a_launch_that_fails_after_the_folder_exists_is_cleaned_up_and_retried(rig, ntfy, herdr_log, monkeypatch):
    from fleet import launch as lc
    real = lc.start_agent
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise lc.LaunchError("herdr hiccup")
        return real(*args, **kwargs)
    monkeypatch.setattr(lc, "start_agent", flaky)
    stop_after_fail(rig)
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 5)
    (oc,) = oncall_tickets()
    assert len(attempts) == 2 and oc["status"] == "claimed"      # the retry worked: no stuck branch
    assert git(rig.repo, "rev-parse", oc["branch"]).strip() == rig.tip()
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} failed, on-call OC-1 is on it"]


def test_an_on_call_launch_that_leaves_its_branch_behind_ends_with_a_needs_a_human_ping(rig, ntfy, herdr_log, monkeypatch):
    from fleet import launch as lc

    def broken(*args, **kwargs):
        raise lc.LaunchError("herdr is down")
    monkeypatch.setattr(lc, "start_agent", broken)
    monkeypatch.setattr(lc, "discard_oncall_worktree", lambda *a, **k: False)   # it may hold work: kept
    stop_after_fail(rig)
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 6)
    (oc,) = oncall_tickets()
    assert oc["status"] == "failed" and "left task/oc-" in oc["close_note"]
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} failed, on-call OC-1 is on it",
                                          f"{rig.id} needs a human, see notes"]


def test_discard_only_removes_an_untouched_folder_and_branch(rig, herdr_log):
    from fleet import launch as lc
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    d = dp.decide_oncall(tk.list_tickets(), rig.settings)[0]
    folder = rig.repo / ".worktrees" / d.name
    git(rig.repo, "worktree", "add", str(folder), "-b", oc["branch"], d.base)
    (folder / "new.txt").write_text("work")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "work")
    assert lc.discard_oncall_worktree(d, folder, d.base) is False   # has a commit of its own: kept
    assert folder.is_dir()
    git(folder, "reset", "-q", "--hard", d.base)
    assert lc.discard_oncall_worktree(d, folder, d.base) is True
    assert not folder.exists() and not dp.branch_exists(rig.repo, oc["branch"])
    assert rig.tip() == d.base                                       # the failed branch is untouched


def test_the_on_call_slot_stays_taken_while_a_failed_on_call_agent_still_runs(rig, ntfy, herdr_log):
    oc = failed_ticket_with_oncall(rig, herdr_log)
    second = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-next", "title": "n", "body": "b",
                               "parent": rig.id}, oncall=True)
    tk.set_status(oc["id"], "failed", allowed_from=("claimed",))
    running = {oc["id"].lower()}
    d = dp.decide_oncall(tk.list_tickets(), rig.settings, running)
    assert [(x.ticket["id"], x.eligible) for x in d] == [(second["id"], False)] and "busy" in d[0].skip
    assert dp.decide_oncall(tk.list_tickets(), rig.settings, set())[0].eligible   # it has stopped: free


def test_a_review_file_that_is_a_symlink_is_not_copied_into_the_ticket(rig, ntfy, env):
    secret = env / "elsewhere.md"
    secret.write_text(f"Reviewer: Codex\nCommit: {rig.tip()}\n\nVERDICT: FAIL\nMUST FIX:\n1. LEAKED TEXT\n")
    (rig.wt / ".reviews").mkdir()
    (rig.wt / ".reviews" / "codex.md").symlink_to(secret)
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    assert "LEAKED TEXT" not in oc["body"]


def test_a_review_folder_that_is_a_symlink_is_not_copied_into_the_ticket(rig, ntfy, env):
    outside = env / "outside-reviews"
    outside.mkdir()
    (outside / "codex.md").write_text(
        f"Reviewer: Codex\nCommit: {rig.tip()}\n\nVERDICT: FAIL\nMUST FIX:\n1. LEAKED TEXT\n")
    (rig.wt / ".reviews").symlink_to(outside)
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    assert "LEAKED TEXT" not in oc["body"]


def test_on_call_launch_waits_when_herdr_cannot_say_whether_the_slot_is_free(rig, ntfy, herdr_log, monkeypatch, capsys):
    oc = failed_ticket_with_oncall(rig, herdr_log)
    tk.set_status(oc["id"], "failed", allowed_from=("claimed",))     # its agent is still running, though
    second = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-next", "title": "n", "body": "b",
                               "parent": rig.id}, oncall=True)
    world(rig, {"name": oc["id"].lower(), "agent_status": "working"})
    monkeypatch.setenv("FLEET_HERDR", str(rig.env / "no-such-herdr"))
    go(rig, 3, 2)
    assert tk.load_ticket(second["id"])["status"] == "open"
    assert "on-call launch deferred" in capsys.readouterr().out


def test_an_open_on_call_ticket_with_a_leftover_task_folder_is_escalated(rig, ntfy):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    (rig.repo / ".worktrees" / oc["branch"].removeprefix("task/")).mkdir()   # a folder, no branch
    rig.cycle(7)
    assert tk.load_ticket(oc["id"])["status"] == "failed"
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} failed, on-call OC-1 is on it",
                                          f"{rig.id} needs a human, see notes"]


def test_the_on_call_branch_starts_from_the_commit_that_failed_even_if_the_old_branch_moves_on(rig, ntfy, herdr_log):
    failed_at = rig.tip()
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)                       # failed; the on-call ticket waits, not yet launched
    (oc,) = oncall_tickets()
    assert oc["oncall_base"] == failed_at and oc["status"] == "open"
    rig.commit("the old agent kept going")
    assert rig.tip() != failed_at
    assert dp.decide_oncall(tk.list_tickets(), rig.settings)[0].base == failed_at
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 2)
    assert git(rig.repo, "rev-parse", oc["branch"]).strip() == failed_at


def test_a_blocker_that_keeps_an_on_call_ticket_from_launching_is_reported(rig, ntfy, capsys, monkeypatch):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    path = tk.tickets_dir() / f"{oc['id']}.json"
    data = json.loads(path.read_text())
    data["oncall_base"] = "0" * 40                       # a commit that does not exist
    path.write_text(json.dumps(data))
    world(rig, {"name": rig.id.lower(), "agent_status": "idle"})
    go(rig, 3, 2)
    assert "on-call not launched: commit to start from not found" in capsys.readouterr().out
    assert tk.load_ticket(oc["id"])["status"] == "open"


def test_oncall_base_needs_an_on_call_ticket_and_a_full_commit(rig):
    with pytest.raises(tk.TicketError, match="oncall_base"):
        tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/x", "title": "x", "body": "b",
                          "parent": rig.id}, oncall_base="a" * 40)
    rig.edit(oncall_base="a" * 40)                       # not an on-call ticket
    with pytest.raises(tk.TicketError, match="oncall_base"):
        rig.get()


@pytest.mark.parametrize("status", ["failed", "cancelled", "review", "done"])
def test_the_on_call_slot_stays_taken_while_any_on_call_agent_runs_whatever_its_status(rig, status):
    first = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-a", "title": "a", "body": "b",
                              "parent": rig.id}, oncall=True)
    second = tk.create_ticket({"type": "fix", "repo": "app", "branch": "task/oc-b", "title": "b", "body": "b",
                               "parent": rig.id}, oncall=True)
    tk.set_status(first["id"], "claimed", allowed_from=("open",), assignee="claude")
    tk.set_status(first["id"], status, allowed_from=("claimed",))
    running = {first["id"].lower()}
    d = dp.decide_oncall(tk.list_tickets(), rig.settings, running)
    assert [(x.ticket["id"], x.eligible) for x in d] == [(second["id"], False)]
    assert dp.decide_oncall(tk.list_tickets(), rig.settings, set())[0].eligible


def test_a_log_write_that_fails_does_not_stop_recovery(rig, ntfy, monkeypatch):
    def broken(kind, message):
        raise OSError("disk full")
    monkeypatch.setattr(dp, "log", broken)
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    assert rig.get()["status"] == "failed" and len(oncall_tickets()) == 1
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} failed, on-call OC-1 is on it"]


def test_a_stuck_on_call_escalation_survives_a_failing_log_write(rig, ntfy, monkeypatch):
    stop_after_fail(rig)
    rig.cycle(0)
    rig.cycle(6)
    (oc,) = oncall_tickets()
    (rig.repo / ".worktrees" / oc["branch"].removeprefix("task/")).mkdir()
    monkeypatch.setattr(dp, "log", lambda kind, message: (_ for _ in ()).throw(OSError("disk full")))
    rig.cycle(7)
    assert tk.load_ticket(oc["id"])["status"] == "failed"
    assert [t[0] for t in sent(ntfy)][-1] == f"{rig.id} needs a human, see notes"


def test_failure_ping_keys_of_closed_tickets_are_capped(rig, ntfy, monkeypatch):
    monkeypatch.setattr(rw, "FAILURE_KEYS_KEPT", 2)
    rw.state_path().write_text(json.dumps({
        "sent": [f"X-{n}:oncall" for n in range(5)], "blocked_since": {}, "seen": {}}))
    rig.cycle()
    assert json.loads(rw.state_path().read_text())["sent"] == ["X-3:oncall", "X-4:oncall"]


def test_a_pending_ping_with_a_bad_detail_is_dropped_on_load(rig):
    rw.state_path().write_text(json.dumps({
        "sent": [], "blocked_since": {}, "seen": {},
        "pending": [{"id": "BD-1", "event": "oncall", "detail": ["x"]},
                    {"id": "BD-1", "event": "oncall", "detail": "OC-1"}]}))
    state = rw._load_state(lambda m: None)
    assert state["pending"] == [{"id": "BD-1", "event": "oncall", "detail": "OC-1"}]
