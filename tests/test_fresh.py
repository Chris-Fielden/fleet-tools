"""A fresh agent session when the context is large (fake herdr, fixture session
sizes, temp FLEET_HOME, real git)."""
import json
import os

import pytest

from fleet import fresh
from fleet import ticket as tk
from test_dispatch import calls
from test_review_watch import Rig, herdr_log, projects, rig, ntfy  # noqa: F401


@pytest.fixture
def sizes(monkeypatch):
    """Fixture session sizes: set sizes['n'] to what the session measures (None = unknown)."""
    box = {"n": 150000}
    monkeypatch.setattr(fresh, "session_tokens", lambda t, folder: box["n"])
    return box


def tabbed(rig, status="working"):
    rig.world.write_text(json.dumps({
        "workspaces": [{"workspace_id": "w1", "label": "fleet"}],
        "tabs": [{"tab_id": "w1:t4", "label": rig.id, "workspace_id": "w1", "focused": False}],
        "agents": [{"name": rig.id.lower(), "agent_status": status, "tab_id": "w1:t4"}]}))


def prompts(herdr_log):
    return [c[3] for c in calls(herdr_log) if c[:2] == ["agent", "prompt"]]


def log_text():
    return (tk.fleet_home() / "dispatch.log").read_text() if (tk.fleet_home() / "dispatch.log").exists() else ""


def test_large_session_after_a_fail_is_replaced_with_the_full_context(rig, sizes, herdr_log):
    tk.add_note(rig.id, "Decided to keep the old format.")
    rig.review_file("Codex", "FAIL")
    (rig.wt / ".reviews" / "codex.md").write_text(
        f"Reviewer: Codex\nCommit: {rig.tip()}\n\nMUST FIX:\n- the widget leaks\n\nVERDICT: FAIL\n")
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle()
    got = calls(herdr_log)
    assert ["tab", "close", "w1:t4"] in got
    assert any(c[:2] == ["agent", "start"] for c in got)
    (prompt,) = prompts(herdr_log)
    assert rig.ticket["title"] in prompt and rig.ticket["body"] in prompt
    assert "Decided to keep the old format." in prompt
    assert "first.txt" in prompt                      # the diff summary
    assert "the widget leaks" in prompt               # the open MUST FIX item
    assert "FRESH_SESSION" in log_text()
    assert rig.get()["status"] == "claimed"


def test_each_failed_commit_is_handled_once(rig, sizes, herdr_log):
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle()
    tabbed(rig)
    rig.cycle(1)
    assert len(prompts(herdr_log)) == 1
    rig.commit("fix")
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle(2)
    assert len(prompts(herdr_log)) == 2


def test_small_session_is_left_alone_and_not_measured_again(rig, sizes, herdr_log):
    sizes["n"] = 100000     # at the limit is not over it
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle()
    sizes["n"] = 500000
    rig.cycle(1)
    assert prompts(herdr_log) == [] and "FRESH_SESSION" not in log_text()


def test_unknown_size_is_retried_next_cycle(rig, sizes, herdr_log):
    sizes["n"] = None
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle()
    assert prompts(herdr_log) == []
    sizes["n"] = 150000
    rig.cycle(1)
    assert len(prompts(herdr_log)) == 1


@pytest.mark.parametrize("limit", [0])
def test_zero_turns_it_off(rig, sizes, herdr_log, limit):
    rig.settings["fresh_session_tokens"] = limit
    rig.verdict("FAIL")
    tabbed(rig)
    rig.cycle()
    assert prompts(herdr_log) == []


def test_a_pass_or_a_missing_agent_never_replaces_anything(rig, sizes, herdr_log):
    rig.verdict("PASS")
    tabbed(rig)
    rig.cycle()
    rig.no_agent()
    rig.commit("again")
    rig.verdict("FAIL")
    rig.cycle(1)
    assert prompts(herdr_log) == []


def test_a_tab_that_cannot_be_closed_means_no_relaunch(rig, sizes, herdr_log):
    rig.verdict("FAIL")
    rig.set_agent("working")     # no tab id to close
    rig.cycle()
    assert prompts(herdr_log) == [] and rig.warnings


def test_a_failed_relaunch_is_noted_and_the_ticket_stays_claimed(rig, sizes, herdr_log, monkeypatch):
    rig.verdict("FAIL")
    tabbed(rig)
    monkeypatch.setattr(fresh.lc, "relaunch", lambda *a, **k: (1, "relaunch failed: boom"))
    rig.cycle()
    assert rig.get()["status"] == "claimed"
    assert any(n["text"].startswith(fresh.NOTE_PREFIX) for n in rig.get()["notes"])


