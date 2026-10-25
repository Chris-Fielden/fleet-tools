"""BD-128: auto-open rules and file-area locks (accept tests 17 to 23 of ID-56)."""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from conftest import run
from fleet import areas
from fleet import dispatch as dp
from fleet import ticket as tk
from test_dispatch import claim, make_repo, mk, projects  # noqa: F401  (fixtures)
from test_guard_hooks import bash, run_hook

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
GOOD_BODY = "Add a helper.\n\nAcceptance:\n- the helper returns the sorted names\n- a test covers the empty case\n"


def as_agent(monkeypatch, who="claude"):
    monkeypatch.setenv("CLAUDECODE" if who == "claude" else "CODEX_SANDBOX", "1")


def as_human(monkeypatch):
    for var in ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"):
        monkeypatch.delenv(var, raising=False)


def handoff(monkeypatch):
    """A human files an investigate ticket and approves it: the handoff."""
    as_human(monkeypatch)
    h = tk.create_ticket({"type": "investigate", "repo": "app", "branch": "task/handoff",
                          "title": "Handoff", "body": "GOAL ..."})
    tk.approve_ticket(h["id"])
    return h


def controller_ticket(monkeypatch, h, n=1, **over):
    as_agent(monkeypatch)
    f = {"type": "build", "repo": "app", "branch": f"task/step-{n}", "title": f"Step {n}",
         "body": GOOD_BODY, "origin": h["id"], "areas": ["docs/notes.md"]}
    f.update(over)
    t = tk.create_ticket(f)
    as_human(monkeypatch)   # from here on the dispatcher process acts
    return t


def settings(**over):
    s = dp.load_settings()
    s.update(over)
    return s


def opened(tickets, settings_=None, now=NOW):
    return [t["id"] for t in dp.auto_open(tk.list_tickets(), settings_ or settings(), now)]


# --- 17: an agent cannot open its own ticket ----------------------------------------

def test_agent_ticket_starts_proposed_and_cannot_be_opened_by_an_agent(projects, monkeypatch):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h)
    assert t["status"] == "proposed" and t["origin"] == h["id"]
    as_agent(monkeypatch)
    with pytest.raises(tk.TicketError, match="only the dispatcher"):
        tk.open_proposed(t["id"], "me")
    with pytest.raises(tk.TicketError, match="only a human"):
        tk.approve_ticket(t["id"])
    assert tk.load_ticket(t["id"])["status"] == "proposed"
    as_human(monkeypatch)
    assert opened(tk.list_tickets()) == [t["id"]]   # only the dispatcher's rule opens it
    got = tk.load_ticket(t["id"])
    assert got["status"] == "open" and "auto_opened_at" in got
    assert f"AUTO_OPEN {t['id']}" in dp.log_path().read_text()


@pytest.mark.parametrize("make", ["no_origin", "agent_handoff", "unapproved_handoff", "chained"])
def test_stays_proposed_without_a_human_handoff(projects, monkeypatch, make):
    h = handoff(monkeypatch)
    if make == "no_origin":
        t = controller_ticket(monkeypatch, h, origin=None)
    elif make == "agent_handoff":   # an agent-filed investigate ticket is not a handoff
        as_agent(monkeypatch)
        a = tk.create_ticket({"type": "investigate", "repo": "app", "branch": "task/inv", "title": "I", "body": "b"})
        t = controller_ticket(monkeypatch, a)
    elif make == "unapproved_handoff":
        as_human(monkeypatch)
        p = tk.create_ticket({"type": "investigate", "repo": "app", "branch": "task/inv", "title": "I", "body": "b"})
        t = controller_ticket(monkeypatch, p)
    elif make == "chained":   # work that was itself auto-opened cannot vouch for more
        first = controller_ticket(monkeypatch, h, 1, type="investigate", parent=h["id"], areas=None, body="look")
        assert opened(None) == [first["id"]]
        t = controller_ticket(monkeypatch, tk.load_ticket(first["id"]), 2)
    assert opened(None) == []
    assert tk.load_ticket(t["id"])["status"] == "proposed"


def test_investigate_opens_only_under_a_handoff_parent(projects, monkeypatch):
    h = handoff(monkeypatch)
    ok = controller_ticket(monkeypatch, h, 1, type="investigate", parent=h["id"], areas=None, body="look")
    other = controller_ticket(monkeypatch, h, 2, type="investigate", areas=None, body="look")
    assert opened(None) == [ok["id"]]
    assert tk.load_ticket(other["id"])["status"] == "proposed"


