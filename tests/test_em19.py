"""EM-19: stall only when idle, failed tabs closed, note/MUST FIX copying, fail_commits cap,
and the sweep of stale agent tabs (fake herdr with a world file, temp FLEET_HOME)."""
import json

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import review_watch as rw
from fleet import ticket as tk
from test_dispatch import calls, claim, herdr_log, mk, projects  # noqa: F401
from test_review_watch import ntfy, rig  # noqa: F401


def world(rig, agents, tabs, workspaces=None):
    rig.world.write_text(json.dumps({
        "workspaces": workspaces or [{"workspace_id": "w1", "label": "fleet"}],
        "tabs": tabs, "agents": agents}))


def tab(label, tab_id, ws="w1", focused=False):
    return {"tab_id": tab_id, "label": label, "workspace_id": ws, "focused": focused, "agent_status": "unknown"}


def tab_labels(rig):
    return sorted(t["label"] for t in json.loads(rig.world.read_text())["tabs"])


# --- 1. a stall counts only while the agent is idle or waiting ----------------------

def test_a_busy_agent_silent_for_sixty_minutes_is_not_failed(rig, ntfy):
    rig.set_agent("working", revision=1)
    for minutes in (0, 30, 60, 120):
        rig.cycle(minutes)
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_the_stall_clock_starts_when_the_agent_stops_working(rig, ntfy):
    rig.set_agent("working", revision=1)
    rig.cycle(0)
    rig.cycle(90)                       # silent for 90 minutes, but busy
    rig.set_agent("idle", revision=1)
    rig.cycle(91)                       # idle only now
    rig.cycle(120)
    assert rig.get()["status"] == "claimed"
    rig.cycle(137)
    assert rig.get()["status"] == "failed" and "stalled" in rig.get()["close_note"]


# --- 2. the failed ticket's tab is closed in recovery, never under a running agent ---

def test_a_failed_tickets_tab_is_closed_after_the_on_call_ticket_is_filed(rig, ntfy):
    world(rig, [{"name": rig.id.lower(), "agent_status": "idle", "revision": 1, "tab_id": "w1:t7"}],
          [tab("fleet", "w1:t1"), tab(rig.id, "w1:t7")])
    rig.cycle(0)
    rig.cycle(46)
    assert rig.get()["status"] == "failed"
    assert [x["parent"] for x in tk.list_tickets() if x.get("oncall")] == [rig.id]
    assert tab_labels(rig) == ["fleet"]
    assert any("closed" in line and rig.id in line for line in rig.lines)


def test_a_failed_tickets_tab_stays_while_its_agent_still_runs(rig, ntfy, herdr_log):
    world(rig, [{"name": rig.id.lower(), "agent_status": "blocked", "tab_id": "w1:t7"}],
          [tab("fleet", "w1:t1"), tab(rig.id, "w1:t7")])
    rig.cycle(0)
    rig.cycle(21)
    assert rig.get()["status"] == "failed"
    assert tab_labels(rig) == sorted(["fleet", rig.id])
    assert not [c for c in calls(herdr_log) if c[:2] == ["tab", "close"]]


# --- 3. whitespace in copied notes ------------------------------------------------

def test_copied_notes_are_not_duplicated_when_only_whitespace_differs(rig, ntfy):
    rig.edit(notes=[{"at": "2026-10-04T10:00:00Z", "by": "claude", "text": "a   b  \n\n\n\nc\t d"}])
    failed = rig.get()   # as it was when the notes were copied
    rig.set_agent("idle", revision=1)
    rig.cycle(0)
    rig.cycle(46)
    (oc,) = [x for x in tk.list_tickets() if x.get("oncall")]
    before = [n["text"] for n in tk.load_ticket(oc["id"])["notes"]]
    assert any("a b\n\nc d" in n for n in before)
    path = tk.tickets_dir() / f"{oc['id']}.json"
    data = json.loads(path.read_text())
    for n in data["notes"]:
        n["text"] = n["text"].replace(" ", "  ")   # the store or a mirror re-spaced them
    path.write_text(json.dumps(data))
    rw._copy_notes(oc["id"], failed, lambda m: None)
    assert len(tk.load_ticket(oc["id"])["notes"]) == len(before)


