"""Ticket format, validation and storage (phase 1).

Tickets are one JSON file each under $FLEET_HOME/tickets/. Standard library
only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TYPES = ("build", "review", "fix", "question")
STATUSES = ("proposed", "open", "claimed", "done", "failed", "cancelled")
CREATORS = ("human", "claude", "codex")

FIELDS = (
    "id", "type", "repo", "branch", "commit", "parent", "title", "body",
    "created_by", "assignee", "status", "verdict", "created_at", "updated_at",
)
# Optional fields: may be absent from a stored ticket.
OPTIONAL_FIELDS = ("close_note",)
CLOSE_NOTE_MAX = 500
AGENTS = ("claude", "codex")
CLOSE_OUTCOMES = {"done": "done", "failed": "failed", "cancelled": "cancelled"}
# Fields a caller may supply when creating a ticket. Everything else in
# FIELDS is set by the tool.
CALLER_FIELDS = ("type", "repo", "branch", "title", "body", "commit", "parent")

ID_RE = re.compile(r"T-\d{8}-\d{3,}")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
BRANCH_RE = re.compile(r"task/[a-z0-9-]{1,64}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")

GITLEAKS_INSTALL_HINT = (
    "install gitleaks (https://github.com/gitleaks/gitleaks#installing) "
    "or set FLEET_GITLEAKS to its path"
)


class TicketInvalid(Exception):
    """The input is wrong. Carries every problem found, one string each."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class TicketError(Exception):
    """Anything else that stops a command (missing ticket, refusal, I/O)."""


# --- locations ---------------------------------------------------------------

def fleet_home() -> Path:
    return Path(os.environ.get("FLEET_HOME") or Path.home() / ".fleet")


def tickets_dir() -> Path:
    return fleet_home() / "tickets"


def _ticket_path(ticket_id: str) -> Path:
    return tickets_dir() / f"{ticket_id}.json"


@contextlib.contextmanager
def _locked():
    """Exclusive lock covering id assignment and read-modify-write."""
    d = tickets_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- environment -------------------------------------------------------------

def current_creator(env=None) -> str:
    env = os.environ if env is None else env
    if "CLAUDECODE" in env:
        return "claude"
    if "CODEX_SANDBOX" in env or "CODEX_SANDBOX_NETWORK_DISABLED" in env:
        return "codex"
    return "human"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- validation --------------------------------------------------------------

def _is_str(value) -> bool:
    return isinstance(value, str)


def _check_common(f: dict, errors: list[str]) -> None:
    """Field-level rules shared by creation and reading. Parent existence is
    checked separately because it needs the store."""
    t = f.get("type")
    if t not in TYPES:
        errors.append(f"type must be one of: {', '.join(TYPES)}")

    repo = f.get("repo")
    if not _is_str(repo) or not REPO_RE.fullmatch(repo):
        errors.append("repo must be 1 to 64 characters: letters, digits, '.', '_' or '-'")

    branch = f.get("branch")
    if not _is_str(branch) or not BRANCH_RE.fullmatch(branch):
        errors.append(
            "branch must start with 'task/' followed by 1 to 64 characters: "
            "lowercase letters, digits or '-'"
        )

    commit = f.get("commit")
    if t == "review" and commit is None:
        errors.append("a review ticket needs a commit")
    elif commit is not None:
        if t in TYPES and t != "review":
            errors.append(f"a {t} ticket must not have a commit")
        elif not _is_str(commit) or not COMMIT_RE.fullmatch(commit):
            errors.append("commit must be 7 to 40 lowercase hex characters")

    parent = f.get("parent")
    if t in ("review", "fix") and parent is None:
        errors.append(f"a {t} ticket needs a parent ticket id")
    elif t == "build" and parent is not None:
        errors.append("a build ticket must not have a parent")
    elif parent is not None and (not _is_str(parent) or not ID_RE.fullmatch(parent)):
        errors.append("parent must be a ticket id like T-20250101-001")

    title = f.get("title")
    if not _is_str(title) or not 1 <= len(title) <= 100:
        errors.append("title must be 1 to 100 characters")
    elif "\n" in title or "\r" in title:
        errors.append("title must be a single line")

    body = f.get("body")
    if not _is_str(body) or not 1 <= len(body) <= 4000:
        errors.append("body must be 1 to 4000 characters")


