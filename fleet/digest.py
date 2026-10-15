"""`fleet digest`: what happened in the last 24 hours, as plain text. Read-only.

Sends nothing and writes nothing. Two sources that already exist:
  * the PING_SENT / PING_FAILED lines of dispatch.log (written by notify.py), which
    carry a time, an event and a ticket id: the informational events listed in EVENTS;
  * the ticket store: tickets that went done (shipped), failed, or were proposed or
    finished as an investigation within the window, by their updated_at/created_at.
The ping lines are the record of what was told to the human; the ticket store fills in
what happened while notifications were off. Ids and event names only, never titles'
bodies or notes. Standard library only.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import dispatch as dp
from . import ticket as tk

WINDOW = timedelta(hours=24)
EVENTS = ("shipped", "investigated", "proposed", "oncall", "usage_pause", "failed")
_LINE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (PING_SENT|PING_FAILED) (\w+) (\S+?)(?::|$)")
_LOG_TAIL_BYTES = 2_000_000   # a day of pings is far smaller; never read an unbounded log


def _parse(stamp) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _log_events(since: datetime) -> list[tuple[datetime, str, str, str]]:
    """(time, event, ticket id, 'sent' or 'not sent') for each informational ping line."""
    path = dp.log_path()
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m or m.group(3) not in EVENTS:
            continue
        when = _parse(m.group(1))
        if when and when >= since:
            out.append((when, m.group(3), m.group(4), "sent" if m.group(2) == "PING_SENT" else "not sent"))
    return out


def _ticket_events(since: datetime) -> list[tuple[datetime, str, str, str]]:
    out = []
    for t in tk.list_tickets():
        updated = _parse(t.get("updated_at"))
        created = _parse(t.get("created_at"))
        if t["status"] == "done" and updated and updated >= since:
            out.append((updated, "shipped", t["id"], "ticket"))
        elif t["status"] == "failed" and updated and updated >= since:
            out.append((updated, "failed", t["id"], "ticket"))
        elif t["status"] == "proposed" and created and created >= since:
            out.append((created, "proposed", t["id"], "ticket"))
        if t["type"] == "investigate" and t["status"] == "done" and updated and updated >= since:
            out.append((updated, "investigated", t["id"], "ticket"))
    return out


def collect(now: datetime | None = None) -> list[tuple[datetime, str, str, str]]:
    """Events of the last 24h, oldest first. A ticket-store entry is dropped when a ping line
    already reports the same event for the same ticket."""
    now = now or datetime.now(timezone.utc)
    since = now - WINDOW
    events = _log_events(since)
    seen = {(e[1], e[2]) for e in events}
    events += [e for e in _ticket_events(since) if (e[1], e[2]) not in seen]
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    return events


def render(now: datetime | None = None) -> str:
    events = collect(now)
    if not events:
        return "Nothing informational in the last 24 hours."
    lines = [f"Last 24 hours: {len(events)} event(s)"]
    for event in EVENTS:
        rows = [e for e in events if e[1] == event]
        if not rows:
            continue
        lines.append(f"{event} ({len(rows)})")
        for when, _, ident, how in rows:
            lines.append(f"  {when.strftime('%H:%M')}  {ident}" + ("" if how == "sent" else f"  [{how}]"))
    return "\n".join(lines)


def main(argv=None) -> int:
    print(render())
    return 0