# --- 4. long MUST FIX sections are split, not cut ---------------------------------

def test_split_keeps_every_line_and_the_limit():
    text = "\n".join(f"{i}. file.py: problem number {i} " + "x" * 60 for i in range(30))
    parts = rw._split(text, 500)
    assert len(parts) > 1 and all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == text
    assert rw._split("y" * 1200, 500) == ["y" * 500, "y" * 500, "y" * 200]


def test_a_long_must_fix_reaches_the_on_call_body_in_parts(rig):
    items = "\n".join(f"{i}. a.py: fix thing {i} " + "z" * 80 for i in range(1, 21))
    d = rig.wt / ".reviews"
    d.mkdir()
    (d / "codex.md").write_text(f"Reviewer: Codex\nCommit: {rig.tip()}\n\nVERDICT: FAIL\n"
                                f"MUST FIX:\n{items}\nSHOULD FIX: none\nSUMMARY: x\n")
    body = rw._oncall_body(rig.get(), "why", rig.tip(), rig.repo)
    assert "(part 1/" in body and "20. a.py: fix thing 20" in body


# --- 5. fail_commits is capped ----------------------------------------------------

def test_fail_commits_keeps_only_the_newest(rig):
    shas = [f"{i:040x}" for i in range(1, tk.FAIL_COMMITS_MAX + 11)]
    for sha in shas:
        tk.record_verdict("task/demo", "app", "FAIL", sha, "Codex")
    assert rig.get()["fail_commits"] == shas[-tk.FAIL_COMMITS_MAX:]


# --- the sweep of stale tabs ------------------------------------------------------

def finished(branch, outcome, **over):
    t = claim(mk(branch=f"task/{branch}", **over))
    if outcome == "review":
        return tk.set_status(t["id"], "review", allowed_from=("claimed",))
    if outcome == "claimed":
        return t
    return tk.close_ticket(t["id"], outcome, "closed in a test")


def test_sweep_closes_only_stale_tabs_of_the_agents_workspace(rig):
    done = finished("a", "done")
    failed = finished("b", "failed")
    cancelled = finished("c", "cancelled")
    claimed = finished("d", "claimed")
    review = finished("e", "review")
    running = finished("f", "failed")
    tabs = [tab("fleet", "w1:t1"), tab(done["id"], "w1:t2", focused=True), tab(failed["id"], "w1:t3"),
            tab(cancelled["id"], "w1:t4"), tab(claimed["id"], "w1:t5"), tab(review["id"], "w1:t6"),
            tab(running["id"], "w1:t7"), tab(done["id"], "w2:t1", ws="w2")]
    agents = [{"name": done["id"].lower(), "agent_status": "idle", "tab_id": "w1:t2"},
              {"name": failed["id"].lower(), "agent_status": "done", "tab_id": "w1:t3"},
              {"name": claimed["id"].lower(), "agent_status": "working", "tab_id": "w1:t5"},
              {"name": running["id"].lower(), "agent_status": "working", "tab_id": "w1:t7"}]
    world(rig, agents, tabs, [{"workspace_id": "w1", "label": "fleet"}, {"workspace_id": "w2", "label": "fleet-tools"}])
    swept = lc.sweep_tabs(rig.settings, lambda m: None)
    assert sorted(i for i, _ in swept) == sorted([done["id"], failed["id"], cancelled["id"]])
    left = json.loads(rig.world.read_text())["tabs"]
    assert sorted(t["tab_id"] for t in left) == ["w1:t1", "w1:t5", "w1:t6", "w1:t7", "w2:t1"]
    log = dp.log_path().read_text()
    assert log.count("TAB_SWEPT") == 3 and all(i in log for i, _ in swept)


def test_sweep_asks_herdr_nothing_when_no_ticket_is_finished(rig, herdr_log):
    assert lc.sweep_tabs(rig.settings, lambda m: None) == []
    assert not [c for c in (calls(herdr_log) if herdr_log.exists() else []) if c[:1] == ["tab"]]
