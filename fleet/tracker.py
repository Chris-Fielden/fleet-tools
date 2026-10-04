"""The tracker interface: where tickets live, behind one set of operations.

Callers (the `ticket` command, the dispatcher, the review watcher) ask a
Tracker to list what is eligible, show, create, claim, set a status, add a
note, set the risk label or close a ticket, and never touch a backend directly.

The first backend, LocalTracker, is the JSON store under $FLEET_HOME/tickets
(source of truth) plus the one-way Kaneo mirror, exactly as before. Another
backend only has to implement Tracker and be named in get_tracker().
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod

from . import dispatch as dp
from . import kaneo
from . import settings as st
from . import ticket as tk


def _warn(message: str) -> None:
    print(f"ticket: {message}", file=sys.stderr)


class Tracker(ABC):
    """Operations every ticket backend provides. Tickets are the dicts
    documented in docs/ticket-spec.md; rules (who may do what, which status
    may follow which) are enforced by the backend and raise tk.TicketError /
    tk.TicketInvalid with a plain message."""

    name: str

    @abstractmethod
    def list_tickets(self) -> list[dict]:
        """Every ticket, newest first."""

    @abstractmethod
    def list_eligible(self, settings: dict) -> list[dict]:
        """Tickets the dispatcher may launch now, in the order it would."""

    @abstractmethod
    def show(self, ticket_id: str) -> dict: ...

    @abstractmethod
    def create(self, fields: dict, *, needs_approval: bool = False) -> dict: ...

    @abstractmethod
    def approve(self, ticket_id: str) -> dict:
        """proposed -> open (humans only)."""

    @abstractmethod
    def claim(self, ticket_id: str, agent: str, *, allowed_from=("open",),
              base_commit: str | None = None, started_at: str | None = None) -> dict:
        """Move to claimed for `agent`."""

    @abstractmethod
    def set_status(self, ticket_id: str, status: str, *, allowed_from, **fields) -> dict:
        """Move to `status` if it currently has one of `allowed_from`. Extra
        fields (assignee, close_note, base_commit, started_at) as in
        tk.set_status."""

    @abstractmethod
    def add_note(self, ticket_id: str, text: str) -> dict: ...

    @abstractmethod
    def set_risk(self, ticket_id: str, level: str) -> dict:
        """Raise the stored risk level (never lowers it; humans only)."""

    @abstractmethod
    def close(self, ticket_id: str, outcome: str, note: str | None = None) -> dict:
        """done, failed or cancelled (humans only)."""

    @abstractmethod
    def check(self) -> list[tuple[str, str, str]]:
        """Health rows for `fleet doctor`: (state, what, detail), state being
        ok, warn or fail. Never includes a secret."""


class LocalTracker(Tracker):
    """The local JSON store, mirrored one way onto Kaneo when it is set up.
    A Kaneo problem is a warning (`warn`), never a failed operation."""

    name = "local"

    def __init__(self, warn=_warn):
        self._warn = warn

    def _mirrored(self, ticket: dict) -> dict:
        kaneo.mirror_quietly(ticket, self._warn)
        return ticket

    def list_tickets(self):
        return tk.list_tickets()

    def list_eligible(self, settings):
        tickets = tk.list_tickets()
        decisions = dp.decide_investigations(tickets, settings) + dp.decide(tickets, settings)
        return [d.ticket for d in decisions if d.eligible]

    def show(self, ticket_id):
        return tk.load_ticket(ticket_id)

    def create(self, fields, *, needs_approval=False):
        return self._mirrored(tk.create_ticket(fields, needs_approval=needs_approval))

    def approve(self, ticket_id):
        return self._mirrored(tk.approve_ticket(ticket_id))

    def claim(self, ticket_id, agent, *, allowed_from=("open",), base_commit=None, started_at=None):
        return self.set_status(ticket_id, "claimed", allowed_from=allowed_from, assignee=agent,
                               base_commit=base_commit, started_at=started_at)

    def set_status(self, ticket_id, status, *, allowed_from, **fields):
        return self._mirrored(tk.set_status(ticket_id, status, allowed_from=allowed_from, **fields))

    def add_note(self, ticket_id, text):
        return self._mirrored(tk.add_note(ticket_id, text))

    def set_risk(self, ticket_id, level):
        return self._mirrored(tk.raise_risk(ticket_id, level))

    def close(self, ticket_id, outcome, note=None):
        return self._mirrored(tk.close_ticket(ticket_id, outcome, note))

    def check(self):
        rows = []
        home = tk.fleet_home()
        rows.append(("ok" if home.is_dir() else "warn", "ticket store",
                     f"{home}/tickets" if home.is_dir() else f"{home} does not exist yet (made on the first ticket)"))
        try:
            n = len(tk.list_tickets())
            rows.append(("ok", "tickets readable", f"{n} ticket(s)"))
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            rows.append(("fail", "tickets readable", str(exc)))
        rows.append(self._check_kaneo())
        return rows

    @staticmethod
    def _check_kaneo() -> tuple[str, str, str]:
        try:
            client = kaneo.Client(kaneo.load_config())
        except kaneo.NotConfigured:
            return ("warn", "kaneo mirror", f"off: no {kaneo.config_path()} (optional)")
        except kaneo.KaneoError as exc:
            return ("fail", "kaneo mirror", str(exc))
        try:
            client.request("GET", "/project", query={"workspaceId": client.workspace_id})
        except kaneo.KaneoError as exc:
            return ("fail", "kaneo reachable", str(exc))
        return ("ok", "kaneo reachable", f"{client.base_url}, workspace {client.workspace_id}")


BACKENDS = {"local": LocalTracker}


def get_tracker(warn=_warn, settings: dict | None = None) -> Tracker:
    """The backend named by the `tracker` setting."""
    name = (settings or st.load_settings())["tracker"]
    try:
        return BACKENDS[name](warn=warn)
    except KeyError:
        raise st.SettingsError(f"tracker '{name}' is not one of: {', '.join(BACKENDS)}") from None