def validate_new(fields: dict) -> list[str]:
    """Return every problem with caller-supplied fields (empty list = fine)."""
    errors: list[str] = []
    for name in sorted(set(fields) - set(CALLER_FIELDS)):
        if name in FIELDS:
            errors.append(f"{name} is set by the tool and cannot be supplied")
        else:
            errors.append(f"unknown field: {name}")
    f = {k: fields.get(k) for k in CALLER_FIELDS}
    _check_common(f, errors)
    return errors


def validate_stored(data) -> list[str]:
    """Return every problem with a ticket as read from disk."""
    if not isinstance(data, dict):
        return ["ticket file is not a JSON object"]
    errors: list[str] = []
    for name in sorted(set(data) - set(FIELDS) - set(OPTIONAL_FIELDS)):
        errors.append(f"unknown field: {name}")
    for name in FIELDS:
        if name not in data:
            errors.append(f"missing field: {name}")
    if errors:
        return errors
    if not _is_str(data["id"]) or not ID_RE.fullmatch(data["id"]):
        errors.append("id is not a valid ticket id")
    _check_common(data, errors)
    if data["created_by"] not in CREATORS:
        errors.append(f"created_by must be one of: {', '.join(CREATORS)}")
    if data["status"] not in STATUSES:
        errors.append(f"status must be one of: {', '.join(STATUSES)}")
    if data["assignee"] is not None and data["assignee"] not in AGENTS:
        errors.append(f"assignee must be null or one of: {', '.join(AGENTS)}")
    if data["verdict"] is not None:
        errors.append("verdict must be null in phase 1")
    note = data.get("close_note")
    if "close_note" in data and (not _is_str(note) or not 1 <= len(note) <= CLOSE_NOTE_MAX):
        errors.append(f"close_note must be 1 to {CLOSE_NOTE_MAX} characters")
    for name in ("created_at", "updated_at"):
        if not _is_str(data[name]) or not TIME_RE.fullmatch(data[name]):
            errors.append(f"{name} must be a UTC ISO 8601 time like 2025-01-01T00:00:00Z")
    return errors


# --- secret check ------------------------------------------------------------

def check_secrets(title: str, body: str) -> list[str]:
    """Pipe title and body to gitleaks. Fails closed on any doubt."""
    exe = os.environ.get("FLEET_GITLEAKS") or "gitleaks"
    cmd = [exe, "stdin", "--no-banner", "--redact", "--log-level", "error"]
    try:
        proc = subprocess.run(
            cmd, input=f"{title}\n{body}\n", text=True,
            capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        return [f"gitleaks is not installed, so the ticket cannot be checked for secrets: {GITLEAKS_INSTALL_HINT}"]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"could not run gitleaks ({exc}); the ticket was not saved"]
    if proc.returncode != 0:
        return ["gitleaks found what looks like a secret in the title or body (or failed to run); "
                "remove it and refer to the secret by name instead"]
    return []


# --- storage -----------------------------------------------------------------

def _write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def load_ticket(ticket_id: str) -> dict:
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like T-20250101-001)"])
    return _load_path(_ticket_path(ticket_id), ticket_id)


