"""The fleet status snapshot behind `fleet status --json` and the status endpoint.

One JSON object, built on LOIC, read by the Mac widgets (contrib/mac) and
nothing else. It holds counts, ticket ids, percentages and times: no ticket
text, no paths, no secrets.

    generated_at       when this was built (UTC)
    tokens             claude: five_hour / today / week token totals from local
                       session logs; codex: null (its logs only carry rate
                       limits, see plan_usage)
    plan_usage         real plan usage: claude (saved by the status line, see
                       planusage.py) and codex (from its rate-limit records).
                       Each window has used_percent, resets_at, age_seconds and
                       stale. When there is no recent real figure for Claude,
                       claude.source is "estimate" and only the token counts
                       above are available: an estimate, not plan usage.
    agents_running     [{id, minutes}] for tickets being worked on
    ready_to_ship      ticket ids waiting for the human's review/ship
    launches           {used_today, max_per_day, left_today}
    dispatch_alive     whether dispatch --watch holds its lock
    kaneo_url          the Kaneo board address (no credentials), or null
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone

from . import dispatch as dp
from . import kaneo
from . import planusage
from . import service as sv
from . import settings as st
from . import ticket as tk
from . import usage as us


def _minutes(t: dict, now: datetime) -> int | None:
    since = us._ts(t.get("started_at") or t.get("updated_at"))
    return max(0, int((now - since).total_seconds() // 60)) if since else None


def _board_url() -> str | None:
    try:
        url = kaneo.load_config()["url"]
        parts = urllib.parse.urlsplit(url)
    except (kaneo.NotConfigured, kaneo.KaneoError, ValueError):
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return urllib.parse.urlunsplit((parts.scheme, host + (f":{parts.port}" if parts.port else ""),
                                    parts.path, "", ""))


def _codex_window(w: dict, now: datetime) -> dict | None:
    if w["used_percent"] is None:
        return None
    rec = us._ts(w["recorded_at"])
    return {"used_percent": w["used_percent"], "resets_at": w["resets_at"],
            "age_seconds": int((now - rec).total_seconds()) if rec else None, "stale": w["stale"]}


def _claude_plan(real: dict, tokens: dict) -> dict:
    if real["available"]:
        w = {k: real[k] and dict(real[k], age_seconds=real["age_seconds"]) for k in ("five_hour", "weekly")}
        return {"source": "claude-statusline", "estimate": False, "saved_at": real["saved_at"], **w}
    return {"source": "estimate", "estimate": True, "saved_at": real["saved_at"],
            "five_hour": real["five_hour"] and dict(real["five_hour"], age_seconds=real["age_seconds"]),
            "weekly": real["weekly"] and dict(real["weekly"], age_seconds=real["age_seconds"]),
            "note": "no recent real figure; token counts are an estimate, not plan usage",
            "tokens": {"five_hour": tokens["five_hour"], "week": tokens["week"]}}


def snapshot(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    claude, codex = us.collect(now=now)
    tokens = {"claude": {"five_hour": claude["five_hour"]["tokens"], "today": claude["today"]["tokens"],
                         "week": claude["weekly"]["tokens"]},
              "codex": None}
    tickets = tk.list_tickets()
    running = sorted((t for t in tickets if t["status"] == "claimed"), key=lambda t: t["id"])
    settings = st.load_settings()
    day = now.strftime("%Y-%m-%d")
    try:
        used = dp.launches_today(day)
    except OSError:
        used = None
    cap = settings["max_launches_per_day"]
    return {
        "generated_at": us._iso(now),
        "tokens": tokens,
        "plan_usage": {"claude": _claude_plan(planusage.load(now), tokens["claude"]),
                       "codex": {"five_hour": _codex_window(codex["five_hour"], now),
                                 "weekly": _codex_window(codex["weekly"], now)}},
        "agents_running": [{"id": t["id"], "minutes": _minutes(t, now)} for t in running],
        "ready_to_ship": sorted(t["id"] for t in tickets if t["status"] == "review"),
        "launches": {"used_today": used, "max_per_day": cap,
                     "left_today": None if used is None else max(0, cap - used)},
        "dispatch_alive": sv.dispatcher_running(),
        "kaneo_url": _board_url(),
    }
