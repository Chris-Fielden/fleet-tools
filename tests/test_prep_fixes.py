"""EM-9 review fixes: branch ownership, tips recorded early, keeper validated first, odd reports."""

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import prep
from fleet import ticket as tk
from fleet import ship_cli
from fleet.ship_cli import main as ship_main
from test_dispatch import calls, git, herdr_log, projects, mk, claim  # noqa: F401
from test_prep import (NIGHT as NOW, ntfy, agent_names, commit, conflicting, finish_prep, in_review, log_lines, merge, q,  # noqa: F401
                       report, set_agent_status, start_prep)


def test_ship_never_aborts_a_merge_the_human_started(q, herdr_log, env, capsys):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0
    assert merge(q, "task/beta").returncode != 0               # the human's own half-done merge
    assert ship_main([b["id"]]) == 1
    assert "merge is already in progress" in capsys.readouterr().err
    assert git(q, "rev-parse", "-q", "--verify", "MERGE_HEAD").strip()   # untouched
    assert agent_names(herdr_log) == [] and log_lines(env, "finish") == ["alpha"]


def test_the_prep_agent_leaves_out_tickets_a_reconcile_agent_holds(q, herdr_log, env):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0
    assert ship_main([b["id"]]) == 1                           # a reconcile agent holds beta
    c, _ = in_review(q, "task/gamma", {"g.txt": "g"})
    settings, _, _ = start_prep(force=True)
    assert prep.locked_ids() == {b["id"], c["id"]}
    manifest = json.loads((lc.research_folder(prep.FOLDER_ID, settings) / "manifest-prep.json").read_text())
    assert [t["id"] for t in manifest["tickets"]] == [c["id"]]


def test_a_reconcile_agent_needs_the_same_builder_as_the_prep_agent(q, herdr_log, env, capsys):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0
    (env / "fleet" / "settings.json").write_text(json.dumps({"builder": "codex"}))
    assert ship_main([b["id"]]) == 1
    assert "no reconcile agent could be started" in capsys.readouterr().err and agent_names(herdr_log) == []
    assert prep.locked_ids() == set()


def test_a_long_running_agent_keeps_its_locks(q, herdr_log, env):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    with prep._state() as st:
        st["jobs"][0]["started"] = "2026-10-03T10:00:00Z"
    _, warns = finish_prep(settings, now=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))   # 8 hours later
    assert prep.locked_ids() == {a["id"]} and any("stay locked" in w for w in warns)
    assert not (env / "fleet" / "ship-list.txt").exists()


def test_the_starting_tips_are_recorded_before_the_agent_exists(q, herdr_log, env, monkeypatch):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    tip = prep.tip_of(q, "task/one")
    seen = {}
    real = lc.start_agent

    def spy(*args, **kw):
        seen["job"] = prep._load()["jobs"][0]               # saved already, not ready yet
        commit(a_wt, {"fast.txt": "an agent this fast"})    # the agent writes the moment it starts
        return real(*args, **kw)
    monkeypatch.setattr(lc, "start_agent", spy)
    settings, _, _ = start_prep(force=True)
    assert seen["job"]["tips"] == {a["id"]: tip} and seen["job"]["ready"] is False
    report(settings, order=[a["id"]])
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert log_lines(env, "check") == [str(a_wt)]            # the moved tip was re-checked


def test_a_keeper_that_fails_its_recheck_does_not_cancel_the_duplicate(q, herdr_log, env, monkeypatch):
    full, full_wt = in_review(q, "task/full", {"f.txt": "1"}, body="the full spec " * 20)
    thin, _ = in_review(q, "task/thin", {"f.txt": "1"}, body="short")
    settings, _, _ = start_prep()
    commit(full_wt, {"extra.txt": "folded"})
    monkeypatch.setenv("FAKE_REVIEW_FAIL", "task/full")
    report(settings, order=[full["id"], thin["id"]], duplicates=[{"keep": full["id"], "cancel": [thin["id"]]}])
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert tk.load_ticket(full["id"])["status"] == "claimed"
    assert tk.load_ticket(thin["id"])["status"] == "review"      # still active
    assert [i for i, _ in prep.read_list()] == [thin["id"]]


