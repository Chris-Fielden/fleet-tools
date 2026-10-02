"""Ticket format, validation and storage (phase 1).

Tickets are one JSON file each under $FLEET_HOME/tickets/. Standard library
only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import risk as rk

TYPES = ("build", "review", "fix", "question", "investigate")
PRIORITIES = ("normal", "urgent")
STATUSES = ("proposed", "open", "claimed", "done", "failed", "cancelled")
CREATORS = ("human", "claude", "codex")

FIELDS = (
    "id", "type", "repo", "branch", "commit", "parent", "title", "body",
    "created_by", "assignee", "status", "verdict", "created_at", "updated_at",
)
# Optional fields: may be absent from a stored ticket.
OPTIONAL_FIELDS = ("close_note", "base_commit", "seq", "priority", "risk", "notes", "started_at")
CLOSE_NOTE_MAX = 500
NOTE_MAX = 4000
NOTES_LIMIT = 50  # notes per ticket
BODY_MAX = 4000
AGENTS = ("claude", "codex")
CLOSE_OUTCOMES = {"done": "done", "failed": "failed", "cancelled": "cancelled"}
# Fields a caller may supply when creating a ticket. Everything else in
# FIELDS is set by the tool.
CALLER_FIELDS = ("type", "repo", "branch", "title", "body", "commit", "parent", "priority")

# Short ids like BD-12 (new) and legacy ids like T-20250101-001 (kept as is).
ID_RE = re.compile(r"T-\d{8}-\d{3,}|[A-Z]{2}-[1-9]\d*")
ID_EXAMPLE = "BD-12 or T-20250101-001"

# Id prefix by ticket type. Add a type here (and to TYPES) and it gets its
# prefix. EMERGENCY_PREFIX wins over the type for any ticket whose priority
# field is "urgent"; tickets without a priority field never get it.
TYPE_PREFIX = {
    "build": "BD",
    "fix": "FX",
    "review": "RV",
    "question": "QN",
    "investigate": "ID",
}
EMERGENCY_PREFIX = "EM"
COUNTERS_FILE = ".counters"
AGENT_TICKET_LIMIT = 5      # new tickets per hour, per agent kind
AGENT_TICKET_WINDOW = 3600  # seconds
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


def _real_home() -> Path:
    """The account's real home folder, from the password database. Unlike
    $HOME it cannot be redirected by a test."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def refuse_real_store_in_tests(path=None) -> None:
    """Tripwire: under pytest, never write to the real ~/.fleet (or talk to
    the Kaneo board configured there). Called before every write."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return
    target = Path(path) if path is not None else fleet_home()
    real = _real_home() / ".fleet"
    try:
        inside = target.resolve().is_relative_to(real.resolve())
    except (OSError, RuntimeError):
        inside = True  # cannot tell: fail closed
    if inside:
        raise TicketError(
            f"refusing to write to {target}: a test is running and this is the real "
            f"{real} (the test is not isolated; it must set FLEET_HOME to a temporary folder)")


def tickets_dir() -> Path:
    return fleet_home() / "tickets"


def _ticket_path(ticket_id: str) -> Path:
    return tickets_dir() / f"{ticket_id}.json"


@contextlib.contextmanager
def _locked():
    """Exclusive lock covering id assignment and read-modify-write."""
    refuse_real_store_in_tests()
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


def _is_time(value) -> bool:
    """True for a real UTC time like 2025-01-01T00:00:00Z (rejects 2025-13-45...)."""
    if not _is_str(value) or not TIME_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


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
    elif parent is not None and (not _is_str(parent) or not ID_RE.fullmatch(parent)):
        errors.append("parent must be a ticket id like BD-12 or T-20250101-001")

    if f.get("priority") is not None and f["priority"] not in PRIORITIES:
        errors.append(f"priority must be one of: {', '.join(PRIORITIES)}")

    title = f.get("title")
    if not _is_str(title) or not 1 <= len(title) <= 100:
        errors.append("title must be 1 to 100 characters")
    elif "\n" in title or "\r" in title:
        errors.append("title must be a single line")

    body = f.get("body")
    if not _is_str(body) or not 1 <= len(body) <= BODY_MAX:
        errors.append(f"body must be 1 to {BODY_MAX} characters")


def _check_notes(notes) -> list[str]:
    if not isinstance(notes, list) or not 1 <= len(notes) <= NOTES_LIMIT:
        return [f"notes must be a list of 1 to {NOTES_LIMIT} notes"]
    for n in notes:
        if (not isinstance(n, dict) or set(n) != {"at", "by", "text"} or not _is_time(n["at"])
                or n["by"] not in CREATORS or not _is_str(n["text"])
                or not 1 <= len(n["text"]) <= NOTE_MAX):
            return [f"each note must have a time (at), a creator (by) and text of 1 to {NOTE_MAX} characters"]
    return []


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
    base = data.get("base_commit")
    if "base_commit" in data and (not _is_str(base) or not COMMIT_RE.fullmatch(base)):
        errors.append("base_commit must be a git commit id (7 to 40 hex characters)")
    if "risk" in data and data["risk"] not in rk.LEVELS:
        errors.append(f"risk must be one of: {', '.join(rk.LEVELS)}")
    if "started_at" in data and not _is_time(data["started_at"]):
        errors.append("started_at must be a UTC ISO 8601 time like 2025-01-01T00:00:00Z")
    if "notes" in data:
        errors += _check_notes(data["notes"])
    seq = data.get("seq")
    if "seq" in data and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 1):
        errors.append("seq must be a whole number, 1 or more")
    for name in ("created_at", "updated_at"):
        if not _is_time(data[name]):
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
    refuse_real_store_in_tests(path)
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
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
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


def oldest_first_key(t: dict):
    """Sort key, oldest first: creation time, then the stored creation
    sequence. Tickets from before `seq` existed count as 0, and the id is
    only a last tie-break so the order is stable; nothing is read from it."""
    return (t["created_at"], t.get("seq", 0), t["id"])


def list_tickets() -> list[dict]:
    d = tickets_dir()
    if not d.is_dir():
        return []
    tickets = [_load_path(p, p.stem) for p in d.glob("*.json") if ID_RE.fullmatch(p.stem)]
    tickets.sort(key=oldest_first_key, reverse=True)
    return tickets


def id_prefix(fields: dict) -> str:
    """The id prefix for a new ticket: urgent wins, otherwise by type."""
    if fields.get("priority") == "urgent":
        return EMERGENCY_PREFIX
    return TYPE_PREFIX[fields["type"]]


def _read_counters() -> dict:
    try:
        data = json.loads((tickets_dir() / COUNTERS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise TicketError(f"cannot read the id counters: {exc}") from None
    ok = isinstance(data, dict) and all(
        _is_str(k) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
        for k, v in data.items())
    if not ok:
        raise TicketError("the id counters file is damaged; fix or delete it")
    return data


def _assign_ids(prefix: str) -> tuple[str, int]:
    """Next id for `prefix` and the next creation sequence. Caller holds the
    lock. Counters never go below what is already on disk (highest number for
    the prefix, highest seq overall), so a lost or stale counters file can
    neither repeat an id nor make a new ticket sort before an older one."""
    counters = _read_counters()
    highest = counters.get(prefix, 0)
    seq = counters.get("seq", 0)
    pat = re.compile(re.escape(prefix) + r"-([1-9]\d*)")
    for p in tickets_dir().glob("*.json"):
        if not ID_RE.fullmatch(p.stem):
            continue
        m = pat.fullmatch(p.stem)
        if m:
            highest = max(highest, int(m.group(1)))
        try:
            stored = json.loads(p.read_text(encoding="utf-8")).get("seq", 0)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(stored, int) and not isinstance(stored, bool):
            seq = max(seq, stored)
    counters[prefix] = highest + 1
    counters["seq"] = seq + 1
    _write_atomic(tickets_dir() / COUNTERS_FILE, counters)
    return f"{prefix}-{highest + 1}", seq + 1


def _check_agent_rate(creator: str) -> None:
    """Refuse when an agent kind has already made too many tickets in the last
    hour. Counted from the stored tickets; caller holds the lock."""
    if creator == "human":
        return
    cutoff = datetime.now(timezone.utc).timestamp() - AGENT_TICKET_WINDOW
    recent = 0
    for p in tickets_dir().glob("*.json"):
        if not ID_RE.fullmatch(p.stem):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            made = datetime.strptime(data["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
            who = data["created_by"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if who == creator and made > cutoff:
            recent += 1
    if recent >= AGENT_TICKET_LIMIT:
        raise TicketError(
            f"too many new tickets: {creator} has already made {recent} in the last hour "
            f"(the limit is {AGENT_TICKET_LIMIT}). Wait, or ask the human to create it.")


def create_ticket(fields: dict) -> dict:
    """Validate, check for secrets, assign an id and save. Returns the ticket."""
    creator = current_creator()
    errors = validate_new(fields)
    if not errors:
        errors += check_secrets(fields["title"], fields["body"])
    if errors:
        raise TicketInvalid(errors)

    t = fields["type"]
    # Investigate tickets always wait for approval; everything else starts
    # open (the human approves at merge). A build ticket that follows up an
    # investigation (it has a parent) also waits for approval.
    status = "proposed" if t == "investigate" or (t == "build" and fields.get("parent")) else "open"
    try:
        start_risk = rk.starting_level(t, fields["repo"])
    except rk.RiskError as exc:
        raise TicketError(str(exc)) from None

    with _locked():
        _check_agent_rate(creator)
        parent = fields.get("parent")
        if parent is not None:
            try:
                parent_ticket = _load_path(_ticket_path(parent), parent)
            except TicketError:
                raise TicketInvalid([f"parent ticket {parent} does not exist or is unreadable"]) from None
            if t == "build" and parent_ticket["type"] != "investigate":
                raise TicketInvalid(["a build ticket's parent must be an investigate ticket"])
        now = _now()
        new_id, seq = _assign_ids(id_prefix(fields))
        ticket = {
            "id": new_id,
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
            "seq": seq,
            "risk": start_risk,
        }
        if fields.get("priority") is not None:
            ticket["priority"] = fields["priority"]
        _write_atomic(_ticket_path(ticket["id"]), ticket)
    return ticket


def approve_ticket(ticket_id: str) -> dict:
    """Turn a proposed ticket (build or investigate) into an open one. Humans only."""
    creator = current_creator()
    if creator != "human":
        raise TicketError(f"only a human can approve tickets (running as {creator})")
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["status"] != "proposed":
            raise TicketError(
                f"{ticket_id} is a {ticket['status']} {ticket['type']} ticket; "
                "only proposed tickets can be approved"
            )
        ticket["status"] = "open"
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


_KEEP = object()


def set_status(ticket_id: str, status: str, *, allowed_from, assignee=_KEEP,
               close_note: str | None = None, base_commit: str | None = None,
               started_at: str | None = None) -> dict:
    """Move a ticket to `status` atomically, under the store lock. Refuses
    unless its current status is in `allowed_from`. Returns the new ticket."""
    if status not in STATUSES:
        raise TicketError(f"unknown status: {status}")
    if assignee is not _KEEP and assignee is not None and assignee not in AGENTS:
        raise TicketError(f"assignee must be one of: {', '.join(AGENTS)}")
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["status"] not in allowed_from:
            raise TicketError(
                f"{ticket_id} is {ticket['status']}; it can only change from: "
                + ", ".join(allowed_from))
        ticket["status"] = status
        if assignee is not _KEEP:
            ticket["assignee"] = assignee
        if close_note is not None:
            ticket["close_note"] = close_note
        if base_commit is not None:
            if not COMMIT_RE.fullmatch(base_commit):
                raise TicketError("base_commit must be a git commit id")
            ticket["base_commit"] = base_commit
        if started_at is not None:
            ticket["started_at"] = started_at
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def add_note(ticket_id: str, text: str) -> dict:
    """Append a note (findings, a remark) to a ticket. Anyone may add one, so
    it is checked for secrets and kept small."""
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
    if not _is_str(text) or not 1 <= len(text) <= NOTE_MAX:
        raise TicketInvalid([f"note must be 1 to {NOTE_MAX} characters"])
    errors = check_secrets("note", text)
    if errors:
        raise TicketInvalid(errors)
    who = current_creator()
    with _locked():
        ticket = load_ticket(ticket_id)
        notes = ticket.get("notes", [])
        if len(notes) >= NOTES_LIMIT:
            raise TicketError(f"{ticket_id} already has {NOTES_LIMIT} notes, the most it can hold")
        now = _now()
        ticket["notes"] = notes + [{"at": now, "by": who, "text": text}]
        ticket["updated_at"] = now
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def close_ticket(ticket_id: str, outcome: str, note: str | None = None) -> dict:
    """Close an open or claimed ticket as done, failed or cancelled. Humans only."""
    creator = current_creator()
    if creator != "human":
        raise TicketError(f"only a human can close tickets (running as {creator})")
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
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


def raise_risk(ticket_id: str, level: str) -> dict:
    """Raise a ticket's stored risk level. Humans only, and only upwards:
    nobody can lower a level."""
    creator = current_creator()
    if creator != "human":
        raise TicketError(f"only a human can raise a ticket's risk level (running as {creator})")
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
    if level not in rk.LEVELS:
        raise TicketInvalid([f"level must be one of: {', '.join(rk.LEVELS)}"])
    with _locked():
        ticket = load_ticket(ticket_id)
        try:
            current = rk.stored_level(ticket)
        except rk.RiskError as exc:
            raise TicketError(str(exc)) from None
        if rk.rank(level) <= rk.rank(current):
            raise TicketError(f"{ticket_id} is already {current}; a risk level can only be raised")
        ticket["risk"] = level
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket
