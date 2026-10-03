"""Local, read-only usage figures for each AI tool.

Nothing here reads credential files, uses the network, or writes anything.
Only a human running `fleet usage` makes this touch the real home folder;
tests pass sample directories.

Claude Code -- <config>/projects/**/*.jsonl (config: CLAUDE_CONFIG_DIR or
  ~/.claude). Each "assistant" line has message.usage (input_tokens,
  output_tokens, cache_creation_input_tokens, cache_read_input_tokens) and a
  timestamp. Claude Code records no plan limit or reset time locally, so we
  total tokens over the last 5 hours and 7 days and mark the limit unknown.
  Totals are split into fresh input (input + cache creation), output and
  cache reads, because cache reads count far less against plan limits.
  Lines are de-duplicated by (message.id, requestId). recorded_at is the
  newest message timestamp.

Codex -- <codex home>/sessions/**/rollout-*.jsonl (CODEX_HOME or ~/.codex).
  A log line carries payload.rate_limits (layout confirmed by
  tests/data/codex_rate_limits_sample.json): primary = 5-hour window,
  secondary = weekly window, each with used_percent, window_minutes and
  resets_at (Unix seconds). recorded_at is that log line's own timestamp.
  Codex only logs interactive sessions, so the figure can be old: a window is
  flagged stale if it was recorded longer ago than its window length, or its
  reset time has already passed. A stale window is shown as "unknown (last
  seen N% used)" rather than as a current percentage; JSON keeps the raw data. Only rollout-*.jsonl files are opened.
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


def _local(iso: str | None) -> str | None:
    """An ISO UTC string shown in the local time zone with a short label."""
    dt = _ts(iso)
    if dt is None:
        return iso
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    except (ValueError, OverflowError, OSError):
        return iso


def _lines(path: Path, unreadable: list | None = None):
    """Yield each JSON object line. A file that cannot be read is appended to
    `unreadable` (if given) instead of being skipped silently."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        if unreadable is not None:
            unreadable.append(str(path))


def _files(root: Path, pattern: str, since: datetime,
           unreadable: list | None = None) -> list[Path]:
    """Files under root modified since `since`, newest first."""
    found = []
    if root.is_dir():
        for p in root.rglob(pattern):
            try:
                m = p.stat().st_mtime
            except OSError:
                if unreadable is not None:
                    unreadable.append(str(p))
                continue
            if m >= since.timestamp():
                found.append((m, p))
    return [p for _, p in sorted(found, reverse=True)]


def _window(tokens=None, used_percent=None, resets_at=None, recorded_at=None,
            stale=False, breakdown=None) -> dict:
    return {"tokens": tokens, "used_percent": used_percent,
            "resets_at": _iso(resets_at), "recorded_at": _iso(recorded_at),
            "stale": stale, "breakdown": breakdown}


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def claude_usage(home: Path, now: datetime) -> dict:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    t5, t7 = now - FIVE_HOURS, now - WEEK
    zero = {"fresh_input": 0, "output": 0, "cache_read": 0}
    five, week = dict(zero), dict(zero)
    newest = None
    seen = set()
    unreadable: list[str] = []
    for path in _files(root, "*.jsonl", t7, unreadable):
        for obj in _lines(path, unreadable):
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
            parts = {
                "fresh_input": _int(usage.get("input_tokens"))
                + _int(usage.get("cache_creation_input_tokens")),
                "output": _int(usage.get("output_tokens")),
                "cache_read": _int(usage.get("cache_read_input_tokens")),
            }
            for k, v in parts.items():
                week[k] += v
                if ts >= t5:
                    five[k] += v
            if newest is None or ts > newest:
                newest = ts
    return {
        "tool": "claude", "limit_known": False,
        "five_hour": _window(tokens=sum(five.values()), recorded_at=newest, breakdown=five),
        "weekly": _window(tokens=sum(week.values()), recorded_at=newest, breakdown=week),
        "note": "limit unknown: Claude Code does not record its plan limit locally",
        "unreadable": unreadable,
    }


def _pct(v) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # integer too large to convert to float
        return False


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
    unreadable: list[str] = []
    for path in _files(root, "rollout-*.jsonl", now - WEEK, unreadable):
        for obj in _lines(path, unreadable):
            payload = obj.get("payload")
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            ts = _ts(obj.get("timestamp"))
            usable = isinstance(rl, dict) and any(
                isinstance(rl.get(k), dict) and _pct(rl[k].get("used_percent"))
                for k in ("primary", "secondary"))
            if usable and ts is not None and now - WEEK <= ts <= now and (best is None or ts > best[0]):
                best = (ts, rl)
    out = {"tool": "codex", "limit_known": False, "five_hour": _window(),
           "weekly": _window(), "unreadable": unreadable,
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


def _n(n: int) -> str:
    try:
        return f"{n:,}"
    except ValueError:  # beyond Python's int-to-string digit limit
        return "huge"


def _text(w: dict, label: str) -> str:
    if w["used_percent"] is not None and w["stale"]:
        # An old percentage (often 0% after a reset) would read as real usage.
        s = f"{label} unknown (last seen {w['used_percent']:g}% used)"
    elif w["used_percent"] is not None:
        s = f"{label} {w['used_percent']:g}% used"
        if w["resets_at"]:
            s += f", resets {_local(w['resets_at'])}"
    elif w["tokens"] is not None:
        b = w.get("breakdown")
        s = f"{label} {_n(w['tokens'])} tokens"
        if b:
            s += (f" ({_n(b['fresh_input'])} fresh input, {_n(b['output'])} output,"
                  f" {_n(b['cache_read'])} cache reads)")
        s += ", limit unknown"
    else:
        return f"{label} unknown"
    if w["recorded_at"]:
        s += f" [recorded {_local(w['recorded_at'])}{', STALE' if w['stale'] else ''}]"
    return s


def line(r: dict) -> str:
    out = f"{r['tool']:<7} {_text(r['five_hour'], '5h')}  |  {_text(r['weekly'], 'week')}"
    bad = r.get("unreadable")
    if bad:
        out += f"  |  WARNING: {len(bad)} log file(s) could not be read, figures may be incomplete"
    return out
