"""Shared by the SwiftBar plugin and the iTerm2 component: fetch the fleet
status endpoint and turn it into text. Display only; nothing here writes
anything or talks to anything but that one URL.

Both Mac scripts import this file, so install them in the same folder
(docs/mac-widgets.md).
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

URL = os.environ.get("FLEET_STATUS_URL", "http://loic:8788/status.json")
OFFLINE = "fleet offline"
TIMEOUT = 5
MAX_AGE = 180   # seconds: an older snapshot is not shown as current


def fetch(url=URL, timeout=TIMEOUT):
    """The decoded snapshot, or None if the endpoint cannot be reached or
    answers anything unusable (so the widget never shows stale numbers)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read(1_000_000))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _age(data, now):
    try:
        made = datetime.strptime(data["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return (now - made).total_seconds()


def _tokens(n):
    if not isinstance(n, int):
        return "?"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def usage_text(data):
    """'5h 42%' for a real figure, '~112.0M tok (est)' for the fallback."""
    try:
        claude = data["plan_usage"]["claude"]
        w = claude.get("five_hour")
        if not claude.get("estimate") and w and not w.get("stale"):
            return f"5h {w['used_percent']:g}%"
        return f"5h ~{_tokens(data['tokens']['claude']['five_hour'])} tok (est)"
    except (KeyError, TypeError, ValueError):
        return "5h ?"


def summarize(data, now=None):
    """One line for a bar: usage, agents running, ready to ship. 'fleet offline'
    when there is no data or it is too old."""
    now = now or datetime.now(timezone.utc)
    age = _age(data, now) if data else None
    if age is None or age > MAX_AGE or age < -MAX_AGE:
        return OFFLINE
    try:
        running = len(data["agents_running"])
        ready = len(data["ready_to_ship"])
    except (KeyError, TypeError):
        return OFFLINE
    return f"{usage_text(data)} · {running} running · {ready} to ship"


def details(data):
    """Menu lines (SwiftBar dropdown) for a snapshot already known to be current."""
    out = []
    try:
        for tool, label in (("claude", "Claude"), ("codex", "Codex")):
            for key, name in (("five_hour", "5h"), ("weekly", "week")):
                plan = data["plan_usage"][tool]
                w = plan.get(key)
                if w and not plan.get("estimate"):
                    note = " (stale: window has reset)" if w.get("stale") else ""
                    age = w.get("age_seconds")
                    seen = f", seen {age // 60} min ago" if isinstance(age, int) else ""
                    out.append(f"{label} {name}: {w['used_percent']:g}% used, resets {w.get('resets_at') or '?'}{seen}{note}")
        if data["plan_usage"]["claude"].get("estimate"):
            t = data["tokens"]["claude"]
            out.append(f"Claude tokens (estimate): 5h {_tokens(t['five_hour'])}, today {_tokens(t['today'])}, "
                       f"week {_tokens(t['week'])}")
        for a in data["agents_running"]:
            out.append(f"{a['id']} running {a['minutes']} min")
        out.append("Ready to ship: " + (", ".join(data["ready_to_ship"]) or "none"))
        left = data["launches"]["left_today"]
        out.append(f"Launches left today: {'?' if left is None else left}")
        out.append("Dispatch: " + ("alive" if data["dispatch_alive"] else "NOT RUNNING"))
    except (KeyError, TypeError):
        pass
    return out
