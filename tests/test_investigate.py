"""Agents investigate ideas: `ticket note`, follow-up build tickets, the
dispatcher's settings and rules for investigations, and launching and
finishing them with the fake herdr."""

import json
from pathlib import Path
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
    folder = env / "home" / "fleet-research" / t["id"]
    c = calls(herdr_log)
    assert ["tab", "create", "--workspace", "w1", "--cwd", str(folder),
            "--label", t["id"], "--no-focus"] in c
    prompt = next(x for x in c if x[:2] == ["agent", "prompt"])
    assert "ticket note" in prompt[3] and "--parent" in prompt[3] and "no commits" in prompt[3]
    assert str(projects) in prompt[3]
    flags = [a for call in c for a in call if a.startswith("-")]
    assert set(flags) <= {"--workspace", "--cwd", "--label", "--no-focus", "--kind",
                          "--pane", "--", "--model", "--settings"}
    assert "to investigate" in capsys.readouterr().out
    assert not (projects / "ideas").exists()  # no task folder, no repo


def test_once_launches_one_investigation_and_one_build(projects, herdr_log):
    real_repo(projects)
    a, b = idea(), idea()
    build = mk(branch="task/job")
    assert dispatch_main(["--once"]) == 0
    status = {t["id"]: t["status"] for t in tk.list_tickets()}
    assert status[build["id"]] == "claimed"
    assert sorted([status[a["id"]], status[b["id"]]]) == ["claimed", "open"]


def test_once_respects_the_daily_limit(projects, herdr_log, env):
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "max_investigations_per_day": 1}))
    a, b = idea(), idea()
    assert dispatch_main(["--once"]) == 0
    assert dispatch_main(["--once"]) == 0
    status = sorted(t["status"] for t in tk.list_tickets())
    assert status == ["claimed", "open"]


def test_once_launch_failure_puts_ticket_back_and_does_not_count(projects, herdr_log, monkeypatch):
    t = idea()
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    assert dispatch_main(["--once"]) == 1
    got = tk.load_ticket(t["id"])
    assert got["status"] == "open" and got["assignee"] is None and "started_at" not in got
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


def test_log_failure_during_research_launch_rolls_back(projects, herdr_log, monkeypatch):
    t = idea()

    def boom(kind, message):
        if kind == "CLAIMED":
            raise OSError("disk full")
    monkeypatch.setattr("fleet.launch.dp.log", boom)
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not herdr_log.exists()


def test_finish_ignores_an_agent_with_unreadable_state(projects, herdr_log, monkeypatch):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    monkeypatch.setattr("fleet.launch.agent_listed", lambda name, timeout=120: (True, None))
    assert list(lc.finish_investigations(tk.list_tickets(), lambda m: None)) == []
    assert tk.load_ticket(t["id"])["status"] == "claimed"


def test_dry_run_log_has_no_worktree_for_investigations(projects):
    idea()
    assert dispatch_main(["--dry-run"]) == 0
    assert "worktree" not in dp.log_path().read_text() and ".worktrees" not in dp.log_path().read_text()


def test_finish_uses_notes_saved_after_the_snapshot(projects, herdr_log, monkeypatch):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    stale = tk.list_tickets()          # no notes yet
    tk.add_note(t["id"], "saved just now")
    monkeypatch.setenv("FAKE_HERDR_STATE", "idle")
    (done,) = list(lc.finish_investigations(stale, lambda m: None))
    assert done["status"] == "done"


def test_finish_ignores_an_unexpected_agent_list(projects, herdr_log, monkeypatch, env):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    odd = env / "odd-herdr"
    odd.write_text("#!/bin/sh\necho '{\"result\": {\"oops\": 1}}'\n")
    odd.chmod(0o755)
    monkeypatch.setenv("FLEET_HERDR", str(odd))
    warnings = []
    assert list(lc.finish_investigations(tk.list_tickets(), warnings.append)) == []
    assert warnings and tk.load_ticket(t["id"])["status"] == "claimed"


def test_started_at_is_saved_with_the_claim(projects, herdr_log, monkeypatch):
    t = idea()
    seen = {}

    def fail_after_claim(*a, **k):
        seen["started"] = "started_at" in tk.load_ticket(t["id"])
        raise lc.AgentMayBeRunning("tab stuck")
    monkeypatch.setattr("fleet.launch.start_agent", fail_after_claim)
    assert dispatch_main(["--once"]) == 1
    got = tk.load_ticket(t["id"])
    assert seen["started"] and got["status"] == "claimed" and "started_at" in got  # may be running: counts


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


