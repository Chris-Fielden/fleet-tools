"""BD-127: a human-built ticket reaches review through `ticket submit` (never a hand-set
status), and a repo that cannot take work pings a human once per cause."""
import json

import pytest

from fleet import notify
from fleet import review_watch as rw
from fleet import ticket as tk
from fleet.dispatch_cli import main as dispatch_main
from fleet.ticket_cli import main as ticket_main
from test_dispatch import calls, git, make_repo, mk, projects  # noqa: F401
from test_review_watch import ntfy, no_dialog, rig  # noqa: F401
from test_dispatch import herdr_log  # noqa: F401


def human_built(rig):
    """The rig's ticket as a human leaves it: open, no assignee, no agent in herdr."""
    rig.edit(status="open", assignee=None, created_by="human")
    rig.no_agent()


# --- (15) commits and no agent reach review without a hand-set status -------------------

def test_a_submitted_human_ticket_reaches_review(rig, ntfy, capsys):
    human_built(rig)
    assert ticket_main(["submit", rig.id]) == 0
    t = rig.get()
    assert t["status"] == "claimed" and t["assignee"] is None
    assert t["notes"][-1]["text"].startswith(tk.SUBMIT_NOTE)
    rig.cycle()
    assert rig.get()["status"] == "claimed"   # no passing review of this tip yet
    rig.verdict("PASS", "Codex")
    rig.verdict("PASS", "Claude (opus)")
    rig.cycle()
    assert rig.get()["status"] == "review"


def test_an_unsubmitted_human_ticket_is_left_alone(rig, ntfy):
    human_built(rig)
    rig.verdict("PASS", "Codex")
    rig.cycle()
    assert rig.get()["status"] == "open"


def test_submit_needs_commits_beyond_main(projects, env, capsys):
    repo = make_repo(projects)
    git(repo, "branch", "task/empty")
    t = mk(branch="task/empty")
    assert ticket_main(["submit", t["id"]]) != 0
    assert "nothing to review" in capsys.readouterr().err
    assert tk.load_ticket(t["id"])["status"] == "open"


def test_submit_refuses_a_ticket_with_an_agent(rig, capsys):
    rig.edit(created_by="human")   # claimed by claude
    assert ticket_main(["submit", rig.id]) != 0
    assert "has an agent" in capsys.readouterr().err


# --- (16) review stays with the dispatcher and humans ------------------------------------

@pytest.mark.parametrize("var", ["CLAUDECODE", "CODEX_SANDBOX"])
def test_an_agent_cannot_submit_or_set_review(rig, monkeypatch, capsys, var):
    human_built(rig)
    monkeypatch.setenv(var, "1")
    assert ticket_main(["submit", rig.id]) != 0
    assert "only a human" in capsys.readouterr().err
    with pytest.raises(tk.TicketError):
        tk.set_status(rig.id, "review", allowed_from=("open", "claimed"))
    assert rig.get()["status"] == "open"


# --- (29) a git switch in the main checkout: one ping, no action -------------------------

def test_a_repo_on_another_branch_pings_once(projects, env, ntfy, herdr_log, capsys):
    repo = make_repo(projects)
    mk()
    git(repo, "checkout", "-q", "-b", "other")
    for _ in range(3):
        dispatch_main(["--once"])
    requests = [r["body"] for r in ntfy.requests]
    assert len(requests) == 1
    assert "is on other, not main or master" in requests[0]["message"]
    assert requests[0]["priority"] == 2
    assert git(repo, "branch", "--show-current").strip() == "other"   # nothing was switched
    git(repo, "checkout", "-q", "main")
    dispatch_main(["--once"])   # clears the cause ...
    git(repo, "checkout", "-q", "other")
    dispatch_main(["--once"])   # ... so it pings again
    assert len(ntfy.requests) == 2