@pytest.mark.parametrize("bad", [[], {}, 5, None, [["x"]]])
def test_odd_report_fields_are_ignored_not_fatal(q, herdr_log, env, bad):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    report(settings, order=bad, tickets=bad, duplicates=[{"keep": bad, "cancel": bad}, bad])
    set_agent_status(env, "prep", "idle")
    _, warns = finish_prep(settings)
    assert not warns and [i for i, _ in prep.read_list()] == [a["id"]]


def test_a_half_launched_job_with_a_busy_agent_keeps_its_tickets(q, herdr_log, env):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    with prep._state() as st:
        st["jobs"][0].update(ready=False, started="2026-10-03T10:00:00Z")
    finish_prep(settings, now=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))
    assert prep.locked_ids() == {a["id"]}              # herdr lists it as working: still locked
    set_agent_status(env, "prep", "idle")
    finish_prep(settings, now=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))
    assert prep.locked_ids() == set()


def test_a_reconciled_ticket_shipped_alone_before_gets_a_list_line(q, herdr_log, env):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0 and ship_main([b["id"]]) == 1
    assert merge(b_wt, "main").returncode != 0
    (b_wt / "code.py").write_text("def shared():\n    return 0\n\ndef alpha():\n    return 'a'\n\ndef beta():\n    return 'b'\n")
    git(b_wt, "add", "-A")
    git(b_wt, "commit", "-q", "-m", "merge main")
    set_agent_status(env, f"prep-{b['id'].lower()}", "idle")
    finish_prep(dp.load_settings())
    assert [i for i, _ in prep.read_list()] == [b["id"]]


def test_uncommitted_edits_left_by_the_agent_keep_a_ticket_off_the_list(q, herdr_log, env):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    (a_wt / "one.txt").write_text("edited but not committed")
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert tk.load_ticket(a["id"])["status"] == "claimed" and prep.read_list() == []


def test_a_ticket_being_shipped_is_left_out_of_the_prep_agent(q, herdr_log, env, monkeypatch):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "2"})
    assert prep.reserve_shipping(a["id"]) is None
    assert prep.locked_ids() == {a["id"]}
    settings, _, _ = start_prep(force=True)
    manifest = json.loads((lc.research_folder(prep.FOLDER_ID, settings) / "manifest-prep.json").read_text())
    assert [t["id"] for t in manifest["tickets"]] == [b["id"]]


def test_ship_reserves_its_ticket_while_it_runs_and_releases_it_after(q, herdr_log, env, monkeypatch):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    seen = []
    real = ship_cli._run_script

    def spy(cmd, *args, **kw):
        if any("finish-task" in str(c) for c in cmd):
            seen.append(prep.locked_ids())
        return real(cmd, *args, **kw)

    monkeypatch.setattr(ship_cli, "_run_script", spy)
    assert ship_main([a["id"]]) == 0
    assert seen == [{a["id"]}] and prep.locked_ids() == set()


def test_ship_refuses_a_ticket_another_ship_holds_and_ignores_a_dead_one(q, env, capsys):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    with prep._state() as st:
        st["shipping"].append({"id": a["id"], "pid": 1})     # init: alive, not us
    assert ship_main([a["id"]]) == 1 and "already being shipped" in capsys.readouterr().err
    with prep._state() as st:
        st["shipping"] = [{"id": a["id"], "pid": 2 ** 22 + 12345}]   # no such process
    assert prep.locked_ids() == set()
    assert ship_main([a["id"]]) == 0


def test_an_unreadable_prep_state_fails_closed(q, env, capsys):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    (env / "fleet" / "prep.json").write_text("{not json")
    with pytest.raises(OSError):
        prep.locked_ids()
    assert ship_main([a["id"]]) == 1
    assert "cannot tell whether an agent is working" in capsys.readouterr().err
    assert tk.load_ticket(a["id"])["status"] == "review"


def test_a_half_launched_job_that_stopped_is_still_validated(q, herdr_log, env):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    with prep._state() as st:
        st["jobs"][0].update(ready=False, started="2026-10-03T10:00:00Z")
    (a_wt / "stray.txt").write_text("left behind by the agent")   # uncommitted work
    set_agent_status(env, "prep", "idle")
    _, warns = finish_prep(settings, now=datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc))
    assert tk.load_ticket(a["id"])["status"] == "claimed" and prep.locked_ids() == set()
    assert not [i for i, _ in prep.read_list()]