# --- research_dir: never near the fleet's private folder -----------------------

SAMPLE = json.loads((Path(__file__).parent / "data" / "herdr_agent_list_sample.json").read_text())


def set_research_dir(env, projects, where):
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "research_dir": str(where)}))


def test_research_dir_default_is_outside_the_fleet_folder(projects, env):
    s = dp.load_settings()
    assert s["research_dir"] == env / "home" / "fleet-research"
    assert dp.research_dir_problem(s) is None


def test_research_dir_override_and_invalid(projects, env):
    set_research_dir(env, projects, env / "elsewhere")
    assert dp.load_settings()["research_dir"] == env / "elsewhere"
    (env / "fleet" / "dispatch.json").write_text(json.dumps({"research_dir": ""}))
    with pytest.raises(dp.SettingsError, match="research_dir"):
        dp.load_settings()


@pytest.mark.parametrize("where", ["fleet", "fleet/research", "."])
def test_research_dir_inside_or_around_fleet_home_is_refused(projects, herdr_log, env, where, capsys):
    t = idea()
    set_research_dir(env, projects, (env / where).resolve())
    assert "overlaps the fleet's private folder" in dp.research_dir_problem(dp.load_settings())
    assert dispatch_main(["--dry-run"]) == 0
    assert "skip: research_dir" in capsys.readouterr().out
    assert dispatch_main(["--once"]) == 0
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not herdr_log.exists()


def test_launch_research_refuses_directly_too(projects, herdr_log, env):
    t = idea()
    set_research_dir(env, projects, env / "fleet" / "research")
    s = dp.load_settings()
    d = dp.decide_investigations(tk.list_tickets(), settings())[0]
    code, message = lc.launch_research(d, s, lambda m: None)
    assert code == 1 and "private folder" in message
    assert tk.load_ticket(t["id"])["status"] == "proposed"
    assert not (env / "fleet" / "research").exists() and not herdr_log.exists()


def test_research_dir_symlink_into_fleet_home_is_refused(projects, env):
    (env / "fleet").mkdir(exist_ok=True)
    link = env / "innocent"
    link.symlink_to(env / "fleet")
    set_research_dir(env, projects, link)
    assert dp.research_dir_problem(dp.load_settings())


