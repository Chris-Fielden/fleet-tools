"""`ticket priority`, and dispatch --watch reading Urgent drags back from the board (fake Kaneo)."""
import json

import pytest

from conftest import run
from fake_kaneo import FakeKaneo  # noqa: F401
from test_kaneo import edit_ticket, kaneo, new_ticket, only_task  # noqa: F401

from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import kaneo as kn
from fleet import ticket as tk

from test_dispatch import herdr_log, projects  # noqa: F401


def slug_of(kaneo, name: str) -> str:
    return next(c["slug"] for cols in kaneo.columns.values() for c in cols if c["name"] == name)


def drag(kaneo, name: str) -> None:
    only_task(kaneo)["status"] = slug_of(kaneo, name)


def pull() -> tuple[list[str], list[str]]:
    out, warnings = [], []
    dispatch_cli._pull_board_priorities(out.append, warnings.append)
    return out, warnings


def priority(tid: str) -> str:
    return tk.load_ticket(tid).get("priority", "normal")


# --- ticket priority ----------------------------------------------------------

def test_priority_command_accepts_any_case_id(kaneo):
    tid = new_ticket()
    for typed in (tid.lower(), tid.swapcase(), f" {tid.lower()} "):
        r = run("priority", typed, "urgent")
        assert r.returncode == 0, r.stderr
        assert priority(tid) == "urgent"
        assert run("priority", typed, "normal").returncode == 0
        assert priority(tid) == "normal"


def test_priority_command_sets_it_keeps_the_id_and_moves_the_card(kaneo):
    tid = new_ticket()
    r = run("priority", tid, "urgent")
    assert r.returncode == 0, r.stderr
    t = tk.load_ticket(tid)
    assert t["id"] == tid and t["priority"] == "urgent" and tid.startswith("BD-")
    task = only_task(kaneo)
    assert task["status"] == slug_of(kaneo, "Urgent") and task["priority"] == "urgent"
    assert run("priority", tid, "normal").returncode == 0
    assert priority(tid) == "normal"
    assert only_task(kaneo)["status"] == slug_of(kaneo, "To Do")


def test_priority_command_works_without_kaneo():
    tid = new_ticket()
    assert run("priority", tid, "urgent").returncode == 0
    assert priority(tid) == "urgent"


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_priority_command_refused_for_agents(var):
    tid = new_ticket()
    r = run("priority", tid, "urgent", extra_env={var: "1"})
    assert r.returncode == 1 and "human" in r.stderr
    assert priority(tid) == "normal"


def test_priority_command_refuses_bad_input_and_finished_tickets():
    tid = new_ticket()
    assert run("priority", tid, "high").returncode == 2
    assert run("priority", "bogus", "urgent").returncode == 2
    assert run("priority", "T-20250101-999", "urgent").returncode == 1
    edit_ticket(tid, status="done")
    r = run("priority", tid, "urgent")
    assert r.returncode == 1 and "done" in r.stderr


# --- reading the board ---------------------------------------------------------

def test_a_card_dragged_into_urgent_makes_the_ticket_urgent(kaneo):
    tid = new_ticket()
    drag(kaneo, "Urgent")
    out, warnings = pull()
    assert out == [f"{tid}  priority urgent (moved on the board)"] and warnings == []
    assert priority(tid) == "urgent"
    assert tk.load_ticket(tid)["id"] == tid
    task = only_task(kaneo)   # mirrored right away: priority on the card, still in Urgent
    assert task["priority"] == "urgent" and task["status"] == slug_of(kaneo, "Urgent")
    assert pull() == ([], [])   # and not again


def test_a_card_dragged_out_of_urgent_makes_the_ticket_normal(kaneo):
    tid = new_ticket(priority="urgent")
    assert only_task(kaneo)["status"] == slug_of(kaneo, "Urgent")
    drag(kaneo, "To Do")
    out, _ = pull()
    assert out == [f"{tid}  priority normal (moved on the board)"]
    assert priority(tid) == "normal"
    task = only_task(kaneo)
    assert task["priority"] == "no-priority" and task["status"] == slug_of(kaneo, "To Do")


