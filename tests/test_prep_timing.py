"""BD-61: the ready ping waits for the prep pass that covers a ticket, the pass runs over the pile
(not once per ticket), and `ship --list` never silently skips a ticket that reached review late."""

import json
from datetime import datetime, timezone

from fleet import dispatch as dp
from fleet import prep
from fleet import review_watch as rw
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import git, herdr_log, mk, projects  # noqa: F401
from test_prep import (NIGHT, SIX, agent_names, finish_prep, in_review, log_lines, ntfy, q,  # noqa: F401
                       report, sent, set_agent_status, start_prep, write_list)

THREE_PM = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)   # 15:00 Pacific


def ready(q, monkeypatch, branch, files):
    """A ticket in review whose recorded PASS and summary make it ready to ship."""
    t, wt = in_review(q, branch, files)
    tip = git(q, "rev-parse", branch).strip()
    tk.record_verdict(branch, "app", "PASS", tip, "Codex")
    monkeypatch.setenv("CLAUDECODE", "1")
    try:
        tk.add_note(t["id"], "All done.")
    finally:
        monkeypatch.delenv("CLAUDECODE")
    return t


def watch_cycle(now, reserve=None):
    """The parts of a dispatch cycle that matter here, in its order: finish, review watch, maybe start."""
    settings = dp.load_settings()
    out, warns = [], []
    prep.finish(settings, now, out.append, warns.append)
    rw.check(settings, now, out.append, warns.append)
    prep.maybe_start(settings, now, out.append, warns.append, reserve)
    return out, warns


def titles(ntfy):
    return [title for title, _, _ in sent(ntfy)]


def test_a_single_ticket_reaching_review_after_prep_time_gets_no_prep_and_one_ping(q, herdr_log, env, ntfy, monkeypatch):
    t = ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    for _ in range(3):
        watch_cycle(THREE_PM)
    assert agent_names(herdr_log) == []
    assert titles(ntfy) == [f"{t['id']} ready to ship"]


def test_a_ship_right_after_the_ready_ping_is_not_refused(q, herdr_log, env, ntfy, monkeypatch):
    t = ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    watch_cycle(THREE_PM)
    assert titles(ntfy) == [f"{t['id']} ready to ship"]
    assert ship_main([t["id"]]) == 0
    assert log_lines(env, "finish") == ["one"]


def test_two_waiting_tickets_get_one_prep_pass_and_one_ping_after_it(q, herdr_log, env, ntfy, monkeypatch):
    a = ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    b = ready(q, monkeypatch, "task/two", {"two.txt": "2"})
    watch_cycle(THREE_PM)                                      # nothing left to build: the pass starts
    assert agent_names(herdr_log) == ["prep"]
    assert titles(ntfy) == []                                  # no "ready to ship" while it works
    assert ship_main([a["id"]]) == 1                           # (and ship would be refused, which is why)
    set_agent_status(env, "prep", "idle")
    report(dp.load_settings(), order=[a["id"], b["id"]], tickets={}, duplicates=[])
    out, _ = watch_cycle(THREE_PM)
    prep.wait_checks()
    watch_cycle(THREE_PM)
    watch_cycle(THREE_PM)
    assert titles(ntfy) == ["2 ready to ship, list ready"]
    assert agent_names(herdr_log) == ["prep"]                  # one pass, not one per ticket
    assert ship_main([a["id"]]) == 0                           # never refused after the ping


def test_the_pass_runs_once_at_prep_time_and_later_arrivals_wait_for_nothing_left_to_build(
        q, herdr_log, env, ntfy, monkeypatch):
    ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    ready(q, monkeypatch, "task/two", {"two.txt": "2"})
    mk(branch="task/queued")                                   # work remains: only prep_time can start a pass
    watch_cycle(NIGHT)                                         # 03:00, before prep_time
    assert agent_names(herdr_log) == [] and titles(ntfy) == []   # a pass is coming at 06:00: the pings wait
    watch_cycle(SIX)
    assert agent_names(herdr_log) == ["prep"]
    set_agent_status(env, "prep", "idle")
    finish_prep(dp.load_settings(), SIX)
    ready(q, monkeypatch, "task/three", {"three.txt": "3"})    # arrives later in the day, work still queued
    n = len(ntfy.requests)
    watch_cycle(THREE_PM)
    watch_cycle(THREE_PM)
    assert agent_names(herdr_log) == ["prep"]                  # no second pass
    assert len(ntfy.requests) == n + 1 and titles(ntfy)[-1].endswith("ready to ship")   # pinged at once


