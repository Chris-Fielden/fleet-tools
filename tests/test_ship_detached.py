"""BD-57: a ship asks its questions on the terminal, then checks, merges and pushes as a detached
background job that survives the terminal closing. Real git, a temporary FLEET_HOME."""

import os
import pty
import signal
import subprocess
import sys
import time

import pytest

from conftest import ROOT
from fleet import dispatch as dp
from fleet import notify
from fleet import ship_cli
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main

from test_dispatch import projects  # noqa: F401
from test_risk import commit_in, repo, run_review, task  # noqa: F401
from test_ship import answer, real_task  # noqa: F401


def ship_on_a_pty(ticket_id, *typed):
    """Run `bin/ship ID` on a pseudo-terminal, type the answers; returns (process, master fd)."""
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "ship"), ticket_id], stdin=slave, stdout=slave,
                            stderr=slave, start_new_session=True, env=dict(os.environ))
    os.close(slave)
    time.sleep(1)
    for line in typed:
        os.write(master, line.encode() + b"\n")
    return proc, master


def wait_for(cond, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def reviewed(real_task, env, monkeypatch):  # noqa: F811
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    r = run_review(wt, env / "bin")
    assert r.returncode == 0, r.stdout + r.stderr
    monkeypatch.setenv("FAKE_SLEEP", "3")   # the fake merge script takes a while
    return wt, t


def test_closing_the_terminal_mid_ship_still_merges(reviewed, env):
    wt, t = reviewed
    proc, master = ship_on_a_pty(t["id"], "y")
    assert wait_for(lambda: (env / "finish.log").exists()), "the merge script never started"
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    os.killpg(proc.pid, signal.SIGKILL)   # the terminal and ship itself are gone
    proc.wait()
    os.close(master)
    assert wait_for(lambda: tk.load_ticket(t["id"])["status"] == "done"), "the detached job did not finish"
    assert ship_cli._status(t["id"]) == 0
    assert "merged and marked done" in (env / "fleet" / "ship" / f"{t['id']}.log").read_text()


def test_a_second_ship_of_the_same_ticket_is_refused(reviewed, env, monkeypatch, capsys):
    wt, t = reviewed
    proc, master = ship_on_a_pty(t["id"], "y")
    assert wait_for(lambda: (env / "finish.log").exists())
    asked = answer(monkeypatch, "y")
    assert ship_main([t["id"]]) == 1
    assert "already being shipped" in capsys.readouterr().err
    assert asked == []
    assert ship_cli._status(t["id"]) == 0
    assert "still shipping" in capsys.readouterr().out
    assert proc.wait(timeout=30) == 0
    os.close(master)
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert len((env / "finish.log").read_text().splitlines()) == 1


def test_a_wrong_ticket_id_at_high_risk_cancels(real_task, env, monkeypatch, capsys):  # noqa: F811
    wt, t = real_task
    commit_in(wt, "k8s/app.yaml")
    answer(monkeypatch, "not-it")
    assert ship_main([t["id"]]) == 1
    assert "cancelled; nothing changed" in capsys.readouterr().err
    assert not (env / "finish.log").exists()


def test_the_job_pings_once_with_the_result(real_task, monkeypatch):  # noqa: F811
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    tip = dp._git(wt.parent.parent, "rev-parse", "task/demo").stdout.strip()
    sent = []
    monkeypatch.setattr(notify, "notify_quietly", lambda ticket, event, **kw: sent.append((event, kw)))
    assert ship_cli._run_job(t["id"], tip, "y") == 0
    assert [e for e, _ in sent] == ["shipped"]


def test_the_job_refuses_a_branch_that_moved_and_pings_why(real_task, monkeypatch, env):  # noqa: F811
    wt, t = real_task
    commit_in(wt, "fleet/x.py")
    sent = []
    monkeypatch.setattr(notify, "notify_quietly", lambda ticket, event, **kw: sent.append((event, kw)))
    assert ship_cli._run_job(t["id"], "0" * 40, "y") == 1
    assert [e for e, _ in sent] == ["ship_failed"]
    assert "not the 00000000 that was confirmed" in sent[0][1]["detail"]
    assert not (env / "finish.log").exists()
    assert ship_cli._status(t["id"]) == 1


def test_status_accepts_a_legacy_ticket_id(real_task, capsys):  # noqa: F811
    assert ship_cli._status("T-20261004-001") == 1
    assert "no ship of T-20261004-001" in capsys.readouterr().err


def test_status_of_a_ship_never_run(real_task, capsys):  # noqa: F811
    assert ship_cli._status("BD-9") == 1
    assert "no ship of BD-9" in capsys.readouterr().err
