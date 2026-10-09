"""BD-70: the LOOP action - DESIGN CHANGE note, then a fresh session on the same branch.
Fake claude, fake herdr, real git."""
import json
from datetime import datetime, timezone

import pytest

from fleet import dispatch_cli
from fleet import launch as lc
from fleet import supervisor_run as sr
from fleet import ticket as tk
from test_dispatch import calls, git, herdr_log, projects  # noqa: F401
from test_review_watch import ntfy, rig  # noqa: F401
from test_supervisor import LOOP, model  # noqa: F401
from test_supervisor_run import both_fail, notes, settle, sup  # noqa: F401

DESIGN = "the retry logic is spread over three callers; move it into one function."


def tabbed(rig, status="working", extra=(), focused=False):
    rig.world.write_text(json.dumps({
        "workspaces": [{"workspace_id": "w1", "label": "fleet"}],
        "tabs": [{"tab_id": "w1:t4", "label": rig.id, "workspace_id": "w1", "focused": focused}],
        "agents": [{"name": rig.id.lower(), "agent_status": status, "tab_id": "w1:t4"}, *extra]}))


def prompts(herdr_log):
    return [c[3] for c in calls(herdr_log) if c[:2] == ["agent", "prompt"]]


def looped(sup, model):
    """A FAIL, the model says LOOP, the answer is collected on the next cycle."""
    model.say(LOOP)
    both_fail(sup)
    sup.cycle(1)
    settle()


def test_loop_writes_the_note_and_starts_a_fresh_session_on_the_same_branch(sup, model, herdr_log):
    tabbed(sup)
    looped(sup, model)
    sup.cycle(2)
    assert any(n.startswith("SUPERVISOR LOOP") and "DESIGN CHANGE:" in n and DESIGN in n for n in notes(sup))
    got = calls(herdr_log)
    assert ["tab", "close", "w1:t4"] in got
    (prompt,) = prompts(herdr_log)
    assert "DESIGN CHANGE" in prompt and DESIGN in prompt and sup.ticket["title"] in prompt
    assert sup.get()["status"] == "claimed" and sup.get()["branch"] == "task/demo"
    assert sr.load_state()["pending"] == {}
    assert "SUPERVISOR_RESTART" in (tk.fleet_home() / "dispatch.log").read_text()


def test_stop_agent_leaves_a_tab_that_holds_other_agents_alone(sup, model, herdr_log):
    tabbed(sup, extra=[{"name": "someone-else", "agent_status": "idle", "tab_id": "w1:t4"}])
    looped(sup, model)
    sup.cycle(2)
    assert ["tab", "close", "w1:t4"] not in calls(herdr_log) and prompts(herdr_log) == []
    assert sr.load_state()["pending"][sup.id]["tries"] == 1          # kept, retried next cycle
    ok, why = lc.stop_agent(sup.id, sup.settings)
    assert not ok and "someone-else" in why


def test_stop_agent_stops_only_its_own_ticket(sup, herdr_log):
    other = {"name": "xx-9", "agent_status": "working", "tab_id": "w1:t9"}
    tabbed(sup, extra=[other])
    world = json.loads(sup.world.read_text())
    world["tabs"].append({"tab_id": "w1:t9", "label": "XX-9", "workspace_id": "w1", "focused": False})
    sup.world.write_text(json.dumps(world))
    assert lc.stop_agent(sup.id, sup.settings)[0]
    assert [c for c in calls(herdr_log) if c[:2] == ["tab", "close"]] == [["tab", "close", "w1:t4"]]
    assert lc.stop_agent("XX-0", sup.settings) == (True, "no agent is running")


def test_a_focused_tab_is_left_alone(sup, model, herdr_log):
    tabbed(sup, focused=True)
    ok, why = lc.stop_agent(sup.id, sup.settings)
    assert not ok and "focused" in why and ["tab", "close", "w1:t4"] not in calls(herdr_log)


def test_a_restart_text_that_trips_the_secret_scan_is_never_sent(sup, model, herdr_log, monkeypatch):
    real = tk.check_secrets
    monkeypatch.setattr(tk, "check_secrets",
                        lambda title, body: ["secret"] if title == "restart prompt" and f"{DESIGN}\nMake this" in body
                        else real(title, body))
    tabbed(sup)
    looped(sup, model)
    sup.cycle(2)
    (prompt,) = prompts(herdr_log)                     # the design was swapped for a pointer, then it passed
    assert f"{DESIGN}\nMake this" not in prompt and "(left out:" in prompt


