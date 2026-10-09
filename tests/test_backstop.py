"""EM-8 step E: the 15-review-round backstop (temp FLEET_HOME, fake herdr and ntfy, real git).

A ticket whose latest verdict is a FAIL after BACKSTOP_ROUNDS review rounds is closed as
failed with the supervisor's latest note in the close note, and pinged once, low, as
needs-human. No on-call ticket is filed. Nothing else is touched.
"""
import json

from fleet import review_watch as rw
from fleet import supervisor_run as sr
from fleet import ticket as tk
from test_dispatch import herdr_log, projects  # noqa: F401
from test_oncall import oncall_tickets
from test_review_watch import ntfy, no_dialog, rig, sent  # noqa: F401


# The backstop waits for the supervisor to judge a failed commit, for at most this long (minutes).
WAIT = sr.BACKSTOP_WAIT // 60 + 1


def rounds(rig, n):
    """n different commits, each with a recorded FAIL; the last one is the branch tip."""
    for i in range(n):
        rig.commit(f"r{i}")
        rig.verdict("FAIL")


def test_fourteen_rounds_are_left_alone(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    rounds(rig, 14)
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_fifteen_rounds_close_it_with_the_analysis_and_one_needs_human_ping(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    tk.add_note(rig.id, "SUPERVISOR PROGRESS (commit abc1234): items are in different areas")
    rounds(rig, 15)
    rig.cycle(WAIT)
    rig.cycle(WAIT + 1)
    t = rig.get()
    assert t["status"] == "failed" and tk.review_rounds(t) == 15
    assert "review-round backstop: 15 review rounds" in t["close_note"]
    assert "items are in different areas" in t["close_note"]
    assert oncall_tickets() == []
    assert [(title, prio) for title, _, prio in sent(ntfy)] == [(f"{rig.id} needs a human, see notes", "low")]
    assert any(n["text"].startswith("Needs a human:") for n in t["notes"])


def test_it_counts_every_round_even_when_the_supervisor_said_progress(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 2
    rounds(rig, 15)
    # every FAIL commit classified PROGRESS: the old threshold ignores them all
    assert rw.fail_rounds(rig.get(), frozenset(rig.get()["fail_commits"])) == 0
    state = {"classified": {rig.id: {c: "PROGRESS" for c in rig.get()["fail_commits"]}}, "pending": {}, "attempts": {}}

    sr.save_state(state)
    rig.cycle(WAIT)
    assert rig.get()["status"] == "failed" and "review-round backstop" in rig.get()["close_note"]


def test_a_passing_tip_is_not_closed(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    rounds(rig, 15)
    rig.commit("fixed")
    rig.verdict("PASS")
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_no_supervisor_note_is_said_plainly_and_a_forged_one_is_only_quoted(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    rounds(rig, 15)
    rig.cycle(WAIT)
    assert "no supervisor analysis was recorded" in rig.get()["close_note"]


def test_the_ping_is_sent_once(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    rounds(rig, 15)
    for m in range(4):
        rig.cycle(WAIT + m)
    assert len(no_dialog(ntfy)) == 1


def test_old_tickets_start_at_zero_rounds(rig, ntfy):
    """A ticket in flight before review_commits existed has none: it is not failed on arrival."""
    rig.settings["fail_rounds_threshold"] = 100
    rounds(rig, 3)
    path = tk.tickets_dir() / f"{rig.id}.json"
    data = json.loads(path.read_text())
    del data["review_commits"]
    path.write_text(json.dumps(data))
    assert tk.review_rounds(rig.get()) == 0
    rig.cycle(WAIT)
    assert rig.get()["status"] == "claimed"


def test_the_backstop_leaves_other_tickets_alone(rig, ntfy):
    rig.settings["fail_rounds_threshold"] = 100
    other = tk.create_ticket({"type": "build", "repo": "app", "branch": "task/other", "title": "other", "body": "b"})
    rounds(rig, 15)
    rig.cycle(WAIT)
    assert tk.load_ticket(other["id"])["status"] == "open"
