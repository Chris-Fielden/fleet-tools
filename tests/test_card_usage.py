"""The usage line on a ticket's Kaneo card (fake Kaneo, temporary HOME and FLEET_HOME)."""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from conftest import new_args, run
from fake_kaneo import FakeKaneo
from fleet import kaneo as kn
from fleet import ticket as tk
from fleet import ticketcost as tc

API_KEY = "fake-key-for-tests-0001"
REVIEW_PROMPT = "You are a code reviewer. Review the changes on branch 'task/demo'."


@pytest.fixture(autouse=True)
def _no_real_homes(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.fixture
def kaneo():
    fake = FakeKaneo(API_KEY).start()
    home = tk_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "kaneo.json"
    path.write_text(json.dumps({"url": fake.url, "workspace_id": "ws1", "api_key": API_KEY}))
    path.chmod(0o600)
    yield fake
    fake.stop()


def tk_home() -> Path:
    return Path(os.environ["FLEET_HOME"])


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ticket(**over) -> str:
    r = run("new", *new_args(**over))
    assert r.returncode == 0, r.stderr
    return r.stdout.split()[0]


def claude_session(env, name, branch, first, rows):
    d = env / "home/.claude/projects/p"
    d.mkdir(parents=True, exist_ok=True)
    cwd = str(env / "fleet-tools/.worktrees" / branch.removeprefix("task/"))
    start = datetime.now(timezone.utc) - timedelta(seconds=30 * (len(rows) + 2))
    lines = [json.dumps({"type": "user", "timestamp": z(start), "cwd": cwd,
                         "message": {"role": "user", "content": first}})]
    for i, (inp, out) in enumerate(rows):
        lines.append(json.dumps({
            "type": "assistant", "timestamp": z(start + timedelta(seconds=30 * (i + 1))), "cwd": cwd,
            "requestId": f"r{name}{i}",
            "message": {"id": f"m{name}{i}", "model": "claude-sonnet-5-5",
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}))
    (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


def card(kaneo) -> dict:
    assert len(kaneo.tasks) == 1
    return next(iter(kaneo.tasks.values()))


def test_card_shows_usage_split_by_kind_and_agent(kaneo, env):
    new_ticket()
    claude_session(env, "a", "task/demo", "Do the work", [(1000, 500)])
    claude_session(env, "b", "task/demo", REVIEW_PROMPT, [(200, 100)])
    assert run("sync").returncode == 0
    desc = card(kaneo)["description"]
    assert "**Usage:** " in desc
    assert "build claude" in desc and "review claude" in desc and "2 run(s)" in desc


def test_no_usage_shows_nothing(kaneo):
    new_ticket()
    assert run("sync").returncode == 0
    assert "Usage" not in card(kaneo)["description"]


def test_card_updates_when_usage_changes_and_is_quiet_otherwise(kaneo, env):
    new_ticket()
    claude_session(env, "a", "task/demo", "Do the work", [(1000, 500)])
    run("sync")
    before = card(kaneo)["description"]
    kaneo.requests.clear()
    assert "unchanged" in run("sync").stdout
    assert kaneo.writes == []
    claude_session(env, "b", "task/demo", REVIEW_PROMPT, [(200, 100)])
    assert "updated" in run("sync").stdout
    assert card(kaneo)["description"] != before
    assert "review claude" in card(kaneo)["description"]


def test_oncall_runs_count_towards_the_original_ticket(kaneo, env):
    tid = new_ticket()
    tk.create_ticket({"type": "fix", "repo": "fleet-tools", "branch": "task/oc-fix",
                      "title": "On-call: fix", "body": "Fix it.", "parent": tid}, oncall=True)
    claude_session(env, "a", "task/demo", "Do the work", [(1000, 500)])
    claude_session(env, "b", "task/oc-fix", "Fix it", [(2000, 1000)])
    lines = tc.card_usage([tk.load_ticket(tid)])
    assert "including on-call" in lines[tid]
    assert lines[tid].startswith("4,500 tokens (including on-call)")


def test_card_usage_ignores_zero_and_unknown(env):
    t = tk.create_ticket({"type": "build", "repo": "fleet-tools", "branch": "task/demo",
                          "title": "Do a thing", "body": "Details."})
    assert tc.card_usage([t]) == {}
    assert tc.card_usage([]) == {}
    assert "Usage" not in kn.card_description(tk.load_ticket(t["id"]))
