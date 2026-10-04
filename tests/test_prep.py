"""EM-9: the overnight queue. Real git, a fake herdr (with a world file), a fake ntfy and
a temporary FLEET_HOME. The prep agent's judgment is played by the test: it makes the git
commits and writes the report, exactly what the real agent is asked to do."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from conftest import ROOT
from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import launch as lc
from fleet import prep
from fleet import ticket as tk
from fleet.ship_cli import main as ship_main
from test_dispatch import GIT_ENV, calls, claim, git, herdr_log, make_repo, mk, projects, real_repo  # noqa: F401
from test_notify import FakeNtfy, write_config as write_ntfy_config

NIGHT = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)       # 03:00 Pacific (PDT)
JUST_BEFORE = datetime(2026, 10, 3, 12, 59, tzinfo=timezone.utc)  # 05:59 Pacific
SIX = datetime(2026, 10, 3, 13, 0, tzinfo=timezone.utc)         # 06:00 Pacific

FAKE_REVIEW = """#!/bin/bash
# Fake review.sh: records where it ran, then a PASS on the tip (or exit 1 for a name in FAKE_REVIEW_FAIL).
echo "$PWD" >> "$FAKE_REVIEW_LOG"
branch="$(git branch --show-current)"
[[ " ${FAKE_REVIEW_FAIL:-} " != *" $branch "* ]] || exit 1
PYTHONPATH="$FAKE_ROOT" python3 -c "
import sys
from fleet import ticket as tk
tk.record_verdict(sys.argv[1], 'app', 'PASS', sys.argv[2], 'Codex')" "$branch" "$(git rev-parse HEAD)"
"""
FAKE_CHECK = """#!/bin/bash
echo "$PWD" >> "$FAKE_CHECK_LOG"
exit 0
"""
FAKE_FINISH = """#!/bin/bash
# Fake finish-task: a real merge of the task branch (a conflict leaves the merge half done).
echo "$1" >> "$FAKE_FINISH_LOG"
[[ " ${FAKE_FINISH_FAIL:-} " != *" $1 "* ]] || exit 1
git -c user.name=T -c user.email=t@example.com merge -q --no-ff "task/$1" -m "Merge task/$1"
"""


class At:
    """A clock frozen at one moment."""

    def __init__(self, when):
        self.when = when

    def now(self):
        return self.when


@pytest.fixture
def ntfy():
    fake = FakeNtfy()
    write_ntfy_config(fake.url)
    yield fake
    fake.stop()


@pytest.fixture
def q(projects, env, herdr_log, monkeypatch):
    """A repo with fake check/review/finish scripts, and a fake herdr that knows a workspace."""
    repo = real_repo(projects)
    for name, text in (("check.sh", FAKE_CHECK), ("review.sh", FAKE_REVIEW), ("finish-task.sh", FAKE_FINISH)):
        (repo / "scripts" / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fake scripts")
    world = env / "world.json"
    world.write_text(json.dumps({"workspaces": [{"workspace_id": "w1", "label": "fleet"}], "tabs": [], "agents": []}))
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(world))
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    for var in ("REVIEW", "CHECK", "FINISH"):
        monkeypatch.setenv(f"FAKE_{var}_LOG", str(env / f"{var.lower()}.log"))
    monkeypatch.setattr("fleet.launch.POLL_INTERVAL", 0.01)
    return repo


def world(env):
    return json.loads((env / "world.json").read_text())


def set_agent_status(env, name, status):
    w = world(env)
    for a in w["agents"]:
        if a["name"] == name:
            a["agent_status"] = status
    (env / "world.json").write_text(json.dumps(w))


def log_lines(env, var):
    path = env / f"{var}.log"
    return path.read_text().splitlines() if path.exists() else []


def commit(wt, files, message="work"):
    for rel, text in files.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", message)
    return git(wt, "rev-parse", "HEAD").strip()


def merge(wt, *args):
    """git merge that may conflict (the exit code is the answer, not an error)."""
    return subprocess.run(["git", *GIT_ENV, "-C", str(wt), "merge", *args], capture_output=True, text=True)


def in_review(repo, branch, files, **fields):
    """A finished ticket: a task folder with a commit, a ticket in review."""
    wt = repo / ".worktrees" / branch.removeprefix("task/")
    git(repo, "worktree", "add", "-q", str(wt), "-b", branch)
    commit(wt, files)
    t = claim(mk(branch=branch, **fields))
    tk.set_status(t["id"], "claimed", allowed_from=("claimed",), base_commit=git(repo, "rev-parse", "main").strip())
    tk.set_status(t["id"], "review", allowed_from=("claimed",))
    return tk.load_ticket(t["id"]), wt


def start_prep(settings_over=None, now=NIGHT):
    settings = dp.load_settings()
    settings.update(settings_over or {})
    out, warns = [], []
    prep.maybe_start(settings, now, out.append, warns.append)
    return settings, out, warns


def finish_prep(settings, now=NIGHT):
    """One finish pass, then (when re-checks were started in the background) waits for them
    and passes again, as the next cycle would."""
    out, warns = [], []
    prep.finish(settings, now, out.append, warns.append)
    if prep._CHECKS:
        prep.wait_checks()
        prep.finish(settings, now, out.append, warns.append)
    return out, warns


def report(settings, **data):
    folder = lc.research_folder(prep.FOLDER_ID, settings)
    (folder / "report-prep.json").write_text(json.dumps(data))


def agent_names(herdr_log):
    if not herdr_log.exists():    # herdr was never called
        return []
    return [c[2] for c in calls(herdr_log) if c[:2] == ["agent", "start"]]


def sent(ntfy):
    return [(r["body"]["title"], r["body"]["message"], r["priority"]) for r in ntfy.requests]


# --- settings ---------------------------------------------------------------------

def test_prep_time_default_and_override(projects, env):
    assert dp.load_settings()["prep_time"] == "06:00"
    (env / "fleet" / "settings.json").write_text(json.dumps({"prep_time": "05:30"}))
    assert dp.load_settings()["prep_time"] == "05:30"


@pytest.mark.parametrize("value", ["6:00", "24:00", "06:60", "noon", 600, None, True])
def test_prep_time_invalid(projects, env, value):
    (env / "fleet" / "settings.json").write_text(json.dumps({"prep_time": value}))
    with pytest.raises(dp.SettingsError, match="prep_time"):
        dp.load_settings()


# --- 1. a ticket in review no longer fills a slot --------------------------------------

def test_two_tickets_reach_review_and_the_next_still_launches(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    in_review(q, "task/two", {"two.txt": "2"})
    third = mk(branch="task/three")
    out = []
    # 03:00 Pacific with work still queued: no prep pass yet, but the next ticket starts.
    assert dispatch_cli._cycle(dp.load_settings(), out.append, out.append, clock=At(NIGHT)) == 0
    assert tk.load_ticket(third["id"])["status"] == "claimed"
    assert agent_names(herdr_log) == [third["id"].lower()]
    assert (q / ".worktrees" / "three").is_dir()


def test_reviews_still_hold_their_branch(q):
    in_review(q, "task/one", {"one.txt": "1"})
    same = mk(branch="task/one")
    (d,) = dp.decide(tk.list_tickets(), dp.load_settings())
    assert d.ticket["id"] == same["id"] and "in use by claimed ticket" in d.skip


# --- 2. when the prep pass starts --------------------------------------------------------

def test_prep_starts_when_nothing_is_left_to_build_and_only_once(q, herdr_log, env):
    a, _ = in_review(q, "task/one", {"one.txt": "1"})
    b, _ = in_review(q, "task/two", {"two.txt": "2"})
    settings, out, _ = start_prep()
    assert agent_names(herdr_log) == ["prep"]
    tab = next(c for c in calls(herdr_log) if c[:2] == ["tab", "create"])
    assert tab[tab.index("--label") + 1] == "PREP"
    assert prep.locked_ids() == {a["id"], b["id"]}
    manifest = json.loads((lc.research_folder(prep.FOLDER_ID, settings) / "manifest-prep.json").read_text())
    assert [t["id"] for t in manifest["tickets"]] == [a["id"], b["id"]]
    assert "prep agent started over 2 tickets" in out[0]
    start_prep(now=NIGHT)   # still running: not a second one
    assert agent_names(herdr_log) == ["prep"]


def test_prep_waits_for_prep_time_while_work_remains(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    mk(branch="task/queued")     # still something to build
    start_prep(now=JUST_BEFORE)
    assert agent_names(herdr_log) == []
    start_prep(now=SIX)
    assert agent_names(herdr_log) == ["prep"]


def test_prep_time_setting_moves_the_hour(q, herdr_log):
    in_review(q, "task/one", {"one.txt": "1"})
    mk(branch="task/queued")
    start_prep({"prep_time": "07:00"}, now=SIX)
    assert agent_names(herdr_log) == []
    start_prep({"prep_time": "05:00"}, now=JUST_BEFORE)
    assert agent_names(herdr_log) == ["prep"]


def test_nothing_in_review_means_no_prep(q, herdr_log):
    start_prep()
    assert agent_names(herdr_log) == []


def test_a_builder_that_is_not_claude_is_refused(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    (env / "fleet" / "settings.json").write_text(json.dumps({"builder": "codex"}))
    _, out, _ = start_prep()
    assert agent_names(herdr_log) == [] and "not started" in out[0]


def test_review_watch_leaves_locked_tickets_alone(q, herdr_log, env):
    from fleet import review_watch as rw
    a, wt = in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep()
    commit(wt, {"more.txt": "x"})        # the prep agent moves the tip: normally this sends it back to claimed
    rw.check(settings, NIGHT, lambda m: None, lambda m: None)
    assert tk.load_ticket(a["id"])["status"] == "review"


# --- 2a. duplicates -----------------------------------------------------------------------

def test_of_two_duplicates_the_one_with_more_context_is_kept_and_gains_the_extra_test(q, herdr_log, env, ntfy):
    full, full_wt = in_review(q, "task/full", {"feature.py": "def f(): return 1\n", "tests/test_feature.py": "def test_a(): pass\n"},
                              title="Add feature", body="The full spec. " * 20)
    tk.add_note(full["id"], "design note")
    thin, thin_wt = in_review(q, "task/thin", {"feature.py": "def f(): return 1\n", "tests/test_edge.py": "def test_edge(): pass\n"},
                              title="Add the feature", body="short")
    settings, _, _ = start_prep()
    before = prep.tip_of(q, "task/full")
    # the agent: fold the other ticket's extra test into the keeper's branch, then report
    commit(full_wt, {"tests/test_edge.py": "def test_edge(): pass\n"}, "fold in the edge test")
    report(settings, order=[full["id"]], duplicates=[{"keep": full["id"], "cancel": [thin["id"]], "note": "edge test"}],
           tickets={full["id"]: {"summary": "adds the feature"}})
    set_agent_status(env, "prep", "idle")
    out, warns = finish_prep(settings)

    assert tk.load_ticket(thin["id"])["status"] == "cancelled"
    note = tk.load_ticket(thin["id"])["close_note"]
    assert full["id"] in note and "Duplicate" in note
    kept = tk.load_ticket(full["id"])
    assert kept["status"] == "review" and prep.tip_of(q, "task/full") != before
    assert (full_wt / "tests" / "test_edge.py").is_file()
    assert any(n["text"].startswith(prep.NOTE_PREFIX) and thin["id"] in n["text"] for n in kept["notes"])
    # the changed branch (only) was checked and reviewed again, in its own folder
    assert log_lines(env, "check") == [str(full_wt)] == log_lines(env, "review")
    assert kept["verdict"]["result"] == "PASS" and kept["verdict"]["commit"] == prep.tip_of(q, "task/full")
    # the list: one line, the keeper, with the folded duplicate
    ((tid, line),) = prep.read_list()
    assert tid == full["id"] and f"folded in: {thin['id']}" in line and "adds the feature" in line
    assert [x.strip() for x in line.split("|")][:3] == [full["id"], "Add feature", "medium"]
    assert sent(ntfy) == [("1 ready to ship, list ready", "run `ship --list`", "default")]
    assert prep.locked_ids() == set() and not warns


def test_a_duplicate_is_not_cancelled_when_the_keeper_has_less_context(q, herdr_log, env):
    thin, _ = in_review(q, "task/thin", {"a.txt": "1"}, body="short")
    full, _ = in_review(q, "task/full", {"a.txt": "1"}, body="a much longer and fuller specification " * 5)
    settings, _, _ = start_prep()
    report(settings, order=[thin["id"], full["id"]], duplicates=[{"keep": thin["id"], "cancel": [full["id"]]}])
    set_agent_status(env, "prep", "idle")
    _, warns = finish_prep(settings)
    assert tk.load_ticket(full["id"])["status"] == "review" and tk.load_ticket(thin["id"])["status"] == "review"
    assert any("more context" in w for w in warns)
    assert [i for i, _ in prep.read_list()] == [thin["id"], full["id"]]


# --- 2b. conflicts --------------------------------------------------------------------------

def conflicting(q):
    base = "def shared():\n    return 0\n"
    wt = q / "code.py"
    wt.write_text(base)
    git(q, "add", "-A")
    git(q, "commit", "-q", "-m", "shared code")
    a, a_wt = in_review(q, "task/alpha", {"code.py": base + "def alpha():\n    return 'a'\n"}, title="Alpha")
    b, b_wt = in_review(q, "task/beta", {"code.py": base + "def beta():\n    return 'b'\n"}, title="Beta")
    return a, a_wt, b, b_wt


def test_two_conflicting_branches_are_reconciled_and_both_ship_in_list_order(q, herdr_log, env, ntfy, capsys):
    a, a_wt, b, b_wt = conflicting(q)
    settings, _, _ = start_prep()
    # the agent: order alpha, beta; bring beta up to date with alpha by writing the best combination
    merged = merge(b_wt, "task/alpha", "-m", "bring up to date")
    assert merged.returncode != 0    # a mechanical merge really does conflict
    (b_wt / "code.py").write_text("def shared():\n    return 0\n\ndef alpha():\n    return 'a'\n\ndef beta():\n    return 'b'\n")
    git(b_wt, "add", "-A")
    git(b_wt, "commit", "-q", "-m", "combine alpha and beta")
    report(settings, order=[a["id"], b["id"]],
           tickets={a["id"]: {"summary": "adds alpha"}, b["id"]: {"summary": "adds beta", "reconciled": [a["id"]]}})
    set_agent_status(env, "prep", "idle")
    out, warns = finish_prep(settings)

    assert not warns
    assert log_lines(env, "check") == [str(b_wt)] == log_lines(env, "review")   # only the changed branch
    rows = [[x.strip() for x in line.split("|")] for _, line in prep.read_list()]
    assert [r[0] for r in rows] == [a["id"], b["id"]]
    assert rows[1][3] == "adds beta" and rows[1][4] == f"reconciled with: {a['id']}" and rows[0][4] == "-"
    assert sent(ntfy)[0][0] == "2 ready to ship, list ready"

    # the human ships the list: both merge, in list order, with no conflict
    assert ship_main(["--list"]) == 0
    assert log_lines(env, "finish") == ["alpha", "beta"]
    code = (q / "code.py").read_text()
    assert "def alpha" in code and "def beta" in code
    merges = git(q, "log", "--first-parent", "--merges", "--format=%s", "--reverse").split("\n")
    assert [m for m in merges if m] == ["Merge task/alpha", "Merge task/beta"]
    assert tk.load_ticket(a["id"])["status"] == "done" and tk.load_ticket(b["id"])["status"] == "done"
    assert [c for c in agent_names(herdr_log) if c != "prep"] == []   # no conflict, no agent


def test_a_branch_that_fails_its_recheck_goes_back_and_is_left_off_the_list(q, herdr_log, env, ntfy, monkeypatch):
    a, a_wt = in_review(q, "task/alpha", {"a.txt": "1"})
    b, b_wt = in_review(q, "task/beta", {"b.txt": "2"})
    settings, _, _ = start_prep()
    commit(b_wt, {"b.txt": "changed by prep"}, "reconcile")
    monkeypatch.setenv("FAKE_REVIEW_FAIL", "task/beta")
    report(settings, order=[a["id"], b["id"]])
    set_agent_status(env, "prep", "idle")
    out, _ = finish_prep(settings)
    assert tk.load_ticket(b["id"])["status"] == "claimed"
    assert any(n["text"].startswith(prep.NOTE_PREFIX) and "left off" in n["text"] for n in tk.load_ticket(b["id"])["notes"])
    assert [i for i, _ in prep.read_list()] == [a["id"]]
    assert sent(ntfy)[0][0] == "1 ready to ship, list ready"
    assert any("left off the ship list" in line for line in out)


def test_an_unusable_report_still_rechecks_what_changed_and_lists_everything(q, herdr_log, env):
    a, _ = in_review(q, "task/alpha", {"a.txt": "1"})
    b, _ = in_review(q, "task/beta", {"b.txt": "2"})
    settings, _, _ = start_prep()
    folder = lc.research_folder(prep.FOLDER_ID, settings)
    (folder / "report-prep.json").write_text("not json")
    set_agent_status(env, "prep", "done")
    _, warns = finish_prep(settings)
    assert any("unusable" in w for w in warns)
    assert [i for i, _ in prep.read_list()] == [a["id"], b["id"]]
    assert log_lines(env, "check") == []   # nothing moved, nothing to re-check


def test_a_working_agent_is_not_finished_and_its_tab_closes_afterwards(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep()
    finish_prep(settings)
    assert prep.locked_ids() and not (env / "fleet" / "ship-list.txt").exists()
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert prep.locked_ids() == set()
    assert any(c[:2] == ["tab", "close"] for c in calls(herdr_log))


# --- once per day -----------------------------------------------------------------------------

def test_the_prep_agent_runs_once_a_day_unless_new_tickets_reach_review(q, herdr_log, env):
    in_review(q, "task/one", {"one.txt": "1"})
    settings, _, _ = start_prep()
    set_agent_status(env, "prep", "idle")
    finish_prep(settings)
    assert agent_names(herdr_log) == ["prep"]
    for now in (NIGHT, SIX, datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc)):
        start_prep(now=now)
    assert agent_names(herdr_log) == ["prep"]                  # the same tickets: not again
    in_review(q, "task/two", {"two.txt": "2"})                 # a new one reaches review
    start_prep(now=datetime(2026, 10, 3, 23, 0, tzinfo=timezone.utc))
    assert agent_names(herdr_log) == ["prep", "prep"]


# --- 3. ship --list ----------------------------------------------------------------------------

def write_list(ids_lines):
    prep._write_list(ids_lines, NIGHT)


def test_ship_list_stops_at_the_first_failure(q, herdr_log, env, monkeypatch, capsys):
    ts = [in_review(q, f"task/t{i}", {f"t{i}.txt": str(i)})[0] for i in (1, 2, 3)]
    write_list([f"{t['id']} | T | medium | does {i} | -" for i, t in enumerate(ts, 1)])
    monkeypatch.setenv("FAKE_FINISH_FAIL", "t2")
    assert ship_main(["--list"]) == 1
    err = capsys.readouterr().err
    assert f"stopped at {ts[1]['id']}" in err and "rest of the list was not shipped" in err
    assert log_lines(env, "finish") == ["t1", "t2"]          # t3 was never tried
    assert [tk.load_ticket(t["id"])["status"] for t in ts] == ["done", "review", "review"]


def test_ship_list_shows_the_list_and_skips_what_is_done(q, herdr_log, env, capsys):
    ts = [in_review(q, f"task/t{i}", {f"t{i}.txt": str(i)})[0] for i in (1, 2)]
    write_list([f"{t['id']} | Title {i} | low | does {i} | -" for i, t in enumerate(ts, 1)])
    assert ship_main([ts[0]["id"]]) == 0                       # one ticket on its own still works
    assert ship_main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "Title 1" in out and "Title 2" in out and f"{ts[0]['id']} is already done" in out
    assert log_lines(env, "finish") == ["t1", "t2"]


def test_ship_list_refuses_a_ticket_that_left_review(q, herdr_log, env, capsys):
    t, _ = in_review(q, "task/t1", {"t1.txt": "1"})
    write_list([f"{t['id']} | T | medium | does 1 | -"])
    tk.set_status(t["id"], "claimed", allowed_from=("review",))
    assert ship_main(["--list"]) == 1
    assert "no longer ready" in capsys.readouterr().err and log_lines(env, "finish") == []


def test_ship_list_without_a_list(projects, capsys):
    assert ship_main(["--list"]) == 1
    assert "there is no ship list" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["BD-1", "--list"]])
def test_ship_needs_an_id_or_the_list_flag(projects, capsys, args):
    with pytest.raises(SystemExit):
        ship_main(args)


@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_ship_list_is_human_only(projects, monkeypatch, capsys, var):
    monkeypatch.setenv(var, "1")
    assert ship_main(["--list"]) == 1
    assert "only a human ships" in capsys.readouterr().err


# --- 3. a conflict at ship time -----------------------------------------------------------------

def test_a_conflict_at_ship_time_launches_a_reconcile_agent_and_pings_when_ready(q, herdr_log, env, ntfy, capsys):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0 and tk.load_ticket(a["id"])["status"] == "done"
    assert agent_names(herdr_log) == []                        # no conflict so far: no agent
    capsys.readouterr()

    assert ship_main([b["id"]]) == 1                           # beta now conflicts with main
    err = capsys.readouterr().err
    assert "conflicts with main" in err and "reconcile agent" in err
    assert git(q, "status", "--porcelain").strip() == ""       # main was put back as it was
    assert agent_names(herdr_log) == [f"prep-{b['id'].lower()}"]
    assert prep.locked_ids() == {b["id"]} and tk.load_ticket(b["id"])["status"] == "review"
    assert ship_main([b["id"]]) == 1 and "still working" in capsys.readouterr().err   # not while it works

    # the agent merges main into the branch, keeping both features
    settings = dp.load_settings()
    assert merge(b_wt, "main").returncode != 0
    (b_wt / "code.py").write_text("def shared():\n    return 0\n\ndef alpha():\n    return 'a'\n\ndef beta():\n    return 'b'\n")
    git(b_wt, "add", "-A")
    git(b_wt, "commit", "-q", "-m", "merge main, keep both")
    set_agent_status(env, f"prep-{b['id'].lower()}", "idle")
    out, warns = finish_prep(settings)
    assert not warns and prep.locked_ids() == set()
    assert tk.load_ticket(b["id"])["status"] == "review"
    assert log_lines(env, "review") == [str(b_wt)]
    # the review watch now sends the usual "ready to ship" ping for the new tip
    from fleet import review_watch as rw
    ticket = tk.load_ticket(b["id"])
    assert rw.passed_for_tip(ticket, prep.tip_of(q, "task/beta")) and rw.has_summary_after_review(ticket)

    assert ship_main([b["id"]]) == 0
    assert tk.load_ticket(b["id"])["status"] == "done"
    assert "def alpha" in (q / "code.py").read_text() and "def beta" in (q / "code.py").read_text()


def test_a_reconcile_agent_that_leaves_the_conflict_sends_the_ticket_back(q, herdr_log, env, capsys):
    a, a_wt, b, b_wt = conflicting(q)
    assert ship_main([a["id"]]) == 0
    assert ship_main([b["id"]]) == 1
    set_agent_status(env, f"prep-{b['id'].lower()}", "done")   # the agent stopped without doing anything
    out, warns = finish_prep(dp.load_settings())
    assert tk.load_ticket(b["id"])["status"] == "claimed"
    assert prep.locked_ids() == set()
