"""The `idea` and `alert` commands: one-line ways to file a ticket."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import kaneo
from . import ticket as tk

IDEAS_REPO = "ideas"


def _warn(message: str) -> None:
    print(f"ticket: {message}", file=sys.stderr)


def _file(prog: str, fields: dict) -> int:
    try:
        ticket = tk.create_ticket(fields)
    except tk.TicketInvalid as exc:
        for line in exc.errors:
            print(f"{prog}: {line}", file=sys.stderr)
        return 2
    except (tk.TicketError, OSError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(ticket["id"])
    kaneo.mirror_quietly(ticket, _warn)
    return 0


def _branch(kind: str) -> str:
    return f"task/{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def _text(words: list[str]) -> str:
    return " ".join(words).strip()


def _title(text: str) -> str:
    return " ".join(text.split())[:100].strip()


def idea_main(argv=None) -> int:
    words = sys.argv[1:] if argv is None else argv
    text = _text(words)
    if not text:
        print("usage: idea TEXT\n"
              f"  Files an investigate ticket in the '{IDEAS_REPO}' repo for the human to approve.\n"
              "  Prints the new ticket id.", file=sys.stderr)
        return 2
    return _file("idea", {"type": "investigate", "repo": IDEAS_REPO, "branch": _branch("idea"),
                          "title": _title(text), "body": text})


def alert_main(argv=None) -> int:
    words = sys.argv[1:] if argv is None else argv
    text = _text(words[1:])
    if not words or not text:
        print("usage: alert REPO TEXT\n"
              "  Files an urgent build ticket in REPO. Prints the new ticket id.",
              file=sys.stderr)
        return 2
    return _file("alert", {"type": "build", "repo": words[0], "branch": _branch("alert"),
                           "title": _title(text), "body": text, "priority": "urgent"})