def test_parent_only_investigate_opens(projects, monkeypatch):
    h = handoff(monkeypatch)
    inv = controller_ticket(monkeypatch, h, 1, type="investigate", parent=h["id"], areas=None, origin=None, body="look")
    assert "origin" not in inv
    assert opened(None) == [inv["id"]]


def test_criteria_are_needed(projects, monkeypatch):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h, body="Add a helper, no criteria listed.")
    assert opened(None) == [] and tk.load_ticket(t["id"])["status"] == "proposed"
    assert dp.acceptance_criteria(GOOD_BODY) == ["the helper returns the sorted names", "a test covers the empty case"]
    assert dp.acceptance_criteria("Acceptance:\n- short\n") == []
    assert dp.acceptance_criteria("## Acceptance criteria\n1. it prints the list\n") == ["it prints the list"]


# --- 18: a high-risk file set never auto-opens ----------------------------------------

@pytest.mark.parametrize("areas_, body", [
    ([".claude/hooks/guard_bash.py"], GOOD_BODY),
    (["fleet/dispatch_cli.py"], GOOD_BODY),
    (["fleet/dispatch*.py"], GOOD_BODY),
    (["**"], GOOD_BODY),
    (["fleet/*.py"], GOOD_BODY),
    ([".claude"], GOOD_BODY),
    (["CLAUDE.md"], GOOD_BODY),
    (["src/auth/login.py"], GOOD_BODY),
    (["k8s/"], GOOD_BODY),
    (["docs/x.md"], GOOD_BODY + "\nAlso edit scripts/review.sh.\n"),
])
def test_high_risk_file_set_never_auto_opens(projects, monkeypatch, areas_, body):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h, areas=areas_, body=body)
    assert opened(None) == []
    assert tk.load_ticket(t["id"])["status"] == "proposed"


def test_stored_high_risk_never_auto_opens(projects, monkeypatch):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h)
    tk.raise_risk(t["id"], "high")
    assert opened(None) == []


def test_ordinary_file_set_opens(projects, monkeypatch):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h, areas=["fleet/notes.py", "tests/test_notes.py"])
    assert opened(None) == [t["id"]]


def test_no_declared_areas_stays_proposed(projects, monkeypatch):
    h = handoff(monkeypatch)
    controller_ticket(monkeypatch, h, areas=None)
    assert opened(None) == []


# --- 19: caps are respected -----------------------------------------------------------

def test_hourly_cap(projects, monkeypatch):
    h = handoff(monkeypatch)
    monkeypatch.setattr(tk, "AGENT_TICKET_LIMIT", 50)   # let the agent file them
    ts = [controller_ticket(monkeypatch, h, n, areas=[f"docs/n{n}.md"]) for n in range(1, 5)]
    monkeypatch.setattr(tk, "AGENT_TICKET_LIMIT", 2)
    assert opened(None, settings(max_auto_open_per_day=10, max_launches_per_day=10)) == [ts[0]["id"], ts[1]["id"]]
    assert opened(None, settings(max_auto_open_per_day=10)) == []   # still inside the hour


def test_daily_cap_and_zero_turns_it_off(projects, monkeypatch):
    h = handoff(monkeypatch)
    ts = [controller_ticket(monkeypatch, h, n, areas=[f"docs/n{n}.md"]) for n in (1, 2)]
    assert opened(None, settings(max_auto_open_per_day=0)) == []
    assert opened(None, settings(max_auto_open_per_day=1)) == [ts[0]["id"]]
    assert opened(None, settings(max_auto_open_per_day=1)) == []
    assert opened(None, settings(max_auto_open_per_day=1), NOW + timedelta(days=1)) == [ts[1]["id"]]


def test_launch_cap_and_deny_list(projects, monkeypatch):
    h = handoff(monkeypatch)
    t = controller_ticket(monkeypatch, h)
    s = settings()
    dp.add_launches(dp.local_day(NOW, s["timezone"]), s["max_launches_per_day"])
    assert opened(None, s) == []
    s2 = settings(auto_open_deny_repos=["app"])
    assert opened(None, s2) == []
    assert tk.load_ticket(t["id"])["status"] == "proposed"


