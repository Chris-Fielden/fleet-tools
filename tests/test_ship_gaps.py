"""BD-99: the gaps found in ship and fleet status. Real git, a fake finish-task, a temporary
FLEET_HOME."""

import json
import time

import pytest

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import notify, prep, status
from fleet import service as sv
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import claim, git, mk, projects  # noqa: F401
from test_ship import shipping  # noqa: F401


@pytest.fixture
def pings(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "notify_quietly",
                        lambda t, event, warn=None, detail=None, log_unconfigured=False:
                        sent.append((t["id"], event, detail)) or True)
    return sent


def _settings(env, **extra):
    path = env / "fleet" / "dispatch.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **extra}))


# --- gap 3: a ship waiting at a prompt ---------------------------------------------------------

def test_a_merge_waiting_at_a_prompt_is_cancelled_and_pinged_once(shipping, env, monkeypatch, capsys, pings):
    repo, t, log = shipping
    _settings(env, ship_prompt_timeout_s=1)
    monkeypatch.setenv("FAKE_SLEEP", "60")
    began = time.monotonic()
    assert ship_main([t["id"]]) == 1
    assert time.monotonic() - began < 30                      # the script's sleep was killed
    err = capsys.readouterr().err
    assert "timed out at a prompt" in err and "printed nothing for 1s" in err
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    assert prep.shipping_ids() == set()                       # the reservation was released
    assert pings == [(t["id"], "needs_human", pings[0][2])]   # one ping, and not a second ship_failed
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_a_slow_script_that_keeps_printing_is_not_timed_out(shipping, env, monkeypatch):
    repo, t, log = shipping
    _settings(env, ship_prompt_timeout_s=2)
    script = repo / "scripts" / "finish-task.sh"
    script.write_text("#!/bin/bash\nfor i in 1 2 3; do echo working; sleep 1; done\n"
                      'git -c user.name=T -c user.email=t@example.com merge -q --no-ff "task/$1" -m "Merge task/$1"\n')
    assert ship_main([t["id"]]) == 0
    assert tk.load_ticket(t["id"])["status"] == "done"


def test_the_timeout_can_be_turned_off(projects, env):
    _settings(env, ship_prompt_timeout_s=0)
    assert dp.load_settings()["ship_prompt_timeout_s"] == 0


# --- gap 4: the log path -----------------------------------------------------------------------

def test_a_bad_id_never_builds_a_path(projects, env, capsys):
    from fleet.ship_cli import _ship_recorded
    assert _ship_recorded("../BD-1") == 1
    assert "unknown ticket ID" in capsys.readouterr().err
    assert not (env / "fleet" / "ship").exists() and not (env / "fleet" / "BD-1.log").exists()


def test_the_log_path_is_printed_at_the_start(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"]]) == 0
    assert f"ship: log: {tk.fleet_home() / 'ship' / (t['id'] + '.log')}" in capsys.readouterr().out


def test_the_log_holds_what_was_printed_and_status_reads_the_result(shipping, monkeypatch, capsys):
    repo, t, log = shipping
    monkeypatch.setenv("FAKE_EXIT", "1")
    assert ship_main([t["id"]]) == 1
    text = (tk.fleet_home() / "ship" / f"{t['id']}.log").read_text()
    assert "refused task/demo" in text
    capsys.readouterr()
    assert ship_main(["--status", t["id"]]) == 1
    assert "was not shipped" in capsys.readouterr().err


# --- gap 6: a stacked ticket delivered by its partner ------------------------------------------

def _stack(repo):
    """task/demo has a commit; task/partner is stacked on it and was merged into main."""
    base = git(repo, "rev-parse", "main").strip()
    git(repo, "checkout", "-q", "-b", "task/partner", "task/demo")
    (repo / "partner.txt").write_text("p")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "partner work")
    git(repo, "checkout", "-q", "main")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "merge", "-q", "--no-ff",
        "task/partner", "-m", "Merge task/partner")
    return base


def test_a_stacked_ticket_whose_partner_merged_is_closed_as_delivered(shipping, env, capsys):
    repo, t, log = shipping
    base = _stack(repo)
    tk.set_status(t["id"], "review", allowed_from=("claimed",), base_commit=base)
    partner = claim(mk(branch="task/partner"))
    tk.set_status(partner["id"], "done", allowed_from=("claimed",))
    assert ship_main([t["id"]]) == 0
    done = tk.load_ticket(t["id"])
    assert done["status"] == "done" and partner["id"] in done["close_note"]
    assert not log.exists()                                    # finish-task never ran
    assert "already delivered" in capsys.readouterr().out