def test_an_unchanged_branch_whose_task_folder_is_gone_is_left_off_the_list(q, herdr_log, env):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "2"})
    settings, _, _ = start_prep()
    git(q, "worktree", "remove", "--force", str(a_wt))
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert tk.load_ticket(a["id"])["status"] == "claimed"
    assert [i for i, _ in prep.read_list()] == [b["id"]]


def test_two_ships_into_one_repo_never_run_together(q, herdr_log, env, capsys):
    from fleet import ship_cli
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    with ship_cli._repo_lock(a["repo"]) as busy:
        assert busy is False
        assert ship_main([a["id"]]) == 1                  # another ship holds the repo
        assert "already merging into" in capsys.readouterr().err
        assert log_lines(env, "finish") == []             # the merge script never ran
    assert prep.locked_ids() == set()                     # the refused run released its reservation
    assert ship_main([a["id"]]) == 0


def test_giving_up_on_a_job_is_a_loud_warning(q, herdr_log, env, monkeypatch):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    set_agent_status(env, "prep", "idle")
    monkeypatch.setattr(prep, "_finish_prep", lambda *x: (_ for _ in ()).throw(OSError("disk full")))
    warns = []
    for _ in range(prep.MAX_TRIES):
        warns += finish_prep(settings)[1]
    assert prep.locked_ids() == set()
    assert any("given up" in w and a["id"] in w for w in warns)


def test_the_recheck_runs_the_branch_scripts_with_an_agent_identity(q, herdr_log, env, monkeypatch):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    commit(a_wt, {"more.txt": "changed by the prep agent"})
    seen = []
    real = prep._run

    def spy(cmd, cwd, timeout, env=None):
        seen.append(dict(env or {}))
        return real(cmd, cwd, timeout, env)

    monkeypatch.setattr(prep, "_run", spy)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX", "1")
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert len(seen) == 2 and all(e.get("CLAUDECODE") == "1" and "CODEX_SANDBOX" not in e for e in seen)


# --- EM-17: re-checks never block the cycle; a sent-back ticket gets an agent ---------------

def test_a_slow_recheck_does_not_block_the_cycle(q, herdr_log, env, monkeypatch):
    import threading
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    commit(a_wt, {"more.txt": "changed by the prep agent"})
    gate, started = threading.Event(), threading.Event()
    real = prep.validate

    def slow(t, s):
        started.set()
        gate.wait(10)
        return real(t, s)

    monkeypatch.setattr(prep, "validate", slow)
    set_agent_status(env, "prep", "idle")
    out, warns = [], []
    prep.finish(settings, NOW, out.append, warns.append)   # returns at once, with the check running
    assert started.wait(5)
    assert [j["name"] for j in prep._load()["jobs"]] == ["prep"] and a["id"] in prep.locked_ids()
    assert prep.list_path().exists() is False and not any("not finished" in w for w in warns)
    prep.finish(settings, NOW, out.append, warns.append)   # still running: still pending, nothing doubled
    assert prep._load()["jobs"][0].get("tries", 0) == 0
    gate.set()
    prep.wait_checks()
    prep.finish(settings, NOW, out.append, warns.append)
    assert prep._load()["jobs"] == [] and [i for i, _ in prep.read_list()] == [a["id"]]


def test_a_branch_sent_back_by_the_prep_pass_gets_an_agent_again(q, herdr_log, env, monkeypatch):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    commit(a_wt, {"more.txt": "changed by the prep agent"})
    monkeypatch.setenv("FAKE_REVIEW_FAIL", "task/one")
    set_agent_status(env, "prep", "idle")
    out, _ = finish_prep(settings)
    assert tk.load_ticket(a["id"])["status"] == "claimed"
    assert a["id"].lower() in agent_names(herdr_log)
    assert any("relaunched" in line for line in out)


def test_a_sent_back_ticket_that_cannot_get_an_agent_says_so_once(q, herdr_log, env, ntfy, monkeypatch):
    from test_prep import sent
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    commit(a_wt, {"more.txt": "changed by the prep agent"})
    monkeypatch.setenv("FAKE_REVIEW_FAIL", "task/one")
    monkeypatch.setattr(lc, "relaunch", lambda t, s, w: (1, f"{t['id']}  relaunch failed: no room"))
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    notes = [n["text"] for n in tk.load_ticket(a["id"])["notes"]]
    assert any("no agent could be started" in n for n in notes)
    pings = [p for p in sent(ntfy) if "needs a human" in p[0]]
    assert len(pings) == 1 and pings[0][2] == "high"   # one ping, high priority