def test_one_waiting_ticket_alone_never_starts_a_pass_even_with_nothing_to_build(q, herdr_log, env, ntfy, monkeypatch):
    ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    for now in (NIGHT, SIX, THREE_PM):
        watch_cycle(now)
    assert agent_names(herdr_log) == []


def test_ship_list_reports_a_ticket_that_reached_review_after_the_list_was_written(q, herdr_log, env, capsys):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    late, _ = in_review(q, "task/late", {"late.txt": "2"})     # bounced and back, say: not on the list
    write_list([f"{a['id']} | T | medium | does 1 | -"])
    assert ship_main(["--list"]) == 0
    captured = capsys.readouterr()
    assert log_lines(env, "finish") == ["one"]
    assert f"{late['id']} is in review but NOT on the ship list" in captured.err
    assert "reached review after the list was written" in captured.err
    assert tk.load_ticket(late["id"])["status"] == "review"


def test_two_tickets_finishing_in_one_cycle_get_no_ping_before_the_pass_that_locks_them(
        q, herdr_log, env, ntfy, monkeypatch):
    ids = []
    for name in ("one", "two"):
        t = ready(q, monkeypatch, f"task/{name}", {f"{name}.txt": name})
        tk.set_status(t["id"], "claimed", allowed_from=("review",))   # still claimed, its agent idle
        ids.append(t["id"])
    w = json.loads((env / "world.json").read_text())
    w["agents"] = [{"name": i.lower(), "agent_status": "idle"} for i in ids]
    (env / "world.json").write_text(json.dumps(w))
    watch_cycle(THREE_PM)
    assert [tk.load_ticket(i)["status"] for i in ids] == ["review", "review"]
    assert agent_names(herdr_log) == ["prep"] and titles(ntfy) == []   # not "ready" for a ship that would be refused
    assert ship_main([ids[0]]) == 1


def test_an_older_review_ticket_is_not_pinged_before_a_ticket_that_moves_in_the_same_cycle(
        q, herdr_log, env, ntfy, monkeypatch):
    a = ready(q, monkeypatch, "task/one", {"one.txt": "1"})     # in review, ping not sent yet
    b = ready(q, monkeypatch, "task/two", {"two.txt": "2"})
    tk.set_status(b["id"], "claimed", allowed_from=("review",))
    (env / "world.json").write_text(json.dumps({"workspaces": [{"workspace_id": "w1", "label": "fleet"}], "tabs": [],
                                                "agents": [{"name": b["id"].lower(), "agent_status": "idle"}]}))
    watch_cycle(THREE_PM)
    assert agent_names(herdr_log) == ["prep"] and titles(ntfy) == []
    assert ship_main([a["id"]]) == 1


def test_tickets_arriving_while_a_pass_runs_are_not_pinged_before_the_next_pass(q, herdr_log, env, ntfy, monkeypatch):
    ready(q, monkeypatch, "task/one", {"one.txt": "1"})
    ready(q, monkeypatch, "task/two", {"two.txt": "2"})
    watch_cycle(THREE_PM)
    assert agent_names(herdr_log) == ["prep"]
    for i in (1, 2):
        ready(q, monkeypatch, f"task/late{i}", {f"late{i}.txt": "x"})
    watch_cycle(THREE_PM)                                      # the first pass is still running
    assert titles(ntfy) == []                                  # the next pass will lock these two


def test_the_launch_limit_day_is_the_local_day_not_the_utc_day(q, env):
    settings = dp.load_settings()
    evening = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)     # 19:00 Pacific, 3 Oct
    morning = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)     # 01:00 Pacific, 4 Oct: same UTC day
    state = prep._load()
    state["limit_day"] = dp.local_day(evening, settings["timezone"])
    assert not prep._can_launch(settings, state, evening)
    assert prep._can_launch(settings, state, morning)              # the budget reset at local midnight