def test_research_folder_wires_the_guard_hooks(projects, herdr_log, env):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    path = env / "home" / "fleet-research" / t["id"] / ".claude" / "settings.json"
    got = json.loads(path.read_text())
    repo = Path(__file__).resolve().parent.parent
    mine = json.loads((repo / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in got["hooks"]["PreToolUse"]] == [g["matcher"] for g in mine]
    scripts = [a for g in got["hooks"]["PreToolUse"] for h in g["hooks"] for a in h["args"]]
    assert sorted(Path(a).name for a in scripts) == ["guard_bash.py", "guard_edits.py", "guard_reads.py"]
    for a in scripts:
        assert Path(a).is_absolute() and Path(a).parent == repo / ".claude" / "hooks" and Path(a).is_file()
    assert "CLAUDE_PROJECT_DIR" not in path.read_text()
    assert "Stop" not in got["hooks"]
    assert str(repo / "bin" / "fleet-statusline") in got["statusLine"]["command"]


def test_missing_guard_hooks_stop_the_launch(projects, herdr_log, monkeypatch, env):
    t = idea()
    monkeypatch.setattr("fleet.launch.HOOKS_DIR", env / "nowhere" / ".claude" / "hooks")
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not herdr_log.exists()


def test_codex_builder_gets_no_investigations(projects, herdr_log, env, capsys):
    t = idea()
    (env / "fleet" / "dispatch.json").write_text(json.dumps(
        {"projects_dir": str(projects), "builder": "codex"}))
    assert dispatch_main(["--dry-run"]) == 0
    assert "investigations need builder claude" in capsys.readouterr().out
    d = dp.decide_investigations(tk.list_tickets(), dp.load_settings())[0]
    code, message = lc.launch_research(d, dp.load_settings(), lambda m: None)
    assert code == 1 and "builder claude" in message
    assert tk.load_ticket(t["id"])["status"] == "proposed" and not herdr_log.exists()


@pytest.mark.parametrize("link", ["dir", "file"])
def test_symlinked_claude_folder_is_not_written_through(projects, herdr_log, env, link):
    t = idea()
    victim = env / "victim"
    victim.mkdir()
    folder = env / "home" / "fleet-research" / t["id"]
    (folder / ".claude").parent.mkdir(parents=True)
    if link == "dir":
        (folder / ".claude").symlink_to(victim)
    else:
        (folder / ".claude").mkdir()
        (victim / "settings.json").write_text("keep")
        (folder / ".claude" / "settings.json").symlink_to(victim / "settings.json")
    assert dispatch_main(["--once"]) == 1
    assert sorted(p.name for p in victim.iterdir()) == (["settings.json"] if link == "file" else [])
    if link == "file":
        assert (victim / "settings.json").read_text() == "keep"
    assert tk.load_ticket(t["id"])["status"] == "open" and not herdr_log.exists()


def test_git_timeout_during_research_launch_rolls_back(projects, herdr_log, monkeypatch):
    t = idea()

    def slow(*a, **k):
        raise __import__("subprocess").TimeoutExpired("git", 60)
    monkeypatch.setattr("fleet.launch.dp._git", slow)
    assert dispatch_main(["--once"]) == 1
    got = tk.load_ticket(t["id"])
    assert got["status"] == "open" and got["assignee"] is None and "started_at" not in got


def test_ticket_folder_that_is_its_own_repo_is_refused(projects, herdr_log, env):
    t = idea()
    folder = env / "home" / "fleet-research" / t["id"]
    folder.mkdir(parents=True)
    __import__("subprocess").run(["git", "init", "-q", str(folder)], check=True)
    assert dispatch_main(["--once"]) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
    assert not (folder / ".claude").exists() and not herdr_log.exists()


def test_failed_settings_write_leaves_no_temp_file(projects, herdr_log, env, monkeypatch):
    t = idea()

    def boom(*a, **k):
        raise OSError("disk full")
    real_replace = __import__("os").replace
    monkeypatch.setattr("fleet.launch.os.replace",
                        lambda src, dst: boom() if str(dst).endswith("settings.json") else real_replace(src, dst))
    assert dispatch_main(["--once"]) == 1
    claude = env / "home" / "fleet-research" / t["id"] / ".claude"
    assert list(claude.iterdir()) == []


def test_research_dir_inside_a_git_repo_is_refused(projects, env):
    repo = projects / "somerepo"
    repo.mkdir()
    __import__("subprocess").run(["git", "init", "-q", str(repo)], check=True)
    set_research_dir(env, projects, repo / "research")
    assert "inside a git repo" in dp.research_dir_problem(dp.load_settings())


# --- the real `herdr agent list` shape -----------------------------------------

def test_agent_list_parsing_against_a_real_capture(monkeypatch):
    monkeypatch.setattr("fleet.launch.herdr", lambda *a, **k: SAMPLE)
    assert lc.agent_listed("t-20261002-007") == (True, "idle")
    assert lc.agent_listed("t-20261002-008") == (True, "working")
    assert lc.agent_listed("t-20261002-099") == (False, None)  # the unnamed agent doesn't match


@pytest.mark.parametrize("bad", [{}, {"result": {}}, {"result": {"agents": "x"}},
                                 {"result": {"agents": [1]}}, {"agents": []},
                                 {"result": {"other": [{"name": "t-1", "agent_status": "idle"}]}}])
def test_agent_list_with_another_shape_is_an_error(monkeypatch, bad):
    monkeypatch.setattr("fleet.launch.herdr", lambda *a, **k: bad)
    with pytest.raises(lc.LaunchError):
        lc.agent_listed("t-1")


def test_only_idle_and_done_mean_stopped():
    assert lc.RESEARCH_DONE_STATES == ("idle", "done")  # not working, blocked or unknown


@pytest.mark.parametrize("state, finished", [("working", False), ("blocked", False), ("unknown", False),
                                             ("idle", True), ("done", True)])
def test_finish_by_state(projects, herdr_log, monkeypatch, state, finished):
    t = idea()
    assert dispatch_main(["--once"]) == 0
    tk.add_note(t["id"], "found")
    monkeypatch.setenv("FAKE_HERDR_STATE", state)
    done = list(lc.finish_investigations(tk.list_tickets(), lambda m: None))
    assert bool(done) is finished
