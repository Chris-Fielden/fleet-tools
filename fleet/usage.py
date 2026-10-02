"""Local, read-only usage figures for each AI tool.

Nothing here reads credential files, uses the network, or writes anything.
Only a human running `fleet usage` makes this touch the real home folder;
tests pass sample directories.

Claude Code -- <config>/projects/**/*.jsonl (config: CLAUDE_CONFIG_DIR or
  ~/.claude). Each "assistant" line has message.usage (input_tokens,
  output_tokens, cache_creation_input_tokens, cache_read_input_tokens) and a
  timestamp. Claude Code records no plan limit or reset time locally, so we
  total tokens over the last 5 hours and 7 days and mark the limit unknown.
  Lines are de-duplicated by (message.id, requestId). recorded_at is the
  newest message timestamp.

Codex -- <codex home>/sessions/**/rollout-*.jsonl (CODEX_HOME or ~/.codex).
  A log line carries payload.rate_limits (layout confirmed by
  tests/data/codex_rate_limits_sample.json): primary = 5-hour window,
  secondary = weekly window, each with used_percent, window_minutes and
  resets_at (Unix seconds). recorded_at is that log line's own timestamp.
  Codex only logs interactive sessions, so the figure can be old: a window is
  flagged stale if it was recorded longer ago than its window length, or its
  reset time has already passed. Only rollout-*.jsonl files are opened.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIVE_HOURS = timedelta(hours=5)
WEEK = timedelta(days=7)


def _ts(value) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _lines(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _files(root: Path, pattern: str, since: datetime) -> list[Path]:
    """Files under root modified since `since`, newest first."""
    found = []
    if root.is_dir():
        for p in root.rglob(pattern):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= since.timestamp():
                found.append((m, p))
    return [p for _, p in sorted(found, reverse=True)]


def _window(tokens=None, used_percent=None, resets_at=None, recorded_at=None,
            stale=False) -> dict:
    return {"tokens": tokens, "used_percent": used_percent,
            "resets_at": _iso(resets_at), "recorded_at": _iso(recorded_at),
            "stale": stale}


def claude_usage(home: Path, now: datetime) -> dict:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    t5, t7 = now - FIVE_HOURS, now - WEEK
    five = week = 0
    newest = None
    seen = set()
    for path in _files(root, "*.jsonl", t7):
        for obj in _lines(path):
            msg = obj.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            ts = _ts(obj.get("timestamp"))
            if obj.get("type") != "assistant" or not isinstance(usage, dict) \
                    or ts is None or ts < t7 or ts > now:
                continue
            key = (msg.get("id"), obj.get("requestId"))
            if not all(k is None or isinstance(k, str) for k in key):
                continue
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            n = sum(v for v in (usage.get(k) for k in (
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"))
                    if isinstance(v, int) and not isinstance(v, bool))
            week += n
            if ts >= t5:
                five += n
            if newest is None or ts > newest:
                newest = ts
    return {
        "tool": "claude", "limit_known": False,
        "five_hour": _window(tokens=five, recorded_at=newest),
        "weekly": _window(tokens=week, recorded_at=newest),
        "note": "limit unknown: Claude Code does not record its plan limit locally",
    }


def _pct(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _codex_window(rl, recorded_at: datetime, length: timedelta, now: datetime) -> dict:
    if not isinstance(rl, dict) or not _pct(rl.get("used_percent")):
        return _window()
    resets = _ts(rl.get("resets_at"))
    minutes = rl.get("window_minutes")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) \
            and 0 < minutes <= 60 * 24 * 365:
        length = timedelta(minutes=minutes)
    stale = now - recorded_at > length or (resets is not None and resets <= now)
    return _window(used_percent=float(rl["used_percent"]), resets_at=resets,
                   recorded_at=recorded_at, stale=stale)


def codex_usage(home: Path, now: datetime) -> dict:
    root = Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions"
    best = None  # (recorded_at, rate_limits)
    for path in _files(root, "rollout-*.jsonl", now - WEEK):
        for obj in _lines(path):
            payload = obj.get("payload")
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            ts = _ts(obj.get("timestamp"))
            usable = isinstance(rl, dict) and any(
                isinstance(rl.get(k), dict) and _pct(rl[k].get("used_percent"))
                for k in ("primary", "secondary"))
            if usable and ts is not None and now - WEEK <= ts <= now and (best is None or ts > best[0]):
                best = (ts, rl)
    out = {"tool": "codex", "limit_known": False, "five_hour": _window(),
           "weekly": _window(),
           "note": "limit unknown: no Codex rate-limit record found in the last 7 days"}
    if best:
        at, rl = best
        out["five_hour"] = _codex_window(rl.get("primary"), at, FIVE_HOURS, now)
        out["weekly"] = _codex_window(rl.get("secondary"), at, WEEK, now)
        out["limit_known"] = out["five_hour"]["used_percent"] is not None \
            or out["weekly"]["used_percent"] is not None
        out["note"] = None if out["limit_known"] else out["note"]
    return out


def collect(home: Path | None = None, now: datetime | None = None) -> list[dict]:
    home = home or Path.home()
    now = now or datetime.now(timezone.utc)
    return [claude_usage(home, now), codex_usage(home, now)]


def _text(w: dict, label: str) -> str:
    if w["used_percent"] is not None:
        s = f"{label} {w['used_percent']:g}% used"
        if w["resets_at"]:
            s += f", resets {w['resets_at']}"
    elif w["tokens"] is not None:
        s = f"{label} {w['tokens']:,} tokens, limit unknown"
    else:
        return f"{label} unknown"
    if w["recorded_at"]:
        s += f" [recorded {w['recorded_at']}{', STALE' if w['stale'] else ''}]"
    return s


def line(r: dict) -> str:
    return f"{r['tool']:<7} {_text(r['five_hour'], '5h')}  |  {_text(r['weekly'], 'week')}"