def test_a_stale_submitted_ticket_asks_a_human_not_an_agent(rig, ntfy):
    human_built(rig)
    assert ticket_main(["submit", rig.id]) == 0
    rig.verdict("PASS", "Codex")
    rig.verdict("PASS", "Claude (opus)")
    (rig.repo / "first.txt").write_text("main moved")
    git(rig.repo, "add", "-A")
    git(rig.repo, "commit", "-q", "-m", "main moves on")
    rig.cycle()
    rig.cycle(5)
    assert rig.get()["status"] == "claimed" and not rig.warnings
    assert any("needs a rebase by a human" in line for line in rig.lines)
    needs = [r for r in ntfy.requests if "needs a human" in r["body"]["title"]]
    assert len(needs) == 1 and "rebase" in needs[0]["body"]["message"]


def test_a_failed_ping_is_retried_and_a_new_cause_pings_again(projects, env, ntfy, herdr_log, monkeypatch):
    repo = make_repo(projects)
    mk()
    git(repo, "checkout", "-q", "-b", "other")
    real = notify.notify_quietly
    monkeypatch.setattr(notify, "notify_quietly", lambda *a, **k: False)
    dispatch_main(["--once"])
    dispatch_main(["--once"])
    assert ntfy.requests == []
    monkeypatch.setattr(notify, "notify_quietly", real)
    dispatch_main(["--once"])
    assert len(ntfy.requests) == 1
    git(repo, "checkout", "-q", "main")
    (repo / "README").write_text("changed")
    dispatch_main(["--once"])
    dispatch_main(["--once"])
    assert len(ntfy.requests) == 2
    assert "uncommitted changes" in ntfy.requests[1]["body"]["message"]


def test_a_new_commit_sends_a_submitted_ticket_back_without_starting_an_agent(rig, ntfy, herdr_log):
    human_built(rig)
    assert ticket_main(["submit", rig.id]) == 0
    rig.verdict("PASS", "Codex")
    rig.verdict("PASS", "Claude (opus)")
    rig.cycle()
    assert rig.get()["status"] == "review"
    rig.commit("more")
    rig.cycle(1)
    rw.wait_rereviews()   # the re-review cannot run in this rig
    rig.cycle(2)
    rig.cycle(40)
    assert rig.get()["status"] == "claimed"
    assert not rig.warnings
    assert not [c for c in calls(herdr_log) if "start" in json.dumps(c)]
    assert rw.pending_recoveries(print) == []


def test_no_agent_is_ever_started_for_a_ticket_without_one(rig, ntfy, herdr_log, monkeypatch):
    from fleet import launch as lc
    from fleet import supervisor_run as sr
    human_built(rig)
    assert ticket_main(["submit", rig.id]) == 0
    seen = []
    monkeypatch.setattr(sr, "start_next", lambda candidates, *a, **k: seen.append([t["id"] for t in candidates]))
    rig.verdict("FAIL", "Codex")
    rig.cycle()
    rig.cycle(30)
    assert seen and all(ids == [] for ids in seen)
    code, message = lc.relaunch(rig.get(), rig.settings, print)
    assert code == 1 and "no agent assigned" in message
    assert not [c for c in calls(herdr_log) if "start" in json.dumps(c)]


def test_a_rebase_ping_that_failed_is_retried(rig, ntfy, monkeypatch):
    human_built(rig)
    assert ticket_main(["submit", rig.id]) == 0
    rig.verdict("PASS", "Codex")
    rig.verdict("PASS", "Claude (opus)")
    (rig.repo / "first.txt").write_text("main moved")
    git(rig.repo, "add", "-A")
    git(rig.repo, "commit", "-q", "-m", "main moves on")
    real = notify.notify_quietly
    monkeypatch.setattr(notify, "notify_quietly", lambda *a, **k: False)
    rig.cycle()
    rig.cycle(5)
    assert ntfy.requests == []
    monkeypatch.setattr(notify, "notify_quietly", real)
    rig.cycle(10)
    assert len([r for r in ntfy.requests if "needs a human" in r["body"]["title"]]) == 1
