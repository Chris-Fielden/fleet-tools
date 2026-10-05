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
_WORKTREE = re.compile(r"([^/]+)/\.worktrees/([^/]+)")


def _where(cwd) -> tuple[str, str] | None:
    """(repo, branch) of a task worktree folder, or None. The repo is the name
    of the folder holding .worktrees, which is what a ticket's `repo` names."""
    m = _WORKTREE.search(cwd) if isinstance(cwd, str) else None
    return (m.group(1), f"task/{m.group(2)}") if m else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def _session(agent, where, model, first_user, times, since, tokens) -> dict | None:
    """`times` are all the session's timestamps; only those since `since` count
    towards the duration (tokens are already clipped by the caller)."""
    inside = [t for t in times if t >= since]
    if not inside or where is None:
        return None
    start, end = min(inside), max(inside)
    return {
        "agent": agent, "model": model,
        "kind": "review" if first_user.strip().lower().startswith(REVIEW_MARK) else "build",
        "repo": where[0], "branch": where[1], "started_at": _iso(start), "ended_at": _iso(end),
        "duration_seconds": int((end - start).total_seconds()),
        "fresh_input": tokens[0], "output": tokens[1], "cache_read": tokens[2],
        "tokens": sum(tokens), "_start": min(times),
    }


def _claude_session(path: Path, unreadable, since) -> dict | None:
    where = model = None
    first_user = None
    times, seen = [], set()
    tot = [0, 0, 0]
    for obj in _lines(path, unreadable):
        ts = _ts(obj.get("timestamp"))
        if ts is not None:
            times.append(ts)
        where = where or _where(obj.get("cwd"))
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        if obj.get("type") == "user" and first_user is None:
            first_user = _text(msg.get("content"))
        usage = msg.get("usage")
        if obj.get("type") != "assistant" or not isinstance(usage, dict) \
                or ts is None or ts < since:
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
    return _session("claude", where, model, first_user or "", times, since, tot)


def _codex_session(path: Path, unreadable, since) -> dict | None:
    where = model = None
    first_user = None
    times = []
    best = None  # the running total with the most tokens
    base = None  # the largest running total reached before `since`
    for obj in _lines(path, unreadable):
        ts = _ts(obj.get("timestamp"))
        if ts is not None:
            times.append(ts)
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        if obj.get("type") in ("session_meta", "turn_context"):
            where = where or _where(p.get("cwd"))
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
                if ts is not None and ts < since and (
                        base is None or _int(total.get("total_tokens")) >= _int(base.get("total_tokens"))):
                    base = total
    tot = [0, 0, 0]
    if best:
        # Running totals: what counts is the growth since the cutoff.
        def part(t, key):
            return _int(t.get(key)) if t else 0
        cached = max(part(best, "cached_input_tokens") - part(base, "cached_input_tokens"), 0)
        fresh = (part(best, "input_tokens") - part(base, "input_tokens")) - cached
        tot = [max(fresh, 0), max(part(best, "output_tokens") - part(base, "output_tokens"), 0), cached]
    return _session("codex", where, model, first_user or "", times, since, tot)


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
            s = parse(path, bad, since)
            if s and s["_start"] <= now:
                out.append(s)
    return out


def _owner(tickets: list[dict], s: dict) -> str | None:
    """The ticket a session belongs to: of those on its branch, the newest one
    created before the session began (a fix ticket may reuse the folder)."""
    mine = [t for t in tickets
            if t.get("branch") == s["branch"] and t.get("repo") == s["repo"]]
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


def _root(by_id: dict, t: dict) -> str:
    """The ticket an on-call fix reports to: follow oncall parents up to the original."""
    seen = {t["id"]}
    while t.get("oncall") and t.get("parent") in by_id and t["parent"] not in seen:
        t = by_id[t["parent"]]
        seen.add(t["id"])
    return t["id"]


def card_usage(targets: list[dict], home: Path | None = None) -> dict[str, str]:
    """Ticket id -> the usage line for its Kaneo card, for those of `targets` that
    have any usage (the others are left out). A ticket's total includes the runs of
    the on-call tickets filed to fix it. Session files are read once for all targets."""
    if not targets:
        return {}
    every = tk.list_tickets()
    by_id = {t["id"]: t for t in every}
    wanted = {t["id"] for t in targets}
    root = {t["id"]: _root(by_id, t) for t in every}
    created = [_ts(t.get("created_at")) for t in every if root[t["id"]] in wanted]
    start = min((c for c in created if c), default=datetime.now(timezone.utc) - WEEK)
    rows = [s for s in attribute(every, sessions(home, start - timedelta(minutes=1)))
            if root.get(s["ticket"]) in wanted]
    out: dict[str, str] = {}
    for tid in wanted:
        mine = [s for s in rows if root[s["ticket"]] == tid]
        total = sum(s["tokens"] for s in mine)
        if total <= 0:
            continue
        split: dict[str, int] = {}
        for s in mine:
            key = f"{s['kind']} {s['agent']}"
            split[key] = split.get(key, 0) + s["tokens"]
        parts = ", ".join(f"{k} {_n(v)}" for k, v in sorted(split.items()) if v)
        extra = " (including on-call)" if any(s["ticket"] != tid for s in mine) else ""
        out[tid] = (f"{_n(total)} tokens{extra}: {parts}; "
                    f"{_dur(sum(s['duration_seconds'] for s in mine))} over {len(mine)} run(s)")
    return out


def table(rows: list[dict]) -> str:
    if not rows:
        return "no ticket usage found in the last 7 days"
    out = [f"{'TICKET':<10} {'TOKENS':>14}  RUNS  TIME     SPLIT (build/review, claude/codex)"]
    for r in rows:
        split = "  ".join(f"{k} {_n(v)}" for k, v in sorted(r["split"].items()))
        out.append(f"{r['ticket']:<10} {_n(r['tokens']):>14}  {r['runs']:>4}  {_dur(r['duration_seconds']):<7}  {split}")
    return "\n".join(out)