def test_the_terminal_path_closes_a_delivered_ticket_before_any_review_gate(shipping, env, capsys):
    from fleet.ship_cli import _prepare_detached
    repo, t, log = shipping
    base = _stack(repo)
    tk.set_status(t["id"], "review", allowed_from=("claimed",), base_commit=base)
    partner = claim(mk(branch="task/partner"))
    tk.set_status(partner["id"], "done", allowed_from=("claimed",))
    assert _prepare_detached(tk.load_ticket(t["id"])) == 0     # no reviews exist: it must not ask for them
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert prep.shipping_ids() == set()
    assert "already delivered" in capsys.readouterr().out


def test_delivery_is_found_after_finish_task_deleted_the_partners_branch(shipping, env):
    repo, t, log = shipping
    base = _stack(repo)
    git(repo, "branch", "-D", "task/partner")                  # what finish-task does after merging
    tk.set_status(t["id"], "claimed", allowed_from=("claimed",), base_commit=base)
    partner = claim(mk(branch="task/partner"))
    tk.set_status(partner["id"], "done", allowed_from=("claimed",))
    found = dp.delivered_by_partner(repo, tk.load_ticket(t["id"]), tk.list_tickets())
    assert found and found["id"] == partner["id"]


def test_no_new_commits_alone_is_not_delivered(shipping, env):
    """A branch with nothing of its own, and no merged partner, is left to finish-task."""
    repo, t, log = shipping
    git(repo, "branch", "task/empty", "main")
    other = claim(mk(branch="task/empty"))
    tk.set_status(other["id"], "claimed", allowed_from=("claimed",), base_commit=git(repo, "rev-parse", "main").strip())
    assert dp.delivered_by_partner(repo, tk.load_ticket(other["id"]), tk.list_tickets()) is None


def test_a_branch_that_only_caught_up_with_main_is_not_delivered(shipping, env):
    """A branch fast-forwarded onto main before any commit of its own is not 'delivered' by a
    partner that later branched from main."""
    repo, t, log = shipping
    base = git(repo, "rev-parse", "main").strip()
    git(repo, "checkout", "-q", "-b", "task/fresh", "main")
    (repo / "m.txt").write_text("m")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main moved")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "task/fresh")   # main now has the commit; the branch is at main
    git(repo, "checkout", "-q", "-b", "task/partner2", "main")
    (repo / "p2.txt").write_text("p")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "p2")
    git(repo, "checkout", "-q", "main")
    git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "merge", "-q", "--no-ff",
        "task/partner2", "-m", "Merge task/partner2")
    mine = claim(mk(branch="task/fresh"))
    tk.set_status(mine["id"], "claimed", allowed_from=("claimed",), base_commit=base)
    claim(mk(branch="task/partner2"))
    assert dp.delivered_by_partner(repo, tk.load_ticket(mine["id"]), tk.list_tickets()) is None


def test_an_unmerged_partner_does_not_deliver(shipping, env):
    repo, t, log = shipping
    tk.set_status(t["id"], "claimed", allowed_from=("claimed",), base_commit=git(repo, "rev-parse", "main").strip())
    git(repo, "branch", "task/partner", "task/demo")
    claim(mk(branch="task/partner"))
    assert dp.delivered_by_partner(repo, tk.load_ticket(t["id"]), tk.list_tickets()) is None


def test_reconcile_closes_a_delivered_stacked_ticket(shipping, env):
    repo, t, log = shipping
    base = _stack(repo)
    tk.set_status(t["id"], "claimed", allowed_from=("claimed",), base_commit=base)
    partner = claim(mk(branch="task/partner"))
    tk.set_status(partner["id"], "claimed", allowed_from=("claimed",), base_commit=base)
    changed = [n["id"] for n in dp.reconcile(tk.list_tickets(), dp.load_settings())]
    assert set(changed) == {t["id"], partner["id"]}
    assert partner["id"] in tk.load_ticket(t["id"])["close_note"]


# --- gap 7: an agent that cannot be stopped ----------------------------------------------------

def test_a_stuck_agent_sends_one_needs_human_ping(shipping, env, monkeypatch, capsys, pings):
    repo, t, log = shipping
    monkeypatch.setattr(lc, "ensure_workspace", lambda name: "w1")
    monkeypatch.setattr(lc, "quiesce_agent", lambda tid, settings: (False, "tab shared"))
    monkeypatch.setenv("FAKE_MERGE", "1")
    assert ship_main([t["id"]]) == 0                          # merged; cleanup is finish-task's
    from fleet.ship_cli import _stop_agent
    tk_ = tk.load_ticket(t["id"])
    assert _stop_agent(tk_, dp.load_settings()) == (False, "tab shared")
    assert _stop_agent(tk_, dp.load_settings()) == (False, "tab shared")
    assert [p[1] for p in pings] == ["needs_human"]


def test_stopping_an_agent_recreates_the_missing_workspace(shipping, monkeypatch):
    repo, t, log = shipping
    made = []
    monkeypatch.setattr(lc, "ensure_workspace", lambda name: made.append(name) or "w1")
    monkeypatch.setattr(lc, "quiesce_agent", lambda tid, settings: (True, "no agent is running"))
    from fleet.ship_cli import _stop_agent
    assert _stop_agent(t, dp.load_settings())[0] is True
    assert made == [dp.load_settings()["herdr_workspace"]]


