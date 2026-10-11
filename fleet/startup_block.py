"""Launches that Claude Code could not start (herdr says agent_not_ready, usually a
workspace trust dialog waiting for a person). One marker file holds them so the
dispatcher stops retrying: every retry would spend a daily launch and fail again.

<fleet_home>/startup-blocked.json: {"<ID>": {"folder": "...", "at": "...", "kind": "research"|"build"}}

A research marker holds ALL research launches (the cause hits every new folder); a
build marker holds only its own ticket. Nothing clears a marker by time: a human
runs `fleet unblock ID` after fixing the cause.
"""
from __future__ import annotations

import json

from . import ticket as tk

KINDS = ("research", "build")


def state_path():
    return tk.fleet_home() / "startup-blocked.json"


def load() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)
            and isinstance(v.get("folder"), str) and isinstance(v.get("at"), str)
            and v.get("kind") in KINDS}


def blocked_ids() -> frozenset:
    return frozenset(load())


def research_hold() -> str | None:
    """The ticket whose research launch is stuck at startup, or None."""
    for ticket_id, entry in sorted(load().items()):
        if entry["kind"] == "research":
            return ticket_id
    return None


def mark(ticket_id: str, folder: str, kind: str) -> None:
    data = load()
    data.setdefault(ticket_id, {"folder": folder, "at": tk._now(), "kind": kind})
    state_path().parent.mkdir(parents=True, exist_ok=True)
    tk._write_atomic(state_path(), data)


def clear(ticket_id: str) -> bool:
    data = load()
    if ticket_id not in data:
        return False
    del data[ticket_id]
    tk._write_atomic(state_path(), data)
    return True


def main(argv=None) -> int:
    """fleet unblock ID: clear the hold and let a later failure ping again."""
    import sys
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1 or argv[0].startswith("-"):
        print("usage: fleet unblock ID   (after fixing why the agent could not start)", file=sys.stderr)
        return 2
    ticket_id = argv[0].upper()
    if not clear(ticket_id):
        held = ", ".join(sorted(load())) or "none"
        print(f"fleet: {ticket_id} is not startup-blocked (blocked: {held})", file=sys.stderr)
        return 1
    from . import review_watch as rw
    state = rw._load_state(lambda m: print(f"warning: {m}", file=sys.stderr))
    key = f"{ticket_id}:needs_human"
    state["sent"] = [k for k in state["sent"] if k != key]
    state["pending"] = [p for p in state["pending"] if not (p["id"] == ticket_id and p["event"] == "needs_human")]
    rw._save_state(state)
    print(f"{ticket_id}  unblocked; the dispatcher may launch again")
    return 0