def test_the_setting_is_validated(env):
    (tk.fleet_home()).mkdir(parents=True, exist_ok=True)
    (tk.fleet_home() / "settings.json").write_text('{"fresh_session_tokens": -1}')
    from fleet import settings as st
    with pytest.raises(st.SettingsError):
        st.load_settings()
    (tk.fleet_home() / "settings.json").write_text("{}")
    assert st.load_settings()["fresh_session_tokens"] == 100000


def test_claude_session_size_reads_the_authors_transcript_not_a_reviewers(env, monkeypatch):
    cfg = env / "cfg"
    folder = env / "my.proj" / "wt"
    folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    proj = cfg / "projects" / str(folder.resolve()).replace("/", "-").replace(".", "-").replace("_", "-")
    proj.mkdir(parents=True)
    usage = lambda i, c, r: json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": i, "cache_creation_input_tokens": c, "cache_read_input_tokens": r, "output_tokens": 9}}})
    first = lambda text: json.dumps({"type": "user", "message": {"role": "user", "content": text}})
    author = proj / "a.jsonl"
    author.write_text(first("You are working ticket EM-1 in this task folder.\nRun it.") + "\n" + usage(1, 2, 3)
                      + "\nnot json\n" + usage(10, 20000, 90000) + '\n{"type":"user"}\n')
    reviewer = proj / "r.jsonl"      # newer, small: a review run in the same folder
    reviewer.write_text(first("Review this branch.") + "\n" + usage(1, 2, 3) + "\n")
    os.utime(author, (1, 1))
    assert fresh.claude_session_tokens(folder, "EM-1") == 110010
    assert fresh.claude_session_tokens(folder, "EM-2") is None
    assert fresh.claude_session_tokens(env, "EM-1") is None
    taken = proj / "t.jsonl"         # a replacement session counts too, and is the newest
    taken.write_text(first("You are taking over ticket EM-1 in this task folder") + "\n" + usage(5, 0, 0) + "\n")
    assert fresh.claude_session_tokens(folder, "EM-1") == 5
    taken.unlink()
    oncall = proj / "o.jsonl"        # an on-call agent's prompt, after a malformed record
    oncall.write_text(json.dumps({"type": "user", "message": 7}) + "\n"
                      + first("You are the on-call agent for ticket EM-1, in this task folder.") + "\n"
                      + usage(7, 0, 0) + "\n")
    assert fresh.claude_session_tokens(folder, "EM-1") == 7


def test_a_tab_close_error_means_no_relaunch_and_is_retried(rig, sizes, herdr_log, monkeypatch):
    rig.verdict("FAIL")
    tabbed(rig)
    real = fresh.lc.herdr

    def boom(*a, **k):
        if a[:2] == ("tab", "close"):
            raise fresh.lc.LaunchError("close failed")
        return real(*a, **k)
    monkeypatch.setattr(fresh.lc, "herdr", boom)
    rig.cycle()
    assert prompts(herdr_log) == [] and rig.warnings
    monkeypatch.setattr(fresh.lc, "herdr", real)
    rig.cycle()
    assert len(prompts(herdr_log)) == 1


def test_a_prompt_that_cannot_be_built_leaves_the_agent_running(rig, sizes, herdr_log, monkeypatch):
    rig.verdict("FAIL")
    tabbed(rig)
    monkeypatch.setattr(fresh, "build_prompt", lambda *a: 1 / 0)
    rig.cycle()
    assert prompts(herdr_log) == [] and rig.warnings
    assert not any(c[:2] == ["tab", "close"] for c in calls(herdr_log))


def test_a_fail_of_an_older_commit_is_not_acted_on(rig, sizes, herdr_log):
    rig.verdict("FAIL")
    rig.commit("fix")          # the agent already committed a fix: the verdict is obsolete
    tabbed(rig)
    rig.cycle()
    assert prompts(herdr_log) == [] and ["tab", "close", "w1:t4"] not in calls(herdr_log)


def test_a_tab_shared_with_another_agent_is_not_closed_and_is_retried(rig, sizes, herdr_log):
    rig.verdict("FAIL")
    tabbed(rig)
    world = json.loads(rig.world.read_text())
    world["agents"].append({"name": "someone-else", "agent_status": "working", "tab_id": "w1:t4"})
    rig.world.write_text(json.dumps(world))
    rig.cycle()
    assert prompts(herdr_log) == [] and rig.warnings
    tabbed(rig)
    rig.cycle(1)
    assert len(prompts(herdr_log)) == 1


def test_the_prompt_keeps_the_newest_notes_when_there_are_too_many_to_fit(rig):
    lines = [f"note {i:03d} " + "x" * 100 for i in range(200)]
    out = fresh._recent(lines, rig.id, limit=1000)
    assert "note 199" in out and "note 000" not in out
    assert f"ticket show {rig.id}" in out
