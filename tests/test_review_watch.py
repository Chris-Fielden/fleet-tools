"""In Review on a recorded PASS, and the failure / blocked / stall pings, as
`dispatch --watch` does them (fake herdr, fake ntfy, temp FLEET_HOME, real git)."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from conftest import run
from fake_kaneo import FakeKaneo
from fleet import dispatch as dp
from fleet import notify
from fleet import review_watch as rw
from fleet import ticket as tk
from test_dispatch import calls, claim, git, herdr_log, make_repo, mk, projects  # noqa: F401
from test_kaneo import API_KEY, write_config as write_kaneo_config
from test_notify import FakeNtfy, write_config as write_ntfy_config
from test_watch import FakeClock, watch

TIP_FILE = "work.txt"


@pytest.fixture
def ntfy():
    fake = FakeNtfy()
    write_ntfy_config(fake.url)
    yield fake
    fake.stop()


class Rig:
    """One claimed build ticket on a real branch with a task folder, and a fake
    herdr whose one agent is named after the ticket."""

    def __init__(self, projects, env, monkeypatch):
        self.env = env
        self.monkeypatch = monkeypatch
        self.repo = make_repo(projects)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        (self.repo / ".gitignore").write_text(".worktrees/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "ignore")
        self.wt = self.repo / ".worktrees" / "demo"
        git(self.repo, "worktree", "add", "-q", str(self.wt), "-b", "task/demo")
        self.ticket = claim(mk(branch="task/demo"))
        self.id = self.ticket["id"]
        self.world = env / "world.json"
        monkeypatch.setenv("FAKE_HERDR_WORLD", str(self.world))
        self.set_agent("working")
        self.commit("first")
        self.settings = dp.load_settings()
        self.now = datetime.now(timezone.utc)
        self.lines, self.warnings = [], []

    # git
    def commit(self, name):
        (self.wt / f"{name}.txt").write_text(name)
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", name)
        return self.tip()

    def tip(self):
        return git(self.repo, "rev-parse", "task/demo").strip()

    # herdr
    def set_agent(self, status, **extra):
        agent = {"name": self.id.lower(), "agent_status": status, **extra}
        self.world.write_text(json.dumps({"workspaces": [], "tabs": [], "agents": [agent]}))

    def no_agent(self):
        self.world.write_text(json.dumps({"workspaces": [], "tabs": [], "agents": []}))

    # ticket
    def verdict(self, result="PASS", reviewer="Codex", commit=None):
        tk.record_verdict("task/demo", "app", result, commit or self.tip(), reviewer)

    def summary(self, text="All done.", by="claude"):
        """A note as the agent writes it (`ticket note` run inside its session)."""
        for var in ("CLAUDECODE", "CODEX_SANDBOX"):
            self.monkeypatch.delenv(var, raising=False)
        self.monkeypatch.setenv("CLAUDECODE" if by == "claude" else "CODEX_SANDBOX", "1")
        try:
            tk.add_note(self.id, text)
        finally:
            self.monkeypatch.delenv("CLAUDECODE" if by == "claude" else "CODEX_SANDBOX", raising=False)

    def get(self):
        return tk.load_ticket(self.id)

    def edit(self, **changes):
        path = tk.tickets_dir() / f"{self.id}.json"
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))

    def finish(self):
        """What a finished agent leaves behind: a recorded PASS, then its summary, then idle."""
        self.verdict()
        self.summary()
        self.set_agent("idle")

    def review_file(self, kind="Codex", verdict="PASS", age=600):
        d = self.wt / ".reviews"
        d.mkdir(exist_ok=True)
        path = d / f"{kind.lower()}.md"
        path.write_text(f"Reviewer: {kind}\nCommit: {self.tip()}\n\nVERDICT: {verdict}\n")
        stamp = self.now.timestamp() - age
        os.utime(path, (stamp, stamp))

    # one watch pass
    def cycle(self, minutes=0):
        rw.check(self.settings, self.now + timedelta(minutes=minutes), self.lines.append, self.warnings.append)


@pytest.fixture
def rig(projects, env, herdr_log, monkeypatch):
    return Rig(projects, env, monkeypatch)


def sent(ntfy):
    return [(r["body"]["title"], r["body"]["message"], r["priority"]) for r in ntfy.requests]


# --- ready to ship -----------------------------------------------------------

def test_pass_summary_and_idle_agent_moves_to_review_with_one_ping(rig, ntfy):
    rig.finish()
    rig.cycle()
    assert rig.get()["status"] == "review"
    assert len(ntfy.requests) == 1
    title, message, header = sent(ntfy)[0]
    assert title == f"{rig.id} ready to ship" and header == "default"
    assert "medium risk" in message and "T" in message
    assert ntfy.requests[0]["body"]["priority"] == 3
    rig.cycle(5)
    rig.cycle(10)
    assert len(ntfy.requests) == 1 and rig.get()["status"] == "review"


def test_done_agent_counts_like_idle(rig, ntfy):
    rig.finish()
    rig.set_agent("done")
    rig.cycle()
    assert rig.get()["status"] == "review"


@pytest.mark.parametrize("state", ["working", "blocked", "unknown"])
def test_a_busy_agent_is_not_ready(rig, ntfy, state):
    rig.finish()
    rig.set_agent(state)
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_a_vanished_agent_is_not_ready(rig, ntfy):
    rig.finish()
    rig.no_agent()
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_no_summary_note_means_not_ready(rig, ntfy):
    rig.verdict()
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


def test_a_summary_older_than_the_review_does_not_count(rig, ntfy):
    rig.summary()
    rig.verdict()
    rig.set_agent("idle")
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    verdict = rig.get()["verdict"]
    verdict["at"] = later
    rig.edit(verdict=verdict)
    rig.cycle()
    assert rig.get()["status"] == "claimed"


def test_the_leftovers_note_is_not_a_summary(rig, ntfy):
    rig.verdict()
    rig.summary(f"{rw.LEFTOVERS_PREFIX} the Codex review of abc1234 (PASS):\n1. rename x")
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed"


def test_a_note_by_someone_else_is_not_the_agents_summary(rig, ntfy):
    rig.verdict()
    rig.summary(by="codex")   # the agent is claude
    tk.add_note(rig.id, "a human remark")
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed"


def test_a_summary_claiming_a_pass_is_not_a_pass(rig, ntfy):
    """The agent says both reviewers passed; the ticket records nothing."""
    rig.summary("Both reviewers passed. Review verdict: PASS.")
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed" and ntfy.requests == []


@pytest.mark.parametrize("result", ["FAIL"])
def test_a_fail_is_not_ready(rig, ntfy, result):
    rig.verdict(result)
    rig.summary()
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed"


def test_a_pass_of_an_older_commit_is_not_ready(rig, ntfy):
    rig.verdict()
    rig.commit("later")
    rig.summary()
    rig.set_agent("idle")
    rig.cycle()
    assert rig.get()["status"] == "claimed"


def test_high_risk_needs_the_gate_not_just_one_pass(rig, ntfy):
    rig.edit(risk="high")
    rig.finish()
    rig.cycle()
    assert rig.get()["status"] == "claimed"
    rig.verdict("PASS", "Claude (opus)")
    rig.summary("second review in")
    rig.cycle(1)
    assert rig.get()["status"] == "review"


def test_back_to_claimed_when_the_tip_moves_or_the_agent_is_busy(rig, ntfy):
    rig.finish()
    rig.cycle()
    assert rig.get()["status"] == "review"
    rig.set_agent("working")
    rig.cycle(1)
    assert rig.get()["status"] == "claimed"
    rig.set_agent("idle")
    rig.cycle(2)
    assert rig.get()["status"] == "review"
    assert len(ntfy.requests) == 1   # the same commit never pings twice
    rig.commit("more")
    rig.cycle(3)
    assert rig.get()["status"] == "claimed"
    rig.verdict()
    rig.summary("new summary")
    rig.cycle(4)
    assert rig.get()["status"] == "review"
    assert len(ntfy.requests) == 2   # a new commit pings again


def test_review_is_kept_when_herdr_cannot_be_asked(rig, ntfy, monkeypatch):
    rig.finish()
    rig.cycle()
    monkeypatch.setenv("FLEET_HERDR", str(rig.env / "no-such-herdr"))
    rig.cycle(1)
    assert rig.get()["status"] == "review"
    assert any("cannot check agents" in w for w in rig.warnings)


def test_an_agent_can_never_set_review(rig, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(tk.TicketError, match="only the dispatcher"):
        tk.set_status(rig.id, "review", allowed_from=("claimed",))
    assert rig.get()["status"] == "claimed"


def test_a_review_ticket_still_holds_the_capacity_and_the_branch(rig):
    rig.finish()
    rig.cycle()
    other = mk(branch="task/other")
    decision = dp.decide(tk.list_tickets(), rig.settings)[0]
    assert decision.ticket["id"] == other["id"] and decision.skip == "at capacity"


def test_review_queue_lists_oldest_first(rig):
    second = claim(mk(branch="task/two"))
    for t in (rig.ticket, second):
        tk.set_status(t["id"], "review", allowed_from=("claimed",))
    out = run("list", "--status", "review").stdout.split("\n")
    assert [line.split()[0] for line in out if line] == [rig.id, second["id"]]


def test_a_review_ticket_is_closed_done_when_merged(rig):
    rig.finish()
    rig.cycle()
    git(rig.repo, "merge", "-q", "--no-ff", "-m", "Merge task/demo", "task/demo")
    done = list(dp.reconcile(tk.list_tickets(), rig.settings))
    assert [t["id"] for t in done] == [rig.id] and rig.get()["status"] == "done"


def test_the_kaneo_card_moves_to_in_review_and_gets_the_label(rig, ntfy):
    kaneo = FakeKaneo(API_KEY).start()
    try:
        write_kaneo_config(kaneo.url)
        rig.finish()
        rig.cycle()
        task_id = next(iter(kaneo.tasks))
        card = kaneo.tasks[task_id]
        assert card["status"] == "in-review"
        assert "Ready to ship" in kaneo.label_names(task_id)
        rig.set_agent("working")
        rig.cycle(1)
        assert kaneo.tasks[task_id]["status"] == "in-progress"
        assert "Ready to ship" not in kaneo.label_names(task_id)
    finally:
        kaneo.stop()


def test_an_urgent_ticket_leaves_the_urgent_column_once_claimed(projects, env, herdr_log):
    kaneo = FakeKaneo(API_KEY).start()
    try:
        write_kaneo_config(kaneo.url)
        t = tk.create_ticket({"type": "build", "repo": "app", "branch": "task/hot", "title": "Hot",
                              "body": "B", "priority": "urgent"})
        assert run("sync").returncode == 0
        card = next(iter(kaneo.tasks.values()))
        assert card["status"] == "urgent"
        claim(t)
        assert run("sync").returncode == 0
        assert next(iter(kaneo.tasks.values()))["status"] == "in-progress"
        tk.set_status(t["id"], "review", allowed_from=("claimed",))
        assert run("sync").returncode == 0
        assert next(iter(kaneo.tasks.values()))["status"] == "in-review"
    finally:
        kaneo.stop()


# --- failure pings -------------------------------------------------------------

def test_a_recorded_fail_pings_high_and_names_the_reviewer_once(rig, ntfy):
    rig.verdict("FAIL", "Codex")
    rig.cycle()
    rig.cycle(5)
    assert len(ntfy.requests) == 1
    title, message, header = sent(ntfy)[0]
    assert title == f"Review FAILED: {rig.id}" and "Codex" in message and header == "high"
    assert ntfy.requests[0]["body"]["priority"] == 4
    rig.verdict("FAIL", "Claude (opus)")   # the second reviewer fails too: new information
    rig.cycle(10)
    assert len(ntfy.requests) == 2 and "Claude (opus)" in sent(ntfy)[1][1] and "Codex" not in sent(ntfy)[1][1]


def test_a_fail_of_an_old_commit_is_not_pinged(rig, ntfy):
    rig.verdict("FAIL")
    rig.commit("fix")
    rig.cycle()
    assert ntfy.requests == []


def test_a_review_saved_but_not_recorded_pings_high_once(rig, ntfy):
    rig.review_file("Codex", "PASS", age=600)
    rig.cycle()
    rig.cycle(5)
    assert len(ntfy.requests) == 1
    title, message, header = sent(ntfy)[0]
    assert title == f"Review not recorded: {rig.id}" and header == "high" and "Codex" in message


def test_a_review_still_inside_its_grace_period_is_not_pinged(rig, ntfy):
    rig.review_file("Codex", "PASS", age=10)
    rig.cycle()
    assert ntfy.requests == []


def test_a_recorded_review_is_not_pinged(rig, ntfy):
    rig.review_file("Codex", "PASS")
    rig.verdict("PASS", "Codex")
    rig.cycle()
    assert ntfy.requests == []


def test_one_reviewer_recorded_and_the_other_not(rig, ntfy):
    rig.review_file("Codex", "PASS")
    rig.review_file("Claude", "PASS")
    rig.verdict("PASS", "Codex")
    rig.cycle()
    assert len(ntfy.requests) == 1 and "Claude" in sent(ntfy)[0][1]


def test_a_review_file_without_a_verdict_cannot_be_recorded(rig, ntfy):
    rig.review_file("Codex", "PASS")
    path = rig.wt / ".reviews" / "codex.md"
    path.write_text(f"Reviewer: Codex\nCommit: {rig.tip()}\n\nno verdict here\n")
    os.utime(path, (1, 1))
    rig.cycle()
    assert len(ntfy.requests) == 1 and "no verdict" in sent(ntfy)[0][1]


# --- blocked and stalled -------------------------------------------------------

def test_blocked_over_ten_minutes_pings_high_once_and_rearms(rig, ntfy):
    rig.set_agent("blocked")
    rig.cycle(0)
    rig.cycle(9)
    assert ntfy.requests == []
    rig.cycle(11)
    rig.cycle(20)
    assert len(ntfy.requests) == 1
    title, _, header = sent(ntfy)[0]
    assert title == f"Agent blocked: {rig.id}" and header == "high"
    rig.set_agent("working")
    rig.cycle(21)
    rig.set_agent("blocked")
    rig.cycle(22)
    rig.cycle(33)
    assert len(ntfy.requests) == 2


def test_stalled_for_thirty_minutes_pings_default_once(rig, ntfy):
    rig.set_agent("working", revision=1)
    rig.cycle(0)
    rig.cycle(29)
    assert ntfy.requests == []
    rig.cycle(31)
    rig.cycle(45)
    assert len(ntfy.requests) == 1
    title, message, header = sent(ntfy)[0]
    assert title == f"Ticket stalled: {rig.id}" and header == "default" and "31 min" in message


def test_herdr_activity_resets_the_stall_clock(rig, ntfy):
    rig.set_agent("working", revision=1)
    rig.cycle(0)
    rig.set_agent("working", revision=2)
    rig.cycle(20)
    rig.cycle(40)
    assert ntfy.requests == []
    rig.cycle(51)
    assert len(ntfy.requests) == 1


def test_a_note_or_a_commit_resets_the_stall_clock(rig, ntfy):
    rig.set_agent("working", revision=1)
    rig.cycle(0)
    # a note saved "20 minutes from now" is the latest sign of life
    when = (rig.now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rig.edit(notes=[{"at": when, "by": "claude", "text": "progress"}])
    rig.cycle(40)
    assert ntfy.requests == []
    rig.cycle(51)
    assert len(ntfy.requests) == 1


def test_dispatcher_log_lines_for_the_ticket_count_but_repeating_ones_do_not(rig, ntfy):
    rig.set_agent("working", revision=1)
    rig.cycle(0)
    dp.log("RECONCILE_SKIP", f"{rig.id} task/demo not merged")   # logged every cycle: no sign of life
    rig.cycle(31)
    assert len(ntfy.requests) == 1


def test_a_ticket_in_review_is_never_called_stalled(rig, ntfy):
    rig.finish()
    rig.cycle(0)
    rig.cycle(120)
    assert [t[0] for t in sent(ntfy)] == [f"{rig.id} ready to ship"]


# --- never twice, state and failures ---------------------------------------------

def test_a_restarted_watcher_does_not_repeat_pings(rig, ntfy):
    rig.verdict("FAIL")
    rig.cycle()
    assert (dp.tk.fleet_home() / "review-watch.json").is_file()
    rig.cycle(5)   # same state file, as after a restart
    assert len(ntfy.requests) == 1


def test_a_failed_send_is_retried_and_not_remembered(rig):
    fake = FakeNtfy(status=500)
    write_ntfy_config(fake.url)
    try:
        rig.verdict("FAIL")
        rig.cycle()
        assert rig.warnings and len(fake.requests) == 1
        fake.status = 200
        rig.cycle(1)
        rig.cycle(2)
        assert len(fake.requests) == 2
    finally:
        fake.stop()


def test_a_failed_ready_to_ship_ping_is_retried_while_in_review(rig):
    fake = FakeNtfy(status=500)
    write_ntfy_config(fake.url)
    try:
        rig.finish()
        rig.cycle()
        assert rig.get()["status"] == "review" and len(fake.requests) == 1
        fake.status = 200
        rig.cycle(1)
        rig.cycle(2)
        assert len(fake.requests) == 2   # one retry that worked, then never again
    finally:
        fake.stop()


def test_a_git_timeout_on_one_ticket_still_saves_what_was_sent(rig, ntfy, monkeypatch):
    rig.verdict("FAIL")
    other = claim(mk(branch="task/other"))
    real = rw._tip

    def tip(repo, branch):
        if branch == "task/other":
            raise rw.subprocess.TimeoutExpired("git", 60)
        return real(repo, branch)
    monkeypatch.setattr(rw, "_tip", tip)
    rig.cycle()
    rig.cycle(1)
    assert len(ntfy.requests) == 1 and any(other["id"] in w for w in rig.warnings)


def test_keys_of_finished_tickets_are_dropped(rig, ntfy):
    rig.verdict("FAIL")
    rig.cycle()
    tk.set_status(rig.id, "done", allowed_from=("claimed",))
    rig.cycle(1)
    state = json.loads(rw.state_path().read_text())
    assert state["sent"] == []


def test_nothing_happens_without_notify_config(rig):
    rig.verdict("FAIL")
    rig.cycle()
    assert rig.warnings == []


def test_unreadable_state_starts_fresh_with_a_warning(rig, ntfy):
    path = tk.fleet_home() / "review-watch.json"
    path.write_text("{nope")
    rig.verdict("FAIL")
    rig.cycle()
    assert any("starting with an empty one" in w for w in rig.warnings) and len(ntfy.requests) == 1


def test_messages_carry_no_body_or_notes(rig, ntfy):
    rig.edit(body="SECRET BODY")
    rig.summary("SECRET NOTE")
    rig.verdict("FAIL")
    rig.cycle()
    assert "SECRET" not in json.dumps([r["body"] for r in ntfy.requests])


# --- inside dispatch --watch ---------------------------------------------------

def test_watch_moves_the_ticket_and_says_so(rig, ntfy, capsys):
    rig.finish()
    assert watch({}, FakeClock(2)) == 0
    out = capsys.readouterr().out
    assert f"{rig.id}  ready to ship: moved to In Review" in out
    assert rig.get()["status"] == "review" and len(ntfy.requests) == 1
    log = dp.log_path().read_text()
    assert "REVIEW_READY" in log


# --- priorities in notify.json -------------------------------------------------------

@pytest.mark.parametrize("event, level", [
    ("urgent", "high"), ("failed", "high"), ("blocked", "high"), ("review_failed", "high"),
    ("review_unrecorded", "high"), ("ship", "default"), ("ready", "default"),
    ("stalled", "default"), ("proposed", "low"), ("investigated", "low")])
def test_default_priorities(event, level):
    assert notify.PRIORITIES[event] == level and set(notify.PRIORITIES) == set(notify.EVENTS)


def test_priorities_can_be_overridden(rig, ntfy):
    write_ntfy_config(ntfy.url, priorities={"review_failed": "max", "ship": "low"})
    rig.verdict("FAIL")
    rig.cycle()
    assert sent(ntfy)[0][2] == "max" and ntfy.requests[0]["body"]["priority"] == 5


@pytest.mark.parametrize("bad", [{"nonsense": "high"}, {"ship": "loud"}, {"ship": 4}, ["ship"],
                                  {"ship": ["high"]}, {"ship": {"a": 1}}, {"ship": None}])
def test_bad_priorities_are_refused(env, bad):
    write_ntfy_config("http://127.0.0.1:1", priorities=bad)
    with pytest.raises(notify.NotifyError):
        notify.load_config()


def test_investigation_done_pings_low(projects, herdr_log, ntfy, monkeypatch, env, capsys):
    from fleet import dispatch_cli
    t = tk.create_ticket({"type": "investigate", "repo": "app", "branch": "task/look", "title": "Look",
                          "body": "B"})
    tk.set_status(t["id"], "claimed", allowed_from=("proposed",), assignee="claude")
    tk.add_note(t["id"], "findings")
    world = env / "w2.json"
    world.write_text(json.dumps({"workspaces": [], "tabs": [], "agents": [
        {"name": t["id"].lower(), "agent_status": "idle"}]}))
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(world))
    dispatch_cli._cycle(dp.load_settings(), print, lambda m: None)
    assert tk.load_ticket(t["id"])["status"] == "done"
    assert [r["priority"] for r in ntfy.requests] == ["low"]
