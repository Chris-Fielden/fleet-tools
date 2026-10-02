"""Agents investigate ideas: `ticket note`, follow-up build tickets, the
dispatcher's settings and rules for investigations, and launching and
finishing them with the fake herdr."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import _fake
from fleet import dispatch as dp
from fleet import kaneo
from fleet import launch as lc
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main
from fleet.ticket_cli import main as ticket_main
from test_dispatch import calls, herdr_log, mk, projects, real_repo  # noqa: F401  (fixtures)


def idea(by="human", monkeypatch=None, **over):
    """An investigate ticket. `by` is who files it; the ticket store reads that
    from the environment."""
    if by != "human":
        monkeypatch.setenv("CLAUDECODE", "1")
    try:
        return mk(type="investigate", repo="ideas", branch=f"task/idea-{len(tk.list_tickets())}",
                  title="An idea", body="What if?", **over)
    finally:
        monkeypatch and monkeypatch.delenv("CLAUDECODE", raising=False)


def settings(**over):
    s = dp.load_settings()
    s.update(over)
    return s


def set_created(t, when):
    path = tk._ticket_path(t["id"])
    data = json.loads(path.read_text())
    data["created_at"] = when
    path.write_text(json.dumps(data))


# --- ticket note ---------------------------------------------------------------

def test_note_is_appended_and_shown(capsys):
    t = mk()
    assert ticket_main(["note", t["id"], "first finding"]) == 0
    assert ticket_main(["note", t["id"], "second finding"]) == 0
    got = tk.load_ticket(t["id"])
    assert [n["text"] for n in got["notes"]] == ["first finding", "second finding"]
    assert got["notes"][0]["by"] == "human"
    capsys.readouterr()
    assert ticket_main(["show", t["id"]]) == 0
    out = capsys.readouterr().out
    assert "first finding" in out and "second finding" in out


def test_note_records_who_wrote_it(monkeypatch):
    t = mk()
    monkeypatch.setenv("CODEX_SANDBOX", "1")
    assert ticket_main(["note", t["id"], "from codex"]) == 0
    assert tk.load_ticket(t["id"])["notes"][0]["by"] == "codex"


def test_note_length_limit(capsys):
    t = mk()
    assert ticket_main(["note", t["id"], "x" * 4001]) == 2
    assert "4000" in capsys.readouterr().err
    assert ticket_main(["note", t["id"], ""]) == 2
    assert "notes" not in tk.load_ticket(t["id"])
    assert ticket_main(["note", t["id"], "x" * 4000]) == 0


def test_note_gitleaks_checked(env, monkeypatch, capsys):
    t = mk()
    monkeypatch.setenv("FLEET_GITLEAKS", str(_fake(env / "gl-leak", 1)))
    assert ticket_main(["note", t["id"], "secret"]) == 2
    assert "secret" in capsys.readouterr().err
    assert "notes" not in tk.load_ticket(t["id"])


def test_note_unknown_ticket_and_bad_id(capsys):
    assert ticket_main(["note", "BD-99", "x"]) == 1
    assert ticket_main(["note", "nonsense", "x"]) == 2


def test_note_count_limit(capsys):
    t = mk()
    for i in range(tk.NOTES_LIMIT):
        tk.add_note(t["id"], f"n{i}")
    assert ticket_main(["note", t["id"], "one too many"]) == 1
    assert len(tk.load_ticket(t["id"])["notes"]) == tk.NOTES_LIMIT


@pytest.mark.parametrize("bad", [[], "text", [{"at": "x", "by": "human", "text": "t"}],
                                 [{"at": "2026-01-01T00:00:00Z", "by": "bob", "text": "t"}],
                                 [{"at": "2026-01-01T00:00:00Z", "by": "human", "text": ""}],
                                 [{"at": "2026-01-01T00:00:00Z", "by": "human", "text": "t", "x": 1}]])
def test_stored_notes_validated(bad):
    t = mk()
    path = tk._ticket_path(t["id"])
    data = json.loads(path.read_text())
    data["notes"] = bad
    path.write_text(json.dumps(data))
    with pytest.raises(tk.TicketError, match="note"):
        tk.load_ticket(t["id"])


def test_note_is_on_the_kaneo_card():
    t = tk.add_note(mk()["id"], "found it")
    assert "found it" in kaneo.card_description(t)
    assert "Note" not in kaneo.card_description(mk(branch="task/other"))


# --- follow-up build tickets ---------------------------------------------------

def test_followup_build_waits_for_approval(monkeypatch):
    parent = idea()
    monkeypatch.setenv("CLAUDECODE", "1")
    child = mk(parent=parent["id"], branch="task/follow-up")
    assert child["parent"] == parent["id"] and child["status"] == "proposed"


def test_build_parent_must_be_an_investigation():
    other = mk()
    with pytest.raises(tk.TicketInvalid, match="investigate"):
        mk(parent=other["id"], branch="task/follow-up")


def test_plain_build_still_opens():
    assert mk()["status"] == "open"


# --- settings ------------------------------------------------------------------

def test_investigation_settings_defaults(projects):
    s = dp.load_settings()
    assert s["auto_investigate"] is True and s["max_investigations_per_day"] == 3


def test_investigation_settings_override(env, projects):
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "auto_investigate": False, "max_investigations_per_day": 0}))
    s = dp.load_settings()
    assert s["auto_investigate"] is False and s["max_investigations_per_day"] == 0


@pytest.mark.parametrize("given", [{"auto_investigate": "yes"}, {"auto_investigate": 1},
                                   {"max_investigations_per_day": -1},
                                   {"max_investigations_per_day": True},
                                   {"max_investigations_per_day": "3"}])
def test_investigation_settings_invalid(env, projects, given):
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"projects_dir": str(projects), **given}))
    with pytest.raises(dp.SettingsError):
        dp.load_settings()


# --- the rules -----------------------------------------------------------------

def test_order_human_first_then_oldest(projects, monkeypatch):
    a = idea("claude", monkeypatch)
    b = idea("human", monkeypatch)
    c = idea("human", monkeypatch)
    set_created(a, "2026-01-01T00:00:00Z")   # oldest, but an agent filed it
    set_created(b, "2026-02-01T00:00:00Z")
    set_created(c, "2026-01-15T00:00:00Z")   # human, older than b
    ds = dp.decide_investigations(tk.list_tickets(), settings())
    assert [d.ticket["id"] for d in ds] == [c["id"], b["id"], a["id"]]
    assert [d.eligible for d in ds] == [True, False, False]
    assert ds[1].skip == "only one investigation is started per run"


def test_auto_investigate_off_skips_proposed_but_not_approved(projects):
    waiting = idea()
    approved = idea()
    tk.approve_ticket(approved["id"])
    ds = {d.ticket["id"]: d for d in dp.decide_investigations(tk.list_tickets(), settings(auto_investigate=False))}
    assert "auto_investigate is off" in ds[waiting["id"]].skip
    assert ds[approved["id"]].eligible


def test_daily_limit(projects):
    ts = [idea() for _ in range(3)]
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    tk.set_status(ts[0]["id"], "done", allowed_from=("proposed",), started_at=stamp)
    s = settings(max_investigations_per_day=1)
    ds = dp.decide_investigations(tk.list_tickets(), s)
    assert all("daily limit" in d.skip for d in ds)
    # yesterday's starts don't count
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tk.set_status(ts[1]["id"], "done", allowed_from=("proposed",), started_at=yesterday)
    assert dp.started_today(tk.list_tickets(), now) == 1


def test_limit_zero_stops_everything(projects):
    idea()
    (d,) = dp.decide_investigations(tk.list_tickets(), settings(max_investigations_per_day=0))
    assert not d.eligible and "daily limit" in d.skip


def test_investigations_do_not_use_build_capacity(projects):
    idea()
    busy = mk(branch="task/busy")
    tk.set_status(busy["id"], "claimed", allowed_from=("open",), assignee="claude")
    (d,) = dp.decide_investigations(tk.list_tickets(), settings())
    assert d.eligible
    mk(branch="task/next")
    # and a claimed investigation doesn't block a build
    inv = idea()
    tk.set_status(inv["id"], "claimed", allowed_from=("proposed",), assignee="claude")
    tk.set_status(busy["id"], "done", allowed_from=("claimed",))
    build = [d for d in dp.decide(tk.list_tickets(), settings()) if d.ticket["branch"] == "task/next"][0]
    assert "at capacity" not in (build.skip or "")


def test_decide_ignores_investigate_tickets(projects):
    t = idea()
    tk.approve_ticket(t["id"])
    assert dp.decide(tk.list_tickets(), settings()) == []


def test_reconcile_leaves_investigations_alone(projects):
    t = idea()
    tk.set_status(t["id"], "claimed", allowed_from=("proposed",), assignee="claude")
    assert list(dp.reconcile(tk.list_tickets(), settings())) == []
    assert "RECONCILE_SKIP" not in (dp.log_path().read_text() if dp.log_path().exists() else "")


def test_dry_run_lists_investigations_and_changes_nothing(projects, capsys):
    t = idea()
    before = tk.load_ticket(t["id"])
    assert dispatch_main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"{t['id']}  would launch claude (sonnet) to investigate (read-only)" in out
    assert tk.load_ticket(t["id"]) == before


# --- launching and finishing (fake herdr) --------------------------------------

def test_once_launches_a_proposed_investigation(projects, herdr_log, env, capsys):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(t["id"])
    assert got["status"] == "claimed" and got["assignee"] == "claude" and "started_at" in got
    folder = env / "fleet" / "research" / t["id"]
    c = calls(herdr_log)
    assert ["tab", "create", "--workspace", "w1", "--cwd", str(folder),
            "--label", t["id"], "--no-focus"] in c
    prompt = next(x for x in c if x[:2] == ["agent", "prompt"])
    assert "ticket note" in prompt[3] and "--parent" in prompt[3] and "no commits" in prompt[3]
    flags = [a for call in c for a in call if a.startswith("-")]
    assert set(flags) <= {"--workspace", "--cwd", "--label", "--no-focus", "--kind",
                          "--pane", "--", "--model"}
    assert "to investigate" in capsys.readouterr().out
    assert not (projects / "ideas").exists()  # no task folder, no repo


def test_once_launches_one_investigation_and_one_build(projects, herdr_log):
    real_repo(projects)
    a, b = idea(), idea()
    build = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 0
    status = {t["id"]: t["status"] for t in tk.list_tickets()}
    assert status[build["id"]] == "claimed"
    assert sorted([status[a["id"]], status[b["id"]]]) == ["claimed", "proposed"]


def test_once_respects_the_daily_limit(projects, herdr_log, env):
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "max_investigations_per_day": 1}))
    a, b = idea(), idea()
    assert dispatch_main(["--once"]) == 0
    assert dispatch_main(["--once"]) == 0
    status = sorted(t["status"] for t in tk.list_tickets())
    assert status == ["claimed", "proposed"]


def test_once_launch_failure_puts_ticket_back_and_does_not_count(projects, herdr_log, monkeypatch):
    t = idea()
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 1
    got = tk.load_ticket(t["id"])
    assert got["status"] == "proposed" and got["assignee"] is None and "started_at" not in got
    assert [x for x in calls(herdr_log) if x[:2] == ["tab", "close"]] == [["tab", "close", "w1:t7"]]
    assert "LAUNCH_FAILED" in dp.log_path().read_text()


def test_approved_investigation_goes_back_to_open_on_failure(projects, herdr_log, monkeypatch):
    t = idea()
    tk.approve_ticket(t["id"])
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_finished_investigation_with_notes_is_done(projects, herdr_log, monkeypatch):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    tk.add_note(t["id"], "the answer")
    assert dispatch_main(["--once"]) == 0       # still working: nothing changes
    assert tk.load_ticket(t["id"])["status"] == "claimed"
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(t["id"])
    assert got["status"] == "done" and "close_note" not in got
    assert "INVESTIGATION_DONE" in dp.log_path().read_text()


def test_finished_investigation_without_notes_fails(projects, herdr_log, monkeypatch):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 0
    got = tk.load_ticket(t["id"])
    assert got["status"] == "failed" and "without recording" in got["close_note"]


def test_finish_leaves_ticket_alone_when_herdr_fails(projects, herdr_log, monkeypatch, capsys):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    monkeypatch.setenv("FLEET_HERDR", str(_fake(projects.parent / "broken-herdr", 1)))
    warnings = []
    assert list(lc.finish_investigations(tk.list_tickets(), warnings.append)) == []
    assert warnings and tk.load_ticket(t["id"])["status"] == "claimed"


def test_finish_does_not_touch_other_tickets(projects, herdr_log, monkeypatch):
    build = mk(branch="task/job")
    tk.set_status(build["id"], "claimed", allowed_from=("open",), assignee="claude")
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert list(lc.finish_investigations(tk.list_tickets(), lambda m: None)) == []
    assert tk.load_ticket(build["id"])["status"] == "claimed"


def test_no_log_line_has_ticket_text(projects, herdr_log):
    idea()
    assert dispatch_main(["--once"]) == 0
    assert "What if?" not in dp.log_path().read_text()