def test_an_untouched_board_changes_nothing(kaneo):
    tid = new_ticket()
    urgent = new_ticket(branch="task/other", title="Rotate the signing keys", priority="urgent")
    assert pull() == ([], [])
    assert priority(tid) == "normal" and priority(urgent) == "urgent"


def test_a_failed_mirror_is_not_mistaken_for_a_drag(kaneo):
    tid = new_ticket()
    kaneo.fail_all = True
    assert run("priority", tid, "urgent").returncode == 0   # saved, but the card is behind
    kaneo.fail_all = False
    assert pull() == ([], [])
    assert priority(tid) == "urgent"
    run("sync")
    assert only_task(kaneo)["status"] == slug_of(kaneo, "Urgent")


def test_a_ticket_made_normal_while_kaneo_is_down_stays_normal(kaneo):
    tid = new_ticket(priority="urgent")
    kaneo.fail_all = True
    assert run("priority", tid, "normal").returncode == 0
    kaneo.fail_all = False
    assert pull() == ([], [])   # the card still sits in Urgent, where the sync left it
    assert priority(tid) == "normal"


def test_only_proposed_and_open_tickets_are_read(kaneo):
    tid = new_ticket()
    edit_ticket(tid, status="claimed", assignee="claude")
    drag(kaneo, "Urgent")
    assert pull() == ([], [])
    assert priority(tid) == "normal"


def test_a_deleted_card_is_skipped(kaneo):
    new_ticket()
    kaneo.tasks.clear()
    assert pull() == ([], [])


def test_without_kaneo_it_is_silent(env):
    new_ticket()
    assert pull() == ([], [])


def test_kaneo_trouble_is_only_a_warning(kaneo):
    tid = new_ticket()
    kaneo.fail_all = True
    out, warnings = pull()
    assert out == [] and len(warnings) == 1 and "board priorities not read" in warnings[0]
    assert "fake-key" not in warnings[0]
    assert priority(tid) == "normal"


def test_the_board_never_gets_a_write_from_reading(kaneo):
    new_ticket()
    drag(kaneo, "Urgent")
    before = len(kaneo.writes)
    kn_moves = list(kn.pull_priorities(tk.list_tickets()))
    assert len(kn_moves) == 1 and len(kaneo.writes) == before


# --- the watch cycle -----------------------------------------------------------

def test_watch_cycle_reads_the_board_before_it_mirrors_over_the_drag(kaneo, projects, herdr_log):
    tid = new_ticket()
    drag(kaneo, "Urgent")
    lines = []
    dispatch_cli._cycle(dp.load_settings(), lines.append, lambda m: None,
                        checks=lambda emit, warn, skip: None)
    assert f"{tid}  priority urgent (moved on the board)" in lines
    assert priority(tid) == "urgent"
    assert only_task(kaneo)["status"] == slug_of(kaneo, "Urgent")


def test_one_shot_runs_do_not_read_the_board(kaneo, projects, herdr_log):
    tid = new_ticket()
    drag(kaneo, "Urgent")
    dispatch_cli._cycle(dp.load_settings(), lambda m: None, lambda m: None)
    assert priority(tid) == "normal"


def test_cards_made_before_placements_were_kept_get_a_baseline(kaneo):
    tid = new_ticket(priority="urgent")
    kn.placed_path().unlink()   # an install from before the sync kept placements
    assert pull() == ([], [])   # the card is where the sync put it: remembered
    assert json.loads(kn.placed_path().read_text()) == {tid: slug_of(kaneo, "Urgent")}
    drag(kaneo, "To Do")
    out, _ = pull()
    assert out == [f"{tid}  priority normal (moved on the board)"]
    assert priority(tid) == "normal"
