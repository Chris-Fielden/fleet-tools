"""EM-25: research agents get their rules from the launch command (no trust dialog), and a
launch herdr reports as agent_not_ready is held, pinged once and never retried."""

import json
import shlex

import pytest

from fleet import dispatch as dp
from fleet import launch as lc
from fleet import notify
from fleet import permissions as pm
from fleet import startup_block as sb
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main
from fleet.usage_cli import main as fleet_main
from test_dispatch import calls, herdr_log, mk, projects  # noqa: F401  (fixtures)


@pytest.fixture
def pings(monkeypatch):
    seen = []

    def fake(ticket, event, warn=None, detail=None):
        seen.append((ticket["id"], event, detail))
        return True
    monkeypatch.setattr(notify, "notify_quietly", fake)
    return seen


def idea(**over):
    return mk(type="investigate", repo="ideas", branch=f"task/idea-{len(tk.list_tickets())}",
              title="An idea", body="What if?", **over)


def starts(log):
    return [c for c in calls(log) if c[:2] == ["agent", "start"]]


def test_launch_command_carries_the_rules_file(projects, herdr_log):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    (start,) = starts(herdr_log)
    assert start[start.index("--") + 1] == "--settings"
    path = start[start.index("--settings") + 1]
    assert shlex.quote(path) == path and path.endswith("/research-settings.json")
    assert path.startswith(str(tk.fleet_home()))
    assert json.load(open(path)) == {"permissions": pm.research_permissions()}
    folder_file = json.load(open(lc.research_folder(t["id"], dp.load_settings()) / ".claude" / "settings.json"))
    assert "permissions" not in folder_file


def test_build_launches_do_not_get_the_research_file(projects, herdr_log, monkeypatch):
    d = dp.Decision(ticket={"id": "EM-9"}, agent="claude")
    monkeypatch.setattr(lc, "governor", type("G", (), {"refusal": staticmethod(lambda *a: None)}))
    lc.start_agent(d, projects, "w1", lambda m: None)
    assert "--settings" not in starts(herdr_log)[0]


def test_not_ready_holds_pings_once_and_never_retries(projects, herdr_log, monkeypatch, pings, capsys):
    monkeypatch.setenv("FAKE_HERDR_NOT_READY", "1")
    first, second = idea(), idea()
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(first["id"])["status"] == "proposed"   # put back, as for any failed launch
    assert list(sb.load()) == [first["id"]] and sb.research_hold() == first["id"]
    assert len(starts(herdr_log)) == 1
    assert [(i, e) for i, e, _ in pings] == [(first["id"], "needs_human")]
    assert first["id"] in pings[0][2] and str(tk.load_ticket(first["id"])["id"]) in pings[0][2]
    assert "fleet-research" in pings[0][2] or "research" in pings[0][2]
    capsys.readouterr()
    used = dp.launches_today(dp.local_day(dp.datetime.now(dp.timezone.utc), dp.load_settings()["timezone"]))
    for _ in range(3):   # later cycles: no start, no second ping, no launch spent, either ticket
        dispatch_main(["--once"])
    out = capsys.readouterr().out
    assert "startup blocked" in out
    assert len(starts(herdr_log)) == 1 and len(pings) == 1
    assert tk.load_ticket(second["id"])["status"] == "proposed"
    assert dp.launches_today(dp.local_day(dp.datetime.now(dp.timezone.utc),
                                          dp.load_settings()["timezone"])) == used


def test_unblock_clears_the_hold_and_lets_a_later_failure_ping_again(projects, herdr_log, monkeypatch, pings):
    monkeypatch.setenv("FAKE_HERDR_NOT_READY", "1")
    t = idea()
    dispatch_main(["--once"])
    assert fleet_main(["unblock", t["id"]]) == 0
    assert sb.load() == {} and sb.research_hold() is None
    dispatch_main(["--once"])   # fails again: a new hold, a new ping
    assert len(starts(herdr_log)) == 2 and len(pings) == 2
    monkeypatch.delenv("FAKE_HERDR_NOT_READY")
    assert fleet_main(["unblock", t["id"]]) == 0
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_unblock_of_an_unknown_ticket_is_an_error(projects):
    assert fleet_main(["unblock", "EM-404"]) == 1
