"""EM-9 review fixes: branch ownership, tips recorded early, keeper validated first, odd reports."""

import json
from datetime import datetime, timezone

import pytest

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import prep
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import calls, git, herdr_log, projects, mk, claim  # noqa: F401
from test_prep import (agent_names, commit, conflicting, finish_prep, in_review, log_lines, merge, q,  # noqa: F401
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
    settings, _, _ = start_prep()
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
    settings, _, _ = start_prep()
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
    settings, _, _ = start_prep()
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
    settings, _, _ = start_prep()
    report(settings, order=bad, tickets=bad, duplicates=[{"keep": bad, "cancel": bad}, bad])
    set_agent_status(env, "prep", "idle")
    _, warns = finish_prep(settings)
    assert not warns and [i for i, _ in prep.read_list()] == [a["id"]]
