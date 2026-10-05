"""Tokens and time per ticket, from the Claude and Codex session files on disk.

Read-only, like usage.py: nothing is written and no credential file is opened.
Every session file is one agent launch or one review run. It belongs to a
ticket through its working folder (`.../.worktrees/<name>` is branch
`task/<name>`, which a ticket names in its `branch` field). A session whose
first user message is the review prompt of scripts/review.sh counts as a
review; any other session is build work. Only counts, ids, model names and
times are kept: no prompt text, paths or other session content is returned.

Claude -- <config>/projects/**/*.jsonl: cwd, gitBranch, message.model and
  message.usage per assistant line (de-duplicated by (message.id, requestId)).
Codex -- <codex home>/sessions/**/rollout-*.jsonl: cwd and model from
  session_meta / turn_context; the last token_count event holds the running
  total. Codex counts cached tokens inside input_tokens, so fresh input is
  input minus cached.
Tokens are split like usage.py: fresh input (input + cache creation), output,
cache reads.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ticket as tk
from .usage import WEEK, _files, _int, _iso, _lines, _n, _ts

REVIEW_MARK = "you are a code reviewer"
_WORKTREE = re.compile(r"/\.worktrees/([^/]+)")


def _branch(cwd, git_branch=None) -> str | None:
    if isinstance(git_branch, str) and git_branch.startswith("task/"):
        return git_branch
    m = _WORKTREE.search(cwd) if isinstance(cwd, str) else None
    return f"task/{m.group(1)}" if m else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def _session(agent, branch, model, first_user, times, tokens) -> dict | None:
    if not times or branch is None:
        return None
    start, end = min(times), max(times)
    return {
        "agent": agent, "model": model,
        "kind": "review" if first_user.strip().lower().startswith(REVIEW_MARK) else "build",
        "branch": branch, "started_at": _iso(start), "ended_at": _iso(end),
        "duration_seconds": int((end - start).total_seconds()),
        "fresh_input": tokens[0], "output": tokens[1], "cache_read": tokens[2],
        "tokens": sum(tokens), "_start": start,
    }


def _claude_session(path: Path, unreadable) -> dict | None:
    branch = model = None
    first_user = None
    times, seen = [], set()
    tot = [0, 0, 0]
    for obj in _lines(path, unreadable):
        ts = _ts(obj.get("timestamp"))
        if ts is not None:
            times.append(ts)
        branch = branch or _branch(obj.get("cwd"), obj.get("gitBranch"))
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        if obj.get("type") == "user" and first_user is None:
            first_user = _text(msg.get("content"))
        usage = msg.get("usage")
        if obj.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        key = (msg.get("id"), obj.get("requestId"))
        if not all(k is None or isinstance(k, str) for k in key):
            continue
        if key != (None, None):
            if key in seen:
                continue
            seen.add(key)
        if isinstance(msg.get("model"), str) and not msg["model"].startswith("<"):
            model = model or msg["model"]
        tot[0] += _int(usage.get("input_tokens")) + _int(usage.get("cache_creation_input_tokens"))
        tot[1] += _int(usage.get("output_tokens"))
        tot[2] += _int(usage.get("cache_read_input_tokens"))
    return _session("claude", branch, model, first_user or "", times, tot)


def _codex_session(path: Path, unreadable) -> dict | None:
    branch = model = None
    first_user = None
    times = []
    best = None  # the running total with the most tokens
    for obj in _lines(path, unreadable):
        ts = _ts(obj.get("timestamp"))
        if ts is not None:
            times.append(ts)
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        if obj.get("type") in ("session_meta", "turn_context"):
            branch = branch or _branch(p.get("cwd"))
            if isinstance(p.get("model"), str):
                model = model or p["model"]
        elif obj.get("type") == "event_msg":
            item = p.get("item")
            if p.get("type") == "item_completed" and first_user is None \
                    and isinstance(item, dict) and item.get("type") == "UserMessage":
                first_user = _text(item.get("content"))
            info = p.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if p.get("type") == "token_count" and isinstance(total, dict):
                if best is None or _int(total.get("total_tokens")) >= _int(best.get("total_tokens")):
                    best = total
    tot = [0, 0, 0]
    if best:
        cached = _int(best.get("cached_input_tokens"))
        tot = [max(_int(best.get("input_tokens")) - cached, 0), _int(best.get("output_tokens")), cached]
    return _session("codex", branch, model, first_user or "", times, tot)


def sessions(home: Path | None = None, since: datetime | None = None,
             now: datetime | None = None) -> list[dict]:
    """Every attributable session file modified since `since` (default 7 days)."""
    home = home or Path.home()
    now = now or datetime.now(timezone.utc)
    since = since or now - WEEK
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex") / "sessions"
    out, bad = [], []
    for root, pattern, parse in ((claude, "*.jsonl", _claude_session),
                                 (codex, "rollout-*.jsonl", _codex_session)):
        for path in _files(root, pattern, since, bad):
            s = parse(path, bad)
            if s and s["_start"] <= now:
                out.append(s)
    return out


def _owner(tickets: list[dict], s: dict) -> str | None:
    """The ticket a session belongs to: of those on its branch, the newest one
    created before the session began (a fix ticket may reuse the folder)."""
    mine = [t for t in tickets if t.get("branch") == s["branch"]]
    if not mine:
        return None
    started = s["_start"]
    before = [t for t in mine if (_ts(t.get("created_at")) or started) <= started]
    return max(before or mine, key=lambda t: t.get("created_at") or "")["id"]


def attribute(tickets: list[dict], found: list[dict]) -> list[dict]:
    """The sessions that belong to a ticket, each with its `ticket` id."""
    out = []
    for s in found:
        tid = _owner(tickets, s)
        if tid:
            out.append({**{k: v for k, v in s.items() if k != "_start"}, "ticket": tid})
    return out


def for_ticket(ticket: dict, home: Path | None = None) -> dict:
    created = _ts(ticket.get("created_at")) or datetime.now(timezone.utc) - WEEK
    rows = [s for s in attribute(tk.list_tickets(), sessions(home, created - timedelta(minutes=1)))
            if s["ticket"] == ticket["id"]]
    total = {k: sum(s[k] for s in rows)
             for k in ("fresh_input", "output", "cache_read", "tokens", "duration_seconds")}
    return {"ticket": ticket["id"], "runs": len(rows), **total, "sessions": rows}


def by_ticket(home: Path | None = None, now: datetime | None = None, limit: int = 10) -> list[dict]:
    """Top tickets by tokens over the last 7 days, split build/review and by agent."""
    now = now or datetime.now(timezone.utc)
    rows = attribute(tk.list_tickets(), sessions(home, now - WEEK, now))
    agg: dict[str, dict] = {}
    for s in rows:
        if (_ts(s["ended_at"]) or now) < now - WEEK:
            continue
        a = agg.setdefault(s["ticket"], {"ticket": s["ticket"], "tokens": 0, "runs": 0,
                                         "duration_seconds": 0, "split": {}})
        a["tokens"] += s["tokens"]
        a["runs"] += 1
        a["duration_seconds"] += s["duration_seconds"]
        key = f"{s['kind']}/{s['agent']}"
        a["split"][key] = a["split"].get(key, 0) + s["tokens"]
    return sorted(agg.values(), key=lambda a: (-a["tokens"], a["ticket"]))[:limit]


def _dur(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m{s:02d}s"


def usage_line(c: dict) -> str:
    """One line for `ticket show`."""
    if not c["runs"]:
        return "none found"
    models = sorted({f"{s['agent']}:{s['model'] or '?'}" for s in c["sessions"]})
    return (f"{_n(c['tokens'])} tokens ({_n(c['fresh_input'])} fresh input, {_n(c['output'])} output,"
            f" {_n(c['cache_read'])} cache reads) in {c['runs']} run(s), {_dur(c['duration_seconds'])}"
            f" [{', '.join(models)}]")


def table(rows: list[dict]) -> str:
    if not rows:
        return "no ticket usage found in the last 7 days"
    out = [f"{'TICKET':<10} {'TOKENS':>14}  RUNS  TIME     SPLIT (build/review, claude/codex)"]
    for r in rows:
        split = "  ".join(f"{k} {_n(v)}" for k, v in sorted(r["split"].items()))
        out.append(f"{r['ticket']:<10} {_n(r['tokens']):>14}  {r['runs']:>4}  {_dur(r['duration_seconds']):<7}  {split}")
    return "\n".join(out)