def test_settings_validate(projects):
    (projects.parent / "fleet" / "settings.json").write_text(json.dumps({"auto_open_deny_repos": "app"}))
    with pytest.raises(dp.SettingsError):
        dp.load_settings()
    (projects.parent / "fleet" / "settings.json").write_text(json.dumps(
        {"auto_open_deny_repos": ["a"], "max_auto_open_per_day": 2, "area_lease_hours": 3,
         "require_ticket_for_edits": True}))
    s = dp.load_settings()
    assert s["auto_open_deny_repos"] == ["a"] and s["max_auto_open_per_day"] == 2 and s["area_lease_hours"] == 3


# --- 20: clearing CLAUDECODE does not get an agent past guard_bash -------------------------

@pytest.mark.parametrize("command", [
    "env -u CLAUDECODE ticket approve BD-1",
    "env -u CLAUDECODE bin/ticket approve BD-1",
    "env -u CLAUDECODE bin/ticket release-area BD-1",
    "CLAUDECODE= ticket release-area BD-1",
    "ticket release-area BD-1",
    "fleet ticket release-area BD-1",
    "env -u CLAUDECODE bin/dispatch --once",
    "env -i bin/dispatch --once",
    "sh -c 'unset CLAUDECODE; ticket approve BD-1'",
])
def test_env_spoof_is_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


def test_claim_area_stays_open_to_agents(tmp_path):
    assert bash("ticket claim-area --repo r --branch task/x --title t --body b --area docs/x.md", tmp_path) is None


# --- 21: overlapping globs serialise --------------------------------------------------------

@pytest.mark.parametrize("a, b, meet", [
    ("fleet/a.py", "fleet/a.py", True),
    ("fleet/a.py", "fleet/b.py", False),
    ("fleet/*.py", "fleet/a.py", True),
    ("fleet/*.py", "fleet/sub/a.py", False),
    ("fleet/**", "fleet/sub/a.py", True),
    ("fleet/", "fleet/a.py", True),
    ("fleet", "fleet/a.py", True),            # no extension: a folder too
    ("fleet/a.py", "docs/**", False),
    ("**/*.md", "docs/x.md", True),
    ("**/*.md", "docs/x.py", False),
    ("fleet/d*.py", "fleet/*s.py", True),     # both wild: assume they meet
    ("docs/a.md", "tests/a.md", False),
])
def test_intersects(a, b, meet):
    assert areas.intersects(a, b) is meet and areas.intersects(b, a) is meet


def test_overlapping_tickets_serialise(projects, monkeypatch):
    make_repo(projects)
    first = mk(branch="task/first", areas=["fleet/*.py"])
    second = mk(branch="task/second", areas=["fleet/risk.py"])
    third = mk(branch="task/third", areas=["docs/**"])
    plain = mk(branch="task/plain")
    claim(first)
    areas.acquire(tk.load_ticket(first["id"]), tk.list_tickets())   # decide() judges by the real clock
    by = {d.ticket["id"]: d for d in dp.decide(tk.list_tickets(), settings(max_running=4))}
    assert by[second["id"]].skip.startswith(f"waiting for the area lock of {first['id']}")
    assert by[third["id"]].skip != by[second["id"]].skip and "area lock" not in (by[third["id"]].skip or "")
    assert "area lock" not in (by[plain["id"]].skip or "")   # no areas: nothing to serialise
    with pytest.raises(areas.AreaConflict):
        areas.acquire(tk.load_ticket(second["id"]), tk.list_tickets())
    # a lock of another repo never blocks
    other = mk(repo="other", branch="task/other", areas=["fleet/risk.py"])
    assert areas.blockers(other, areas._read(), tk.list_tickets(), datetime.now(timezone.utc)) == []


def test_lock_ends_with_its_ticket(projects, monkeypatch):
    make_repo(projects)
    first = mk(branch="task/first", areas=["fleet/a.py"])
    second = mk(branch="task/second", areas=["fleet/a.py"])
    claim(first)
    areas.acquire(tk.load_ticket(first["id"]), tk.list_tickets(), NOW)
    assert areas.blocked_reason(tk.load_ticket(second["id"]), tk.list_tickets(), NOW)
    tk.set_status(first["id"], "failed", allowed_from=("claimed",))
    assert areas.blocked_reason(tk.load_ticket(second["id"]), tk.list_tickets(), NOW) is None
    assert areas.sweep(tk.list_tickets(), frozenset(), NOW) == [first["id"]]


def test_unreadable_table_blocks(projects, monkeypatch):
    make_repo(projects)
    t = mk(areas=["fleet/a.py"])
    areas.table_path().write_text("not json")
    assert "can't be read" in areas.blocked_reason(t, [t], NOW)


