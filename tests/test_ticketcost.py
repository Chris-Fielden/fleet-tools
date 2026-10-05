import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleet import ticket as tk
from fleet import ticketcost as tc

ROOT = Path(__file__).resolve().parent.parent
REVIEW_PROMPT = "You are a code reviewer. Review the changes on branch 'task/demo'."


@pytest.fixture(autouse=True)
def _no_real_homes(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(branch="task/demo"):
    return tk.create_ticket({"type": "build", "repo": "fleet-tools", "branch": branch,
                             "title": "Do a thing", "body": "Details."})


def claude_file(home, name, cwd, first, start, rows):
    d = home / ".claude/projects/p"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "timestamp": z(start), "cwd": cwd,
                         "message": {"role": "user", "content": first}})]
    for i, (inp, out, cc, cr) in enumerate(rows):
        lines.append(json.dumps({
            "type": "assistant", "timestamp": z(start + timedelta(minutes=i + 1)), "cwd": cwd,
            "requestId": f"r{name}{i}",
            "message": {"id": f"m{name}{i}", "model": "claude-sonnet-5-5",
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}))
    lines.append(lines[-1])  # a repeated line must be counted once
    (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


def codex_file(home, name, cwd, first, start, inp, cached, out):
    d = home / ".codex/sessions/2026/10/03"
    d.mkdir(parents=True, exist_ok=True)

    def ev(t, ty, p):
        return json.dumps({"timestamp": z(t), "type": ty, "payload": p})
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
             "total_tokens": inp + out}
    lines = [ev(start, "session_meta", {"cwd": cwd}),
             ev(start, "turn_context", {"cwd": cwd, "model": "gpt-test"}),
             ev(start, "event_msg", {"type": "item_completed",
                                     "item": {"type": "UserMessage", "content": [{"type": "text", "text": first}]}}),
             ev(start + timedelta(minutes=1), "event_msg",
                {"type": "token_count", "info": {"total_token_usage": {**usage, "input_tokens": 1, "total_tokens": 1}}}),
             ev(start + timedelta(minutes=3), "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage}})]
    (d / f"rollout-{name}.jsonl").write_text("\n".join(lines) + "\n")


def test_sessions_split_by_kind_agent_and_ticket(env):
    t = ticket()
    other = ticket("task/other")
    now = datetime.now(timezone.utc)
    home = env / "home"
    wt = str(env / "fleet-tools/.worktrees/demo")
    claude_file(home, "a", wt, "Do the work", now, [(10, 20, 30, 40), (1, 2, 3, 4)])
    claude_file(home, "b", wt, REVIEW_PROMPT, now, [(5, 5, 0, 7)])
    codex_file(home, "c", wt, REVIEW_PROMPT, now, inp=1000, cached=400, out=50)
    claude_file(home, "d", str(env / "fleet-tools/.worktrees/other"), "x", now, [(1, 1, 1, 1)])
    claude_file(home, "e", str(env / "fleet-tools"), "no worktree", now, [(9, 9, 9, 9)])

    c = tc.for_ticket(t, home)
    assert c["runs"] == 3
    by = {(s["kind"], s["agent"]): s for s in c["sessions"]}
    build = by[("build", "claude")]
    assert (build["fresh_input"], build["output"], build["cache_read"]) == (10 + 30 + 1 + 3, 22, 44)
    assert build["model"] == "claude-sonnet-5-5" and build["duration_seconds"] == 120
    cx = by[("review", "codex")]
    assert (cx["fresh_input"], cx["output"], cx["cache_read"]) == (600, 50, 400)
    assert cx["model"] == "gpt-test" and cx["duration_seconds"] == 180
    assert ("review", "claude") in by
    assert c["tokens"] == sum(s["tokens"] for s in c["sessions"])
    line = tc.usage_line(c)
    assert "3 run(s)" in line and "codex:gpt-test" in line

    rows = tc.by_ticket(home, now + timedelta(hours=1))
    assert [r["ticket"] for r in rows] == [t["id"], other["id"]]
    assert set(rows[0]["split"]) == {"build/claude", "review/claude", "review/codex"}
    assert tc.table(rows).startswith("TICKET")


def test_old_sessions_are_left_out(env):
    ticket()
    home = env / "home"
    now = datetime.now(timezone.utc)
    claude_file(home, "a", str(env / "fleet-tools/.worktrees/demo"), "x", now - timedelta(days=9), [(1, 1, 1, 1)])
    assert tc.by_ticket(home, now) == []
    assert tc.table([]).startswith("no ticket usage")


def test_no_prompt_text_in_results(env):
    t = ticket()
    home = env / "home"
    claude_file(home, "a", str(env / "fleet-tools/.worktrees/demo"), "SECRET-PROMPT-TEXT",
                datetime.now(timezone.utc), [(1, 1, 1, 1)])
    assert "SECRET-PROMPT-TEXT" not in json.dumps(tc.for_ticket(t, home))


def test_cli_show_and_by_ticket(env):
    t = ticket()
    home = env / "home"
    claude_file(home, "a", str(env / "fleet-tools/.worktrees/demo"), "work", datetime.now(timezone.utc), [(10, 20, 30, 40)])
    bin_ = ROOT / "bin"
    shown = subprocess.run([sys.executable, str(bin_ / "ticket"), "show", t["id"]],
                           capture_output=True, text=True)
    assert shown.returncode == 0 and "usage:" in shown.stdout and "100 tokens" in shown.stdout
    r = subprocess.run([sys.executable, str(bin_ / "fleet"), "usage", "--by-ticket"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and t["id"] in r.stdout and "build/claude 100" in r.stdout
    j = subprocess.run([sys.executable, str(bin_ / "fleet"), "usage", "--by-ticket", "--json"],
                       capture_output=True, text=True)
    assert json.loads(j.stdout)[0]["ticket"] == t["id"]


def test_show_without_sessions_says_so(env):
    t = ticket()
    r = subprocess.run([sys.executable, str(ROOT / "bin/ticket"), "show", t["id"]],
                       capture_output=True, text=True)
    assert "usage:" in r.stdout and "none found" in r.stdout


def test_review_runs_keep_a_codex_session_file():
    # Codex only logs rate limits into session files; --ephemeral wrote none,
    # which is why `fleet usage` showed Codex as STALE after review-only days.
    script = (ROOT / "scripts/review.sh").read_text()
    exec_lines = [ln for ln in script.splitlines() if "codex exec" in ln and not ln.lstrip().startswith("#")]
    assert exec_lines and all("--ephemeral" not in ln for ln in exec_lines)


def test_same_branch_in_two_repos_is_kept_apart(env):
    mine = ticket()
    theirs = tk.create_ticket({"type": "build", "repo": "other-repo", "branch": "task/demo",
                               "title": "Other", "body": "Details."})
    home = env / "home"
    now = datetime.now(timezone.utc)
    claude_file(home, "a", str(env / "fleet-tools/.worktrees/demo"), "x", now, [(1, 1, 1, 1)])
    claude_file(home, "b", str(env / "other-repo/.worktrees/demo"), "x", now, [(5, 5, 5, 5)])
    assert tc.for_ticket(mine, home)["tokens"] == 4
    assert tc.for_ticket(theirs, home)["tokens"] == 20


def test_week_report_counts_only_usage_inside_the_window(env):
    ticket()
    home = env / "home"
    now = datetime.now(timezone.utc)
    wt = str(env / "fleet-tools/.worktrees/demo")
    start = now - timedelta(days=7, seconds=90)
    claude_file(home, "a", wt, "x", start, [(100, 0, 0, 0), (1, 0, 0, 0), (2, 0, 0, 0)])
    rows = tc.by_ticket(home, now)
    assert rows[0]["tokens"] == 3 and rows[0]["duration_seconds"] == 60
    # Codex: running totals; the 7 days ago part is subtracted.
    d = home / ".codex/sessions/2026/10/03"
    d.mkdir(parents=True)

    def ev(t, total):
        u = {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": total}
        return json.dumps({"timestamp": z(t), "type": "event_msg",
                           "payload": {"type": "token_count", "info": {"total_token_usage": u}}})
    meta = json.dumps({"timestamp": z(now - timedelta(days=8)), "type": "session_meta", "payload": {"cwd": wt}})
    (d / "rollout-x.jsonl").write_text("\n".join([
        meta, ev(now - timedelta(days=8), 1000), ev(now - timedelta(minutes=5), 1040)]) + "\n")
    codex = [r for r in tc.sessions(home, now - tc.WEEK, now) if r["agent"] == "codex"]
    assert codex[0]["tokens"] == 40 and codex[0]["duration_seconds"] == 0


def test_a_timed_run_always_has_its_tokens_counted(env):
    # A ticket with several runs, one still waiting for its first reply and one
    # whose log grew after its last reply: neither may add time without tokens.
    t = ticket()
    home = env / "home"
    now = datetime.now(timezone.utc)
    wt = str(env / "fleet-tools/.worktrees/demo")
    claude_file(home, "a", wt, "Do the work", now, [(10, 20, 0, 0), (1, 2, 0, 0)])
    # Log lines after the last assistant line (tool results, attachments).
    path = home / ".claude/projects/p/a.jsonl"
    tail = [json.dumps({"type": t_, "timestamp": z(now + timedelta(minutes=m)), "cwd": wt})
            for t_, m in (("user", 9), ("attachment", 12))]
    path.write_text(path.read_text() + "\n".join(tail) + "\n")
    # A fresh Claude run with no reply yet.
    d = home / ".claude/projects/p"
    (d / "fresh.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": z(now), "cwd": wt, "message": {"role": "user", "content": "go"}}) + "\n")
    # A Codex review with no token_count yet, and one that finished.
    codex_file(home, "done", wt, REVIEW_PROMPT, now, inp=1000, cached=400, out=50)
    cd = home / ".codex/sessions/2026/10/03"
    (cd / "rollout-new.jsonl").write_text(json.dumps(
        {"timestamp": z(now), "type": "session_meta", "payload": {"cwd": wt}}) + "\n")

    c = tc.for_ticket(t, home)
    assert c["runs"] == 2
    assert all(s["tokens"] > 0 for s in c["sessions"])
    by = {s["agent"]: s for s in c["sessions"]}
    assert by["claude"]["duration_seconds"] == 120 and by["codex"]["duration_seconds"] == 180
    assert c["duration_seconds"] == 300