def _load_path(path: Path, label: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TicketError(f"no such ticket: {label}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TicketError(f"cannot read ticket {label}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TicketError(f"ticket file {path.name} is not valid JSON: {exc}") from None
    problems = validate_stored(data)
    if problems:
        raise TicketError(f"ticket file {path.name} is invalid: " + "; ".join(problems))
    if data["id"] != path.stem:
        raise TicketError(f"ticket file {path.name} contains a different id ({data['id']})")
    return data


def _sort_key(t: dict):
    day, seq = t["id"].split("-")[1:]
    return (t["created_at"], day, int(seq))


def list_tickets() -> list[dict]:
    d = tickets_dir()
    if not d.is_dir():
        return []
    tickets = [_load_path(p, p.stem) for p in d.glob("T-*.json")]
    tickets.sort(key=_sort_key, reverse=True)
    return tickets


def _next_id(day: str) -> str:
    prefix = f"T-{day}-"
    highest = 0
    for p in tickets_dir().glob(f"{prefix}*.json"):
        tail = p.stem[len(prefix):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:03d}"


def create_ticket(fields: dict) -> dict:
    """Validate, check for secrets, assign an id and save. Returns the ticket."""
    creator = current_creator()
    errors = validate_new(fields)
    if not errors:
        errors += check_secrets(fields["title"], fields["body"])
    if errors:
        raise TicketInvalid(errors)

    t = fields["type"]
    if t == "build" and creator == "human":
        status = "open"
    elif t == "build":
        status = "proposed"
    else:
        status = "open"

    with _locked():
        parent = fields.get("parent")
        if parent is not None:
            try:
                _load_path(_ticket_path(parent), parent)
            except TicketError:
                raise TicketInvalid([f"parent ticket {parent} does not exist or is unreadable"]) from None
        now = _now()
        ticket = {
            "id": _next_id(datetime.now(timezone.utc).strftime("%Y%m%d")),
            "type": t,
            "repo": fields["repo"],
            "branch": fields["branch"],
            "commit": fields.get("commit"),
            "parent": parent,
            "title": fields["title"],
            "body": fields["body"],
            "created_by": creator,
            "assignee": None,
            "status": status,
            "verdict": None,
            "created_at": now,
            "updated_at": now,
        }
        _write_atomic(_ticket_path(ticket["id"]), ticket)
    return ticket


def approve_ticket(ticket_id: str) -> dict:
    """Turn a proposed build ticket into an open one. Humans only."""
    creator = current_creator()
    if creator != "human":
        raise TicketError(f"only a human can approve tickets (running as {creator})")
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like T-20250101-001)"])
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["type"] != "build" or ticket["status"] != "proposed":
            raise TicketError(
                f"{ticket_id} is a {ticket['status']} {ticket['type']} ticket; "
                "only proposed build tickets can be approved"
            )
        ticket["status"] = "open"
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def set_status(ticket_id: str, status: str, *, allowed_from, assignee=None,
               close_note: str | None = None) -> dict:
    """Move a ticket to `status` atomically, under the store lock. Refuses
    unless its current status is in `allowed_from`. Returns the new ticket."""
    if status not in STATUSES:
        raise TicketError(f"unknown status: {status}")
    if assignee is not None and assignee not in AGENTS:
        raise TicketError(f"assignee must be one of: {', '.join(AGENTS)}")
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["status"] not in allowed_from:
            raise TicketError(
                f"{ticket_id} is {ticket['status']}; it can only change from: "
                + ", ".join(allowed_from))
        ticket["status"] = status
        if assignee is not None:
            ticket["assignee"] = assignee
        if close_note is not None:
            ticket["close_note"] = close_note
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def close_ticket(ticket_id: str, outcome: str, note: str | None = None) -> dict:
    """Close an open or claimed ticket as done, failed or cancelled. Humans only."""
    creator = current_creator()
    if creator != "human":
        raise TicketError(f"only a human can close tickets (running as {creator})")
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like T-20250101-001)"])
    if outcome not in CLOSE_OUTCOMES:
        raise TicketInvalid([f"outcome must be one of: {', '.join(CLOSE_OUTCOMES)}"])
    if note is not None:
        errors = []
        if not 1 <= len(note) <= CLOSE_NOTE_MAX:
            errors.append(f"note must be 1 to {CLOSE_NOTE_MAX} characters")
        else:
            errors += check_secrets("close note", note)
        if errors:
            raise TicketInvalid(errors)
    return set_status(ticket_id, CLOSE_OUTCOMES[outcome],
                      allowed_from=("open", "claimed"), close_note=note)
