"""BD-117 (rebuild of BD-116): a failure that needs a person gets no on-call agent,
and a ticket whose dependency branch has no commits is held, with one ping."""
import json

from fleet import dispatch as dp
from fleet import dispatch_cli
from fleet import review_watch as rw
from fleet import ticket as tk
from test_dispatch import git, make_repo, mk, projects, herdr_log  # noqa: F401  (fixtures)
from test_oncall import go, oncall_tickets, world
from test_review_watch import no_dialog, ntfy, rig  # noqa: F401  (fixtures)


def note(by, text):
    return {"at": "2026-10-15T00:00:00Z", "by": by, "text": text}


# --- part 1: no on-call for a failure only a person can fix -------------------

def test_agent_note_in_the_standard_phrase_needs_a_human():
    assert rw.needs_human_first({"notes": [note("claude", "Needs a human: cancel them")]})


def test_a_real_stall_is_not_needs_human():
    assert not rw.needs_human_first({"notes": [note("claude", "working on the parser")]})
    assert not rw.needs_human_first({"notes": []})


def test_last_agent_note_decides():
    t = {"notes": [note("claude", "BLOCKED: needs the real store"), note("human", "Needs a human: x")]}
    assert rw.needs_human_first(t)
    t = {"notes": [note("claude", "needs a human"), note("claude", "fixed it, continuing")]}
    assert not rw.needs_human_first(t)


def test_stalled_ticket_with_needs_human_note_files_no_oncall(rig, ntfy, herdr_log, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    tk.add_note(rig.id, "SUMMARY: BLOCKED. Needs a human: cancel tickets in the real store.")
    monkeypatch.delenv("CLAUDECODE")
    world(rig, {"name": rig.id.lower(), "agent_status": "idle", "revision": 1})
    go(rig, 15, 5)
    got = rig.get()
    assert got["status"] == "failed"
    assert oncall_tickets() == []
    assert any(n["text"].startswith("Needs a human:") for n in got["notes"])
    assert [t[0] for t in no_dialog(ntfy)] == [f"{rig.id} needs a human, see notes"]


# --- part 2: an empty dependency holds the ticket ----------------------------

def dep_ticket(**over):
    return mk(branch="task/restore-test", body="Risk: low. Depends on task/backup-script. Do it.", **over)


def test_dependency_with_no_commits_is_held(projects, herdr_log):
    repo = make_repo(projects, "app")
    git(repo, "branch", "task/backup-script")
    t = dep_ticket()
    (d,) = dp.decide(tk.list_tickets(), dp.load_settings())
    assert d.ticket["id"] == t["id"] and not d.eligible
    assert d.skip.startswith(dp.UNMET_SKIP) and "task/backup-script" in d.skip


def test_dependency_with_a_commit_launches(projects, herdr_log):
    repo = make_repo(projects, "app")
    git(repo, "checkout", "-q", "-b", "task/backup-script")
    (repo / "b.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "backup")
    git(repo, "checkout", "-q", "main")
    dep_ticket()
    (d,) = dp.decide(tk.list_tickets(), dp.load_settings())
    assert d.eligible


def test_missing_dependency_branch_is_not_held(projects, herdr_log):
    make_repo(projects, "app")
    dep_ticket()
    (d,) = dp.decide(tk.list_tickets(), dp.load_settings())
    assert d.eligible


def test_held_ticket_gets_one_note_and_one_ping(projects, herdr_log, monkeypatch):
    repo = make_repo(projects, "app")
    git(repo, "branch", "task/backup-script")
    t = dep_ticket()
    sent = []
    monkeypatch.setattr(dispatch_cli.notify, "notify_quietly",
                        lambda tk_, event, warn=None, detail=None, **kw: sent.append((tk_["id"], event)) or True)
    for _ in range(3):
        dispatch_cli._cycle(dp.load_settings(), lambda m: None, lambda m: None)
    assert sent == [(t["id"], "needs_human")]
    held = [n for n in tk.load_ticket(t["id"])["notes"] if n["text"].startswith(dp.HELD_NOTE)]
    assert len(held) == 1
    assert tk.load_ticket(t["id"])["status"] == "open"
