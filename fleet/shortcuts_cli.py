"""The `idea` and `alert` commands: one-line ways to file a ticket."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import notify
from . import ticket_cli
from . import settings as st
from . import ticket as tk
from . import tracker as tr

IDEAS_REPO = "ideas"


def _warn(message: str) -> None:
    print(f"ticket: {message}", file=sys.stderr)


def _file(prog: str, fields: dict) -> int:
    try:
        ticket_cli.guard_duplicates(fields, prog=prog)
        ticket = tr.get_tracker(_warn).create(fields)
    except tk.TicketInvalid as exc:
        for line in exc.errors:
            print(f"{prog}: {line}", file=sys.stderr)
        return 2
    except (tk.TicketError, st.SettingsError, OSError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(ticket["id"])
    notify.notify_quietly(ticket, notify.event_for_new(ticket), _warn)
    return 0


def _branch(kind: str) -> str:
    return f"task/{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


def _text(words: list[str]) -> str:
    return " ".join(words).strip()


def _title(text: str) -> str:
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")[:100].strip()


def _alert_title(text: str) -> str:
    return tk.first_line_title(text)


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
                           "title": _alert_title(text), "body": text, "priority": "urgent"})
