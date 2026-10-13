"""The fleet's Prometheus text exposition, served at GET /metrics by the status endpoint.

Read-only and built fresh on every scrape from the ticket store, the launch
counter and the dispatcher lock. Numbers and ticket ids only: no ticket titles,
notes, paths or secrets. All series are gauges (launches reset each day).

    fleet_tickets{status}          tickets per status, every status in ticket.STATUSES
    fleet_launches_today           launches used today (left out when unreadable)
    fleet_launches_max_per_day     the daily launch cap
    fleet_agents_running           tickets being worked on (claimed)
    fleet_ready_to_ship            tickets waiting for the human (in review)
    fleet_dispatch_up              1 when dispatch --watch holds its lock
    fleet_review_rounds{ticket}    reviewed commits, for active tickets only
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import dispatch as dp
from . import service as sv
from . import settings as st
from . import ticket as tk

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    tickets = tk.list_tickets()
    settings = st.load_settings()
    try:
        used = dp.launches_today(dp.local_day(now, settings["timezone"]))
    except OSError:
        used = None
    by_status = {s: 0 for s in tk.STATUSES}
    for t in tickets:
        if t["status"] in by_status:
            by_status[t["status"]] += 1

    lines: list[str] = []

    def metric(name: str, help_text: str, samples: list[tuple[str, int]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.extend(f"{name}{labels} {value}" for labels, value in samples)

    metric("fleet_tickets", "Tickets by status.",
           [(f'{{status="{_label(s)}"}}', n) for s, n in by_status.items()])
    if used is not None:
        metric("fleet_launches_today", "Agent launches used today.", [("", used)])
    metric("fleet_launches_max_per_day", "Daily agent launch cap.", [("", settings["max_launches_per_day"])])
    metric("fleet_agents_running", "Tickets being worked on.", [("", by_status["claimed"])])
    metric("fleet_ready_to_ship", "Tickets waiting for the human to review and ship.", [("", by_status["review"])])
    metric("fleet_dispatch_up", "1 when dispatch --watch is running.", [("", int(bool(sv.dispatcher_running())))])
    active = sorted((t for t in tickets if t["status"] in tk.ACTIVE_STATUSES), key=lambda t: t["id"])
    metric("fleet_review_rounds", "Commits reviewed so far, for active tickets.",
           [(f'{{ticket="{_label(t["id"])}"}}', tk.review_rounds(t)) for t in active])
    return "\n".join(lines) + "\n"