def test_real_diffs_overlap():
    a = {"id": "BD-1", "repo": "r", "status": "claimed"}
    b = {"id": "BD-2", "repo": "r", "status": "review"}
    c = {"id": "BD-3", "repo": "q", "status": "claimed"}
    d = {"id": "BD-4", "repo": "r", "status": "open"}
    diffs = {"BD-1": ["x.py", "y.py"], "BD-2": ["y.py"], "BD-3": ["y.py"], "BD-4": ["y.py"]}
    assert areas.overlaps([a, b, c, d], diffs) == [("BD-1", "BD-2", "y.py")]
    assert areas.overlaps([a, b], {"BD-1": ["x.py"], "BD-2": ["z.py"]}) == []
    both = areas.overlaps([a, b], {"BD-1": ["x.py", "y.py"], "BD-2": ["y.py", "x.py"]})
    assert both == [("BD-1", "BD-2", "x.py"), ("BD-1", "BD-2", "y.py")]


def test_overlap_ping_once(projects, monkeypatch):
    from fleet import review_watch as rw
    repo = make_repo(projects)
    for name in ("one", "two"):
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", f"task/{name}", "main"], check=True)
        (repo / "shared.txt").write_text(name)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n",
                        "commit", "-q", "-m", name], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    one, two = mk(branch="task/one"), mk(branch="task/two")
    claim(one)
    claim(two)
    sent = []
    monkeypatch.setattr(rw.notify, "notify_quietly", lambda t, ev, warn, detail=None: sent.append((t["id"], ev, detail)) or True)
    state = {"sent": []}
    rw._overlap_pings(tk.list_tickets(), settings(), state, print)
    rw._overlap_pings(tk.list_tickets(), settings(), state, print)
    assert len(sent) == 1 and sent[0][1] == "needs_human" and "shared.txt" in sent[0][2]


# --- 22: lease expiry -----------------------------------------------------------------------

def test_lease_expiry_frees_the_area(projects, monkeypatch):
    make_repo(projects)
    first = mk(branch="task/first", areas=["fleet/a.py"])
    second = mk(branch="task/second", areas=["fleet/a.py"])
    claim(first)
    areas.acquire(tk.load_ticket(first["id"]), tk.list_tickets(), NOW, lease_hours=2)
    second = tk.load_ticket(second["id"])
    assert areas.blocked_reason(second, tk.list_tickets(), NOW + timedelta(hours=1))
    assert areas.blocked_reason(second, tk.list_tickets(), NOW + timedelta(hours=3)) is None
    areas.acquire(second, tk.list_tickets(), NOW + timedelta(hours=3))   # takes it over
    assert set(areas._read()) == {second["id"]}


def test_running_agent_renews_and_a_dead_one_expires(projects, monkeypatch):
    make_repo(projects)
    t = mk(branch="task/first", areas=["fleet/a.py"])
    claim(t)
    areas.acquire(tk.load_ticket(t["id"]), tk.list_tickets(), NOW, lease_hours=2)
    areas.sweep(tk.list_tickets(), {t["id"].lower()}, NOW + timedelta(hours=1), 2)   # renewed to +3h
    assert areas.sweep(tk.list_tickets(), frozenset(), NOW + timedelta(hours=2, minutes=30), 2) == []
    assert areas.sweep(tk.list_tickets(), None, NOW + timedelta(hours=4), 2) == [t["id"]]
    assert areas._read() == {}


def test_review_ticket_keeps_its_lock(projects, monkeypatch):
    make_repo(projects)
    t = mk(branch="task/first", areas=["fleet/a.py"])
    claim(t)
    areas.acquire(tk.load_ticket(t["id"]), tk.list_tickets(), NOW, lease_hours=1)
    review = {**tk.load_ticket(t["id"]), "status": "review"}
    assert areas.sweep([review], frozenset(), NOW + timedelta(hours=5), 1) == []


