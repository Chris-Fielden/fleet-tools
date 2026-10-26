"""BD-136: the Cursor reviewer inside the review watcher's cycle (fake CLI, fake herdr, real git)."""
import os

import pytest

import fake_cursor
from fleet import cursor_review as cr
from fleet import ticket as tk
from test_dispatch import calls, herdr_log, projects  # noqa: F401
from test_review_watch import ntfy, rig  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_jobs():
    cr.reset()
    yield
    cr.reset()


def ready(rig, monkeypatch):
    """The ticket has PASS verdicts of both reviewers, a summary, and an idle agent."""
    rig.verdict("PASS", "Codex")
    rig.verdict("PASS", "Claude (opus)")
    rig.summary()
    rig.set_agent("idle")


def cursor_setup(rig, monkeypatch, **cfg):
    fleet = tk.fleet_home()
    (fleet / "secrets").mkdir(parents=True, exist_ok=True)
    (fleet / "secrets" / "cursor-api-key").write_text("fake-key-aaaa-bbbb\n")
    bindir = fake_cursor.install(rig.env / "bin", needs_key=True, **cfg)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    rig.settings["cursor_review"] = True
    cr.probe_result_path().write_text(
        '{"cli_version": "STUB 1.2.3", "probes": {"a": "pass", "b": "pass", "c": "pass", "d": "pass", "e": "pass"}, "passed": true}')
    return bindir


def test_a_counted_cursor_fail_after_ready_sends_the_ticket_back(rig, ntfy, monkeypatch):
    cursor_setup(rig, monkeypatch, review="VERDICT: FAIL\nMUST FIX:\n1. bad\nSUMMARY: no")
    ready(rig, monkeypatch)
    rig.cycle(1)
    cr.wait()
    assert rig.get()["status"] == "review"           # ready before Cursor finished
    rig.cycle(2)                                     # takes up the counted FAIL in this very cycle
    t = rig.get()
    assert t["verdict"]["result"] == "FAIL" and t["verdict"]["reviews"]["cursor"]["counted"] is True
    assert t["status"] == "claimed"                  # judged on the new verdict, not the stale PASS


def test_a_counted_cursor_pass_leaves_the_ticket_ready(rig, ntfy, monkeypatch):
    cursor_setup(rig, monkeypatch)
    ready(rig, monkeypatch)
    rig.cycle(1)
    cr.wait()
    rig.cycle(2)
    rig.cycle(3)
    t = rig.get()
    assert t["status"] == "review" and t["verdict"]["reviews"]["cursor"]["result"] == "PASS"


def test_a_reviewed_again_after_the_probe_is_rerun(rig, ntfy, monkeypatch):
    bindir = cursor_setup(rig, monkeypatch)
    cr.probe_result_path().unlink()                  # no probe yet: the first review is not counted
    ready(rig, monkeypatch)
    rig.cycle(1)
    cr.wait()
    rig.cycle(2)
    assert "cursor" not in rig.get()["verdict"]["reviews"]
    assert len([c for c in fake_cursor.calls(bindir) if "--trust" in c["args"]]) == 1
    cursor_setup(rig, monkeypatch)                   # the human runs the probe: a newer result file
    saved = next((rig.wt / ".reviews").glob("*-cursor.md"))
    os.utime(cr.probe_result_path(), (saved.stat().st_mtime + 10, saved.stat().st_mtime + 10))
    for n in (3, 4, 5):
        rig.cycle(n)
        cr.wait()
    assert rig.get()["verdict"]["reviews"]["cursor"]["counted"] is True
    assert len([c for c in fake_cursor.calls(bindir) if "--trust" in c["args"]]) == 2
