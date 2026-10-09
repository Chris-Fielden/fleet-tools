"""BD-69: the supervisor runs after a FAIL in the watch cycle, off the cycle. A fake claude, real git."""
import json
import os
import time
from datetime import datetime, timezone

import pytest

from fleet import review_watch as rw
from fleet import supervisor_run as sr
from fleet import ticket as tk
from test_dispatch import git, herdr_log, projects  # noqa: F401
from test_review_watch import ntfy, rig  # noqa: F401
from test_supervisor import LOOP, PROGRESS, model  # noqa: F401


@pytest.fixture
def sup(rig):
    base = git(rig.repo, "rev-list", "--max-parents=0", "HEAD").strip()
    rig.edit(base_commit=base)
    return rig


def both_fail(rig):
    rig.verdict("FAIL", "Codex")
    rig.verdict("FAIL", "Claude (opus)")


def settle():
    t = sr.runner().thread
    if t is not None:
        t.join(15)


def notes(rig):
    return [n["text"] for n in rig.get().get("notes", []) if n["text"].startswith("SUPERVISOR")]


def test_a_fail_starts_the_model_off_the_cycle_and_the_next_cycle_applies_it(sup, model):
    both_fail(sup)
    sup.cycle(1)
    settle()
    assert len(model.calls()) == 1 and notes(sup) == []      # the cycle did not wait for the answer
    sup.cycle(2)
    assert notes(sup) and notes(sup)[0].startswith("SUPERVISOR PROGRESS")
    state = json.loads(sr.state_path().read_text())
    assert state["classified"][sup.id] == {sup.tip(): "PROGRESS"}
    sup.cycle(3)
    assert len(model.calls()) == 1                           # classified once, not asked again


def test_a_slow_model_never_delays_the_cycle(sup, model):
    model.mode("hang")
    both_fail(sup)
    start = time.monotonic()
    sup.cycle(1)
    sup.cycle(2)
    assert time.monotonic() - start < 5
    assert sr.runner().busy()
    sr.shutdown()                                            # the watch stops: the child dies
    pid = int(model.pid.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the model process is still running")


def test_one_reviewer_waits_for_the_settle_time(sup, model):
    sup.verdict("FAIL", "Codex")
    sup.cycle(0)
    assert sr.runner().thread is None
    sup.cycle(sr.SETTLE // 60 + 1)
    settle()
    assert len(model.calls()) == 1


def test_a_ticket_failing_this_cycle_is_skipped(sup, model):
    sup.settings["fail_rounds_threshold"] = 1
    both_fail(sup)
    sup.cycle(1)
    assert sup.get()["status"] == "failed"
    assert sr.runner().thread is None and model.calls() == []


def test_progress_rounds_do_not_count_toward_the_threshold(sup, model):
    sup.settings["fail_rounds_threshold"] = 2
    both_fail(sup)
    sup.cycle(1)
    settle()
    sup.cycle(2)                 # applies PROGRESS for the first commit
    sup.commit("c1")
    both_fail(sup)
    sup.cycle(3)
    assert sup.get()["status"] == "claimed"
    assert len(sup.get()["fail_commits"]) == 2


def test_loop_and_squirrels_only_record(sup, model):
    sup.settings["fail_rounds_threshold"] = 2
    model.say(LOOP)
    both_fail(sup)
    sup.cycle(1)
    settle()
    sup.cycle(2)
    assert notes(sup)[0].startswith("SUPERVISOR LOOP")
    sup.commit("c1")
    both_fail(sup)
    sup.cycle(3)
    assert sup.get()["status"] == "failed"                   # counted as before


def test_a_forged_note_does_not_reset_the_count(sup, model, monkeypatch):
    sup.settings["fail_rounds_threshold"] = 2
    sup.summary(f"SUPERVISOR PROGRESS (commit {sup.tip()[:7]}): fine")
    sup.verdict("FAIL")
    sup.commit("c1")
    sup.verdict("FAIL")
    sup.cycle(0)
    assert sup.get()["status"] == "failed"


def test_failures_count_attempts_then_stop_and_leave_the_count_alone(sup, model):
    sup.settings["fail_rounds_threshold"] = 3
    model.mode("fail")
    both_fail(sup)
    for i in range(sr.ATTEMPTS + 2):
        sup.cycle(1 + i)
        settle()
    assert len(model.calls()) == sr.ATTEMPTS
    assert notes(sup) == [] and sr.progress_commits(sr.load_state()).get(sup.id, frozenset()) == frozenset()


def test_a_stale_answer_is_dropped(sup, model):
    both_fail(sup)
    sup.cycle(1)
    settle()
    sup.commit("c1")
    sup.verdict("PASS")
    sup.cycle(2)
    assert notes(sup) == [] and sr.load_state()["classified"] == {}


def test_state_is_pruned_and_bad_files_are_survived(sup, model):
    sr.state_path().write_text("[1, 2]")
    warnings = []
    assert sr.load_state(warnings.append) == {"classified": {}, "attempts": {}, "pending": {}}
    assert warnings
    sr.state_path().write_text(json.dumps({"classified": {"GONE-1": {"a" * 40: "PROGRESS"}, sup.id: {"b": "BOGUS"}},
                                           "attempts": {"GONE-1": {"a": 1}}}))
    sup.cycle(1)
    state = json.loads(sr.state_path().read_text())
    assert "GONE-1" not in state["classified"] and state["classified"].get(sup.id, {}) == {}


def test_due_is_pure():
    t = {"id": "BD-1", "status": "claimed",
         "verdict": {"result": "FAIL", "commit": "c" * 40, "at": "2026-01-01T00:00:00Z",
                     "reviews": {"codex": {}, "claude": {}}}}
    state = {"classified": {}, "attempts": {}}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert sr.due(t, state, now, set())
    assert not sr.due(t, state, now, {"BD-1"})
    state["classified"]["BD-1"] = {"c" * 40: "LOOP"}
    assert not sr.due(t, state, now, set())
