"""Real plan usage, saved from Claude Code's status line.

Claude Code runs the status line command (bin/fleet-statusline) with a JSON
payload on stdin; once the account has made a request, the payload carries
`rate_limits` with the 5-hour and weekly percentages and their reset times.
`record()` keeps only those few numbers, with a timestamp, in
$FLEET_HOME/plan-usage.json. Nothing else from the payload (paths, session
ids, model names, anything that could be a secret) is ever written.

`load()` reads the file back for `fleet status --json` and the widgets, adding
its age, and marking each window stale once its reset time has passed (the
percentage then belongs to a window that is over).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import ticket as tk
from .usage import _iso, _ts

# Payload key (Claude Code) -> our key.
WINDOWS = (("five_hour", "five_hour"), ("seven_day", "weekly"))
# Without a fresh sample the figure is not shown as real (the longest window).
MAX_AGE_SECONDS = 5 * 3600


def path() -> Path:
    return tk.fleet_home() / "plan-usage.json"


def _percent(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= v <= 1000:
        return None
    return float(v)


def extract(payload, now: datetime) -> dict | None:
    """The figures worth keeping from a status line payload, or None."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    out = {"saved_at": _iso(now)}
    found = False
    for src, dst in WINDOWS:
        w = limits.get(src)
        used = _percent(w.get("used_percentage", w.get("used_percent"))) if isinstance(w, dict) else None
        if used is None:
            out[dst] = None
            continue
        found = True
        out[dst] = {"used_percent": used, "resets_at": _iso(_ts(w.get("resets_at")))}
    return out if found else None


def record(payload, now: datetime | None = None) -> bool:
    """Save the rate limits in `payload`. False if it carried none (the old
    file is then left alone: an earlier real figure beats nothing)."""
    now = now or datetime.now(timezone.utc)
    data = extract(payload, now)
    if data is None:
        return False
    target = path()
    tk.refuse_real_store_in_tests(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return True


def load(now: datetime | None = None) -> dict:
    """What the status line last saved: {"available", "saved_at", "age_seconds",
    "five_hour", "weekly"}. Each window has used_percent, resets_at and stale.
    `available` is False when there is no file, or it is older than five hours,
    or both windows are stale: then only the token estimate is a fallback."""
    now = now or datetime.now(timezone.utc)
    out = {"available": False, "saved_at": None, "age_seconds": None,
           "five_hour": None, "weekly": None}
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    saved = _ts(data.get("saved_at")) if isinstance(data, dict) else None
    if saved is None or saved > now:
        return out
    out["saved_at"] = _iso(saved)
    out["age_seconds"] = int((now - saved).total_seconds())
    for _, key in WINDOWS:
        w = data.get(key)
        used = _percent(w.get("used_percent")) if isinstance(w, dict) else None
        if used is None:
            continue
        resets = _ts(w.get("resets_at"))
        out[key] = {"used_percent": used, "resets_at": _iso(resets),
                    "stale": resets is not None and resets <= now}
    fresh = out["age_seconds"] <= MAX_AGE_SECONDS
    out["available"] = fresh and any(out[k] and not out[k]["stale"] for _, k in WINDOWS)
    return out


def line(data: dict) -> str:
    parts = []
    for label, key in (("5h", "five_hour"), ("wk", "weekly")):
        w = data.get(key)
        if w and not w["stale"]:
            parts.append(f"{label} {w['used_percent']:g}%")
    return " | ".join(parts)


def main() -> int:
    """Status line command: save the rate limits, print a short line. Never fails
    (a broken status line must not disturb the agent)."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError, RecursionError):
        payload = None
    try:
        record(payload)
        print(line(load()))
    except Exception:   # noqa: BLE001 - see docstring
        pass
    return 0