def test_cycle_renews_locks_of_running_agents(projects, monkeypatch):
    # the cycle must ask herdr by itself: no on-call ticket is waiting here
    from fleet import dispatch_cli as dc
    make_repo(projects)
    t = mk(branch="task/first", areas=["fleet/a.py"])
    claim(t)
    areas.acquire(tk.load_ticket(t["id"]), tk.list_tickets(), datetime.now(timezone.utc) - timedelta(hours=5), lease_hours=6)
    monkeypatch.setattr(dc.lc, "list_agents", lambda: [{"name": t["id"].lower(), "agent_status": "working"}])
    dc._cycle(settings(), lambda m: None, lambda m: None)
    lease = datetime.strptime(areas._read()[t["id"]]["lease_until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert lease > datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def test_human_release(projects, monkeypatch):
    make_repo(projects)
    t = mk(branch="task/first", areas=["fleet/a.py"])
    claim(t)
    areas.acquire(tk.load_ticket(t["id"]), tk.list_tickets(), NOW)
    as_agent(monkeypatch)
    out = run("release-area", t["id"])
    assert out.returncode == 1 and "only a human" in out.stderr and t["id"] in areas._read()
    as_human(monkeypatch)
    out = run("release-area", t["id"])
    assert out.returncode == 0 and areas._read() == {}


# --- claim-area ---------------------------------------------------------------------------------

def test_claim_area_files_a_ticket_and_names_who_is_ahead(projects, monkeypatch):
    first = run("claim-area", "--repo", "app", "--branch", "task/one", "--title", "One",
                "--body", "Work on notes.", "--area", "fleet/notes.py")
    assert first.returncode == 0, first.stderr
    one = first.stdout.strip()
    second = run("claim-area", "--repo", "app", "--branch", "task/two", "--title", "Two",
                 "--body", "Also notes.", "--area", "fleet/*.py", "--area", "docs/")
    assert second.returncode == 0, second.stderr
    assert f"queues behind {one}" in second.stderr
    t = tk.load_ticket(second.stdout.strip())
    assert t["areas"] == ["fleet/*.py", "docs/"] and t["type"] == "build"
    assert run("claim-area", "--repo", "app", "--branch", "task/three", "--title", "Three",
               "--body", "x").returncode == 2   # --area is required


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "~/x", "a\\b", " x", "", "./fleet/a.py", "fleet//a.py", "/", "fleet/**.py"])
def test_bad_areas_are_refused(projects, bad):
    out = run("new", "--type", "build", "--repo", "app", "--branch", "task/x", "--title", "T",
              "--body", "b", "--area", bad)
    assert out.returncode == 2 and "area" in out.stderr


# --- 23: a chat with no ticket cannot edit a mapped repo worktree [U feasibility] ---------------

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=a@b", "-c", "user.name=n", *args],
                   check=True, capture_output=True)


@pytest.fixture
def mapped(env, monkeypatch):
    """A repo called demo with a task worktree, and a ticket store that knows the repo."""
    repo = env / "demo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "worktree", "add", "-q", "-b", "task/mine", str(repo / ".worktrees" / "mine"))
    as_human(monkeypatch)
    t = tk.create_ticket({"type": "build", "repo": "demo", "branch": "task/elsewhere", "title": "T", "body": "b"})
    return repo / ".worktrees" / "mine", t


def edit(path):
    return run_hook("guard_edits.py", {"file_path": str(path / "f.txt"), "new_string": "y"}, path)


def switch(env, on):
    (env / "fleet").mkdir(exist_ok=True)
    (env / "fleet" / "settings.json").write_text(json.dumps({"require_ticket_for_edits": on}))


def test_chat_without_a_ticket_cannot_edit(env, mapped):
    wt, _ = mapped
    assert edit(wt) is None              # the switch is off by default
    switch(env, False)
    assert edit(wt) is None
    switch(env, True)
    reason = edit(wt)
    assert reason and "claim-area" in reason and "task/mine" in reason


def test_a_ticket_on_the_branch_allows_edits(env, mapped, monkeypatch):
    wt, _ = mapped
    switch(env, True)
    t = tk.create_ticket({"type": "build", "repo": "demo", "branch": "task/mine", "title": "Mine", "body": "b"})
    assert edit(wt) is not None          # open is not enough: nobody holds the branch yet
    tk.set_status(t["id"], "claimed", allowed_from=("open",), assignee="claude")
    assert edit(wt) is None
    tk.set_status(t["id"], "failed", allowed_from=("claimed",))
    assert edit(wt) is not None


def test_an_unmapped_repo_is_left_alone(env, mapped):
    wt, _ = mapped
    switch(env, True)
    other = env / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    (other / "f.txt").write_text("x")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "init")
    _git(other, "worktree", "add", "-q", "-b", "task/y", str(other / ".worktrees" / "y"))
    assert edit(other / ".worktrees" / "y") is None