def test_when_nothing_passes_the_scan_the_agent_keeps_running(sup, model, herdr_log, monkeypatch):
    real = tk.check_secrets
    monkeypatch.setattr(tk, "check_secrets",
                        lambda title, body: ["secret"] if title == "restart prompt" else real(title, body))
    tabbed(sup)
    looped(sup, model)
    sup.cycle(2)
    assert ["tab", "close", "w1:t4"] not in calls(herdr_log) and prompts(herdr_log) == []
    assert sr.load_state()["pending"] == {}


def test_a_moved_branch_drops_the_step_and_stops_nothing(sup, model, herdr_log):
    tabbed(sup)
    looped(sup, model)
    sup.commit("fix")                                  # the agent committed since
    sup.cycle(2)
    assert ["tab", "close", "w1:t4"] not in calls(herdr_log) and prompts(herdr_log) == []
    assert sr.load_state()["pending"] == {}
    assert "SUPERVISOR_CANCELLED" in (tk.fleet_home() / "dispatch.log").read_text()


def test_pending_survives_a_restart_of_the_watch(sup, model, herdr_log):
    tabbed(sup, extra=[{"name": "someone-else", "agent_status": "idle", "tab_id": "w1:t4"}])
    looped(sup, model)
    sup.cycle(2)
    before = json.loads(sr.state_path().read_text())["pending"]
    assert sup.id in before
    assert sr.pending_ids() == {sup.id}


def test_the_restart_is_given_up_after_a_few_cycles(sup, model, herdr_log):
    tabbed(sup, extra=[{"name": "someone-else", "agent_status": "idle", "tab_id": "w1:t4"}])
    looped(sup, model)
    for i in range(2, 2 + sr.RESTART_ATTEMPTS):
        sup.cycle(i)
    assert sr.load_state()["pending"] == {} and prompts(herdr_log) == []
    assert "SUPERVISOR_GAVE_UP" in (tk.fleet_home() / "dispatch.log").read_text()


def test_a_ticket_with_a_pending_restart_is_not_relaunched_as_lost(sup, model, herdr_log):
    sup.no_agent()
    state = sr.load_state()
    state["pending"][sup.id] = {"commit": sup.tip(), "design": DESIGN, "reason": "same flaw", "tries": 0}
    sr.save_state(state)
    assert sup.id in sr.pending_ids()
    tickets = [sup.get()]
    assert lc.lost_agents(tickets, lambda m: None)                       # it would be lost ...
    assert sup.id in dispatch_cli.supervisor_run.pending_ids()           # ... but the dispatcher skips it


def test_relaunch_refuses_a_ticket_whose_agent_is_listed(sup, herdr_log):
    tabbed(sup)
    code, message = lc.relaunch(sup.get(), sup.settings, lambda m: None)
    assert code == 1 and "already running" in message
    assert [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]] == []


def test_pending_entries_are_validated_on_load(env):
    good = {"commit": "a" * 40, "design": DESIGN, "reason": "same flaw", "tries": 0}
    bad = [{**good, "commit": "not-a-commit"}, {**good, "design": "short"}, {**good, "tries": -1},
           {**good, "tries": True}, {**good, "reason": ""}, {**good, "design": "x" * 5000}, "text", None]
    data = {"pending": {"BD-1": good, **{f"BD-{i + 2}": b for i, b in enumerate(bad)}, "nonsense": good}}
    sr.state_path().parent.mkdir(parents=True, exist_ok=True)
    sr.state_path().write_text(json.dumps(data))
    warnings = []
    assert sr.load_state(warnings.append)["pending"] == {"BD-1": good}
    assert len(warnings) == len(bad) + 1


def test_a_loop_reply_without_a_saved_note_starts_nothing(sup, model, herdr_log, monkeypatch):
    real = tk.add_note

    def refuse(tid, text):
        if text.startswith("SUPERVISOR"):
            raise tk.TicketInvalid(["no"])
        return real(tid, text)
    monkeypatch.setattr(tk, "add_note", refuse)
    tabbed(sup)
    looped(sup, model)
    sup.cycle(2)
    assert sr.load_state()["pending"] == {} and prompts(herdr_log) == []
    assert ["tab", "close", "w1:t4"] not in calls(herdr_log)


def test_progress_and_squirrels_never_restart(sup, model, herdr_log):
    tabbed(sup)
    model.say("CLASSIFICATION: PROGRESS\nREASON: different areas\n")
    both_fail(sup)
    sup.cycle(1)
    settle()
    sup.cycle(2)
    assert sr.load_state()["pending"] == {} and prompts(herdr_log) == []