@pytest.mark.skipif(not os.environ.get("FLEET_TEST_REAL_CLAUDE"),
                    reason="set FLEET_TEST_REAL_CLAUDE=1 to spend one tiny real `claude -p` call")
def test_claude_p_starts_with_claudecode_set():
    """review.sh calls `claude -p` from validate(), which sets CLAUDECODE=1: it must not refuse."""
    import pwd
    env = dict(os.environ, CLAUDECODE="1", HOME=pwd.getpwuid(os.getuid()).pw_dir)   # the tests fake HOME
    done = subprocess.run(["claude", "-p", "reply with the single word ok"], env=env,
                          capture_output=True, text=True, timeout=120, cwd="/tmp", stdin=subprocess.DEVNULL)
    assert done.returncode == 0 and "ok" in done.stdout.lower()


def test_the_recheck_runs_as_the_agent_that_built_it_so_the_other_model_reviews(q, herdr_log, env, monkeypatch):
    a, a_wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep(force=True)
    settings["builder"] = "codex"
    commit(a_wt, {"more.txt": "changed by a codex prep agent"})
    seen = []
    real = prep._run

    def spy(cmd, cwd, timeout, env=None):
        seen.append(dict(env or {}))
        return real(cmd, cwd, timeout, env)

    monkeypatch.setattr(prep, "_run", spy)
    monkeypatch.setenv("CLAUDECODE", "1")   # the dispatcher itself may run under either identity
    monkeypatch.setenv("REVIEWER", "codex")
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert len(seen) == 2
    assert all("CLAUDECODE" not in e and e.get("CODEX_SANDBOX") == "1" and "REVIEWER" not in e for e in seen)


# --- BD-85: every ping is logged, one ship-list ping, duplicates are only proposed ---------

def _log_text():
    return dp.log_path().read_text(encoding="utf-8") if dp.log_path().exists() else ""


def test_the_ship_list_ping_is_logged_as_sent(q, herdr_log, env, ntfy):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "1"})
    settings, _, _ = start_prep()
    report(settings, order=[a["id"], b["id"]])
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert len(ntfy.requests) == 1
    assert "PING_SENT ship_list list" in _log_text()


def test_a_ping_that_cannot_be_sent_is_logged_with_its_reason(q, herdr_log, env):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "1"})
    settings, _, _ = start_prep()
    report(settings, order=[a["id"], b["id"]])
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)            # no notify.json in this home
    assert "PING_FAILED ship_list list: notifications are off" in _log_text()


def test_an_owed_ping_that_is_dropped_is_logged(q, herdr_log, env, ntfy):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    with prep._state() as st:
        st["pings"].append({"kind": "list", "ids": ["ZZ-1"]})   # not a ticket in review
    prep.flush_pings(dp.load_settings(), NOW, lambda m: None)
    assert ntfy.requests == []
    assert "PING_FAILED ship_list ZZ-1: dropped" in _log_text()
    assert prep._load()["pings"] == [] and a["status"] == "review"


def test_a_ship_ping_owed_after_a_list_ping_is_not_sent_separately(q, herdr_log, env, ntfy):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "1"})
    with prep._state() as st:
        st["covered"] = [f"{t['id']}:{prep.tip_of(q, t['branch'])}" for t in (a, b)]   # no pass follows
        prep._defer_ping(st, "list", [a["id"], b["id"]])
        prep._defer_ping(st, "ship", [a["id"]])              # already spoken for: not even recorded
        assert [p["kind"] for p in st["pings"]] == ["list"]
        st["pings"].append({"kind": "ship", "ids": [a["id"]]})   # an older record
    prep.flush_pings(dp.load_settings(), NOW, lambda m: None)
    assert [r["body"]["title"] for r in ntfy.requests] == ["2 ready to ship, list ready"]
    assert "not sent: the ship list ping speaks for it" in _log_text()