# --- gap 5: fleet status and the workspace -----------------------------------------------------

def test_the_status_snapshot_says_when_the_workspace_is_gone(projects, monkeypatch):
    monkeypatch.setattr(sv, "herdr_running", lambda: True)

    def gone(name):
        raise lc.WorkspaceNotFound(f"herdr has no workspace '{name}'")
    monkeypatch.setattr(lc, "find_workspace", gone)
    created = []
    monkeypatch.setattr(lc, "ensure_workspace", lambda name: created.append(name))
    assert status._herdr() == {"server": True, "workspace": False}
    assert created == []                                       # the read-only view creates nothing
    monkeypatch.setattr(lc, "find_workspace", lambda name: "w1")
    assert status._herdr() == {"server": True, "workspace": True}


def test_fleet_status_row_fails_when_the_workspace_is_gone(projects, monkeypatch):
    monkeypatch.setattr(sv, "herdr_running", lambda: True)
    monkeypatch.setattr(sv, "dispatcher_running", lambda: True)

    def gone(name):
        raise lc.WorkspaceNotFound(f"herdr has no workspace '{name}'")
    monkeypatch.setattr(lc, "find_workspace", gone)
    rows = {r[1]: r for r in sv.status_rows()}
    assert rows["herdr"][0] == "ok" and rows["workspace"][0] == "fail"


# --- BD-106: timeouts on every step, and a reservation that cannot block forever ---------------

def test_a_merge_that_keeps_talking_but_never_ends_is_stopped_and_pinged(shipping, env, monkeypatch, capsys, pings):
    repo, t, log = shipping
    _settings(env, ship_prompt_timeout_s=0, ship_step_timeout_s=2)
    script = repo / "scripts" / "finish-task.sh"
    script.write_text("#!/bin/bash\nwhile true; do echo working; sleep 0.2; done\n")
    began = time.monotonic()
    assert ship_main([t["id"]]) == 1
    assert time.monotonic() - began < 30
    assert "ran longer than 2s" in capsys.readouterr().err
    assert prep.shipping_ids() == set()
    assert [p[1] for p in pings] == ["needs_human"]


def test_a_hung_gate_comes_back_as_a_failed_run_and_pings_once(projects, env, monkeypatch, pings):
    import subprocess

    from fleet import ship_cli
    _settings(env, ship_step_timeout_s=7)
    t = mk()
    monkeypatch.setattr(ship_cli, "_ship_ticket", t["id"])
    seen = []

    def hang(argv, timeout, **kw):
        seen.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(ship_cli, "_run_group", hang)
    done = ship_cli._gate(projects / "x", projects / "x", "task/x")
    assert seen == [7] and done.returncode == 124 and "timed out after 7s" in done.stderr
    assert [p[1] for p in pings] == ["needs_human"]


def test_a_timed_out_step_kills_its_grandchildren(tmp_path):
    import os
    import subprocess

    from fleet import ship_cli
    pidfile = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        ship_cli._run_group(["bash", "-c", f"sleep 300 & echo $! > {pidfile}; wait"], 1,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild is still running")


def test_step_timeouts_are_settings(projects, env):
    _settings(env, ship_step_timeout_s=0, ship_reservation_max_age_s=30)
    s = dp.load_settings()
    assert s["ship_step_timeout_s"] == 0 and s["ship_reservation_max_age_s"] == 30
    _settings(env, ship_step_timeout_s=-1)
    with pytest.raises(Exception):
        dp.load_settings()


def _hold(ticket_id, **rec):
    with prep._state() as st:
        st["shipping"] = [{"id": ticket_id, **rec}]


def test_a_live_but_unrelated_pid_does_not_hold_a_reservation(projects, env):
    import os
    now = time.time()
    _hold("BD-1", pid=os.getpid(), cmd="some other command entirely", at=now)   # a reused pid
    assert prep.shipping_ids() == set()
    _hold("BD-1", pid=os.getpid(), cmd=prep._cmdline(os.getpid()), at=now)      # the same command
    assert prep.shipping_ids() == {"BD-1"}


def test_an_old_reservation_expires_and_a_fresh_one_is_honoured(projects, env):
    import os
    me = prep._cmdline(os.getpid())
    _settings(env, ship_reservation_max_age_s=60)
    _hold("BD-1", pid=os.getpid(), cmd=me, at=time.time() - 3600)
    assert prep.shipping_ids() == set()
    _hold("BD-1", pid=os.getppid(), cmd=prep._cmdline(os.getppid()), at=time.time() - 10)
    assert prep.shipping_ids() == {"BD-1"}
    assert prep.reserve_shipping("BD-1") == "BD-1 is already being shipped"
