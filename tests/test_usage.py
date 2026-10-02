import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fleet import usage

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = json.loads((ROOT / "tests/data/codex_rate_limits_sample.json").read_text())
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_real_homes(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_codex(home, recorded, rate_limits=None):
    d = home / ".codex/sessions/2026/10/02"
    d.mkdir(parents=True)
    obj = {"timestamp": z(recorded), "type": "event_msg",
           "payload": {"type": "token_count"}}
    node = obj
    for key in SAMPLE["found_at"][:-1]:
        node = node.setdefault(key, {})
    node[SAMPLE["found_at"][-1]] = rate_limits or SAMPLE["rate_limits"]
    (d / "rollout-a.jsonl").write_text("not json\n" + json.dumps(obj) + "\n")


def write_claude(home, rows):
    d = home / ".claude/projects/p"
    d.mkdir(parents=True)
    lines = []
    for ts, mid, n in rows:
        lines.append(json.dumps({
            "type": "assistant", "timestamp": z(ts), "requestId": "r" + mid,
            "message": {"id": mid, "usage": {"input_tokens": n, "output_tokens": 1,
                                             "cache_creation_input_tokens": 2,
                                             "cache_read_input_tokens": 3}}}))
    (d / "s.jsonl").write_text("\n".join(lines) + "\n")


def test_codex_fresh_from_real_sample(tmp_path):
    rl = json.loads(json.dumps(SAMPLE["rate_limits"]))
    rl["primary"]["used_percent"] = 42.5
    rl["primary"]["resets_at"] = int((NOW + timedelta(hours=2)).timestamp())
    rl["secondary"]["resets_at"] = int((NOW + timedelta(days=3)).timestamp())
    write_codex(tmp_path, NOW - timedelta(minutes=10), rl)
    r = usage.codex_usage(tmp_path, NOW)
    assert r["limit_known"] and r["five_hour"]["used_percent"] == 42.5
    assert r["five_hour"]["resets_at"] == z(NOW + timedelta(hours=2))
    assert r["five_hour"]["recorded_at"] == z(NOW - timedelta(minutes=10))
    assert not r["five_hour"]["stale"] and not r["weekly"]["stale"]


def test_codex_stale_when_older_than_window(tmp_path):
    # Real sample: 5h window already reset relative to NOW; recorded 6h ago.
    write_codex(tmp_path, NOW - timedelta(hours=6))
    r = usage.codex_usage(tmp_path, NOW)
    assert r["five_hour"]["stale"]
    assert "STALE" in usage.line(r)


def test_codex_none_found(tmp_path):
    r = usage.codex_usage(tmp_path, NOW)
    assert not r["limit_known"] and "unknown" in usage.line(r)


def test_claude_totals_dedupe_and_windows(tmp_path):
    write_claude(tmp_path, [
        (NOW - timedelta(hours=1), "a", 10),
        (NOW - timedelta(hours=1), "a", 10),      # duplicate
        (NOW - timedelta(hours=10), "b", 100),    # in week only
        (NOW - timedelta(days=8), "c", 1000),     # outside
    ])
    r = usage.claude_usage(tmp_path, NOW)
    assert r["five_hour"]["tokens"] == 16
    assert r["weekly"]["tokens"] == 16 + 106
    assert r["limit_known"] is False
    assert r["five_hour"]["recorded_at"] == z(NOW - timedelta(hours=1))
    assert "limit unknown" in usage.line(r)


def test_cli_json_uses_given_home(tmp_path):
    write_claude(tmp_path, [(datetime.now(timezone.utc) - timedelta(minutes=1), "a", 1)])
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
    env["HOME"] = str(tmp_path)
    p = subprocess.run([sys.executable, str(ROOT / "bin/fleet"), "usage", "--json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert [d["tool"] for d in data] == ["claude", "codex"]
    assert data[0]["five_hour"]["tokens"] == 7
    p = subprocess.run([sys.executable, str(ROOT / "bin/fleet"), "usage"],
                       capture_output=True, text=True, env=env)
    assert len(p.stdout.strip().splitlines()) == 2


def test_codex_ignores_future_and_empty_records(tmp_path):
    write_codex(tmp_path, NOW - timedelta(minutes=5))
    d = tmp_path / ".codex/sessions/2026/10/02"
    bad = [{"timestamp": z(NOW + timedelta(hours=1)), "payload": {"rate_limits": SAMPLE["rate_limits"]}},
           {"timestamp": z(NOW - timedelta(minutes=1)), "payload": {"rate_limits": {}}}]
    (d / "rollout-b.jsonl").write_text("\n".join(map(json.dumps, bad)) + "\n")
    r = usage.codex_usage(tmp_path, NOW)
    assert r["five_hour"]["recorded_at"] == z(NOW - timedelta(minutes=5))


def test_malformed_values_do_not_crash(tmp_path):
    d = tmp_path / ".claude/projects/p"
    d.mkdir(parents=True)
    row = {"type": "assistant", "timestamp": z(NOW - timedelta(hours=1)),
           "message": {"id": "x", "usage": {"input_tokens": "lots", "output_tokens": 4}}}
    (d / "s.jsonl").write_text(json.dumps(row) + "\n" + json.dumps({**row, "timestamp": 1e30}) + "\n")
    assert usage.claude_usage(tmp_path, NOW)["five_hour"]["tokens"] == 4


def test_review_regressions(tmp_path):
    d = tmp_path / ".claude/projects/p"
    d.mkdir(parents=True)
    ts = z(NOW - timedelta(hours=1))
    rows = [{"type": "assistant", "timestamp": ts, "requestId": [1],
             "message": {"id": {"a": 1}, "usage": {"output_tokens": 99}}},
            {"type": "assistant", "timestamp": ts,
             "message": {"id": "ok", "usage": {"output_tokens": 4}}}]
    (d / "s.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
    assert usage.claude_usage(tmp_path, NOW)["five_hour"]["tokens"] == 4

    rl = json.loads(json.dumps(SAMPLE["rate_limits"]))
    rl["primary"]["window_minutes"] = 1e30
    rl["secondary"]["used_percent"] = float("nan")
    write_codex(tmp_path, NOW - timedelta(minutes=5), rl)
    r = usage.codex_usage(tmp_path, NOW)
    assert r["five_hour"]["used_percent"] == 0.0
    assert r["weekly"]["used_percent"] is None


def test_codex_old_record_in_fresh_file_ignored(tmp_path):
    write_codex(tmp_path, NOW - timedelta(days=9))
    assert not usage.codex_usage(tmp_path, NOW)["limit_known"]