def test_a_ticket_with_an_owed_ping_has_no_ready_ping_of_its_own(q, herdr_log, env):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    settings = dp.load_settings()
    with prep._state() as st:
        st["pings"].append({"kind": "list", "ids": [a["id"]]})
    assert prep.holds_ping(a, tk.list_tickets(), settings, NOW) is True


def test_ship_list_shows_proposed_cancellations_and_does_not_call_them_late(q, herdr_log, env, capsys):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "1"})
    prep._write_list([f"{a['id']} | One | low | does one | -"], NOW, [prep._proposal_line(b, a["id"])])
    assert [i for i, _ in prep.read_proposals()] == [b["id"]]
    with prep._state() as st:   # the prep pass covers a proposed duplicate too
        st["covered"] += [f"{a['id']}:{prep.tip_of(q, 'task/one')}", f"{b['id']}:{prep.tip_of(q, 'task/two')}"]
    assert ship_main(["--list"]) == 0
    cap = capsys.readouterr()
    assert "Proposed cancellations" in cap.out and f"ticket close {b['id']} --cancelled" in cap.out
    assert "NOT on the ship list" not in cap.err
    assert tk.load_ticket(b["id"])["status"] == "review"      # shown, never shipped or cancelled


def test_a_keeper_is_never_proposed_as_another_groups_duplicate(q, herdr_log, env):
    big, _ = in_review(q, "task/big", {"a.txt": "1"}, body="the biggest spec " * 30)
    mid, _ = in_review(q, "task/mid", {"b.txt": "1"}, body="a middle spec " * 10)
    low, _ = in_review(q, "task/low", {"c.txt": "1"}, body="short")
    settings, _, _ = start_prep()
    report(settings, order=[big["id"]], duplicates=[{"keep": mid["id"], "cancel": [low["id"]]},
                                                    {"keep": big["id"], "cancel": [mid["id"]]}])
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert [i for i, _ in prep.read_proposals()] == [low["id"]]   # mid stays a keeper
    assert mid["id"] in [i for i, _ in prep.read_list()]


def test_a_fleet_alert_is_logged_too(env, ntfy):
    from fleet import notify
    notify.send_alert("dispatcher_down", "Fleet dispatcher is down", "text")
    assert "PING_SENT dispatcher_down fleet" in _log_text()


def test_only_a_failed_launch_counts_as_an_attempt_on_the_pacific_day(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    in_review(q, "task/two", {"two.txt": "2"})
    start_prep()                                               # a launch that works
    assert prep._load()["attempts"]["n"] == 0
    assert prep._load()["last_pass"] == "2026-10-03T10:00:00Z"


def test_the_pass_cap_holds_a_second_pass_after_prep_time(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    in_review(q, "task/two", {"two.txt": "2"})
    settings = dp.load_settings()
    state = dict(prep._empty(), last_pass="2026-10-03T13:30:00Z")
    after = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)   # 08:00 Pacific, 1.5 h later
    assert prep._pass_cap_holds(settings, state, after)
    assert not prep._pass_cap_holds(settings, state, datetime(2026, 10, 3, 17, 31, tzinfo=timezone.utc))
    assert not prep._pass_cap_holds(dict(settings, prep_pass_gap_hours=0), state, after)
    assert not prep._pass_cap_holds(settings, state, JUST_BEFORE_SIX)


JUST_BEFORE_SIX = datetime(2026, 10, 3, 12, 59, tzinfo=timezone.utc)


def test_failed_launches_count_up_to_the_cap_and_reset_at_pacific_midnight(q, herdr_log, env, monkeypatch):
    in_review(q, "task/one", {"one.txt": "1"})
    in_review(q, "task/two", {"two.txt": "2"})

    def boom(*a, **k):
        raise lc.LaunchError("no herdr")
    monkeypatch.setattr(prep, "_start_job", boom)
    for _ in range(prep.MAX_ATTEMPTS_PER_DAY):
        start_prep()
    assert prep._load()["attempts"] == {"day": "2026-10-03", "n": prep.MAX_ATTEMPTS_PER_DAY}
    _, out, _ = start_prep()
    assert "launch attempts failed today" in out[0]
    late = datetime(2026, 10, 4, 7, 30, tzinfo=timezone.utc)   # 00:30 Pacific on the 4th
    start_prep(now=late)
    assert prep._load()["attempts"] == {"day": "2026-10-04", "n": 1}


