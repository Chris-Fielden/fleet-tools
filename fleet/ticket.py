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
STATUSES = ("proposed", "open", "claimed", "review", "done", "failed", "cancelled")
# "review" (Kaneo: In Review) is set only by the dispatcher, on a recorded PASS.
ACTIVE_STATUSES = ("proposed", "open", "claimed", "review")  # not yet finished
CREATORS = ("human", "claude", "codex")

FIELDS = (
    "id", "type", "repo", "branch", "commit", "parent", "title", "body",
    "created_by", "assignee", "status", "verdict", "created_at", "updated_at",
)
# Optional fields: may be absent from a stored ticket.
OPTIONAL_FIELDS = ("close_note", "base_commit", "seq", "priority", "risk", "notes", "started_at",
                   "oncall", "oncall_base", "fail_commits")
# oncall: true on the fix ticket the dispatcher files for the on-call agent.
# oncall_base: the failed branch's tip when the ticket failed; the on-call branch
# starts from exactly that commit, however long it waits for the on-call slot.
# fail_commits: every commit that got a recorded FAIL, oldest first (a verdict
# only keeps the latest, and "going in circles" counts rounds).
CLOSE_NOTE_MAX = 500
FAIL_COMMITS_MAX = 50   # fail_commits keeps the newest this many (the going-in-circles threshold is far lower)
NOTE_MAX = 4000
REVIEW_KINDS = ("codex", "claude")
VERDICT_RESULTS = ("PASS", "FAIL")
REVIEWER_MAX = 80
NOTES_LIMIT = 50  # notes per ticket
BODY_MAX = 4000
ONCALL_BODY_MAX = 12000  # an on-call ticket carries the failed ticket's whole spec (up to BODY_MAX) plus context
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
ONCALL_PREFIX = "OC"
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


def _check_verdict(v) -> list[str]:
    """A stored verdict: the result of the review of one exact commit."""
    ok = (isinstance(v, dict) and set(v) == {"result", "commit", "reviewer", "at", "reviews"}
          and v["result"] in VERDICT_RESULTS and _is_str(v["commit"])
          and re.fullmatch(r"[0-9a-f]{40}", v["commit"]) and _is_str(v["reviewer"])
          and 1 <= len(v["reviewer"]) <= REVIEWER_MAX and _is_time(v["at"])
          and isinstance(v["reviews"], dict) and v["reviews"]
          and all(k in REVIEW_KINDS and isinstance(r, dict) and set(r) == {"result", "reviewer"}
                  and r["result"] in VERDICT_RESULTS and _is_str(r["reviewer"])
                  and 1 <= len(r["reviewer"]) <= REVIEWER_MAX
                  for k, r in v["reviews"].items())
          and v["result"] == ("FAIL" if any(r["result"] == "FAIL" for r in v["reviews"].values())
                              else "PASS"))
    if not ok:
        return ["verdict must be null or {result: PASS or FAIL, commit: a full commit id, "
                "reviewer, at: a UTC time, reviews: {codex or claude: {result, reviewer}}}"]
    return []


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
    long_ok = data.get("oncall") is True and _is_str(data["body"]) and len(data["body"]) <= ONCALL_BODY_MAX
    _check_common({**data, "body": data["body"][:BODY_MAX]} if long_ok else data, errors)
    if data["created_by"] not in CREATORS:
        errors.append(f"created_by must be one of: {', '.join(CREATORS)}")
    if data["status"] not in STATUSES:
        errors.append(f"status must be one of: {', '.join(STATUSES)}")
    if data["assignee"] is not None and data["assignee"] not in AGENTS:
        errors.append(f"assignee must be null or one of: {', '.join(AGENTS)}")
    if data["verdict"] is not None:
        errors += _check_verdict(data["verdict"])
    note = data.get("close_note")
    if "close_note" in data and (not _is_str(note) or not 1 <= len(note) <= CLOSE_NOTE_MAX):
        errors.append(f"close_note must be 1 to {CLOSE_NOTE_MAX} characters")
    base = data.get("base_commit")
    if "base_commit" in data and (not _is_str(base) or not COMMIT_RE.fullmatch(base)):
        errors.append("base_commit must be a git commit id (7 to 40 hex characters)")
    if "risk" in data and data["risk"] not in rk.LEVELS:
        errors.append(f"risk must be one of: {', '.join(rk.LEVELS)}")
    if "oncall" in data and data["oncall"] is not True:
        errors.append("oncall must be true when present")
    base_tip = data.get("oncall_base")
    if "oncall_base" in data and not (_is_str(base_tip) and re.fullmatch(r"[0-9a-f]{40}", base_tip)
                                      and data.get("oncall") is True):
        errors.append("oncall_base must be a full commit id, on an on-call ticket")
    fails = data.get("fail_commits")
    if "fail_commits" in data and not (
            isinstance(fails, list) and len(fails) <= FAIL_COMMITS_MAX and all(_is_str(c) and re.fullmatch(r"[0-9a-f]{40}", c) for c in fails)):
        errors.append(f"fail_commits must be a list of at most {FAIL_COMMITS_MAX} full commit ids")
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


def id_prefix(fields: dict, oncall: bool = False) -> str:
    """The id prefix for a new ticket: on-call, then urgent, otherwise by type."""
    from . import settings  # here, not at the top: settings imports this module
    try:
        cfg = settings.load_settings()
    except settings.SettingsError as exc:
        raise TicketError(str(exc)) from None
    if oncall:
        return cfg["oncall_prefix"]
    if fields.get("priority") == "urgent":
        return cfg["emergency_prefix"]
    return cfg["id_prefixes"][fields["type"]]


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


def create_ticket(fields: dict, *, needs_approval: bool = False, oncall: bool = False,
                  oncall_base: str | None = None) -> dict:
    """Validate, check for secrets, assign an id and save. Returns the ticket.
    `oncall` (the dispatcher only, a fix ticket) gives it the on-call prefix and flag."""
    creator = current_creator()
    if oncall and (creator != "human" or fields.get("type") != "fix"):
        raise TicketError("only the dispatcher can file an on-call fix ticket")
    if oncall_base is not None and not (oncall and re.fullmatch(r"[0-9a-f]{40}", oncall_base)):
        raise TicketError("oncall_base must be a full commit id, on an on-call ticket")
    # An on-call body may be longer than BODY_MAX: every other rule still applies.
    clipped = oncall and isinstance(fields.get("body"), str) and len(fields["body"]) <= ONCALL_BODY_MAX
    errors = validate_new({**fields, "body": fields["body"][:BODY_MAX]} if clipped else fields)
    if not errors:
        errors += check_secrets(fields["title"], fields["body"])
    if errors:
        raise TicketInvalid(errors)

    t = fields["type"]
    # Investigate tickets always wait for approval; everything else starts
    # open (the human approves at merge). A build ticket that follows up an
    # investigation (it has a parent) also waits for approval.
    # needs_approval: the caller wants a human to approve it first.
    status = ("proposed" if needs_approval or t == "investigate" or (t == "build" and fields.get("parent"))
              else "open")
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
        new_id, seq = _assign_ids(id_prefix(fields, oncall))
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
        if oncall:
            ticket["oncall"] = True
            if oncall_base is not None:
                ticket["oncall_base"] = oncall_base
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
    if status == "review" and current_creator() != "human":
        raise TicketError(f"only the dispatcher (a human's process) can put a ticket in review "
                          f"(running as {current_creator()})")
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
        if started_at == "":
            ticket.pop("started_at", None)  # "" clears it
        elif started_at is not None:
            ticket["started_at"] = started_at
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def finish_investigation(ticket_id: str) -> dict:
    """A claimed investigation's agent has stopped: done if it recorded notes,
    failed if not. Decided from the notes as stored right now, under the lock,
    so findings saved a moment ago count."""
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["status"] != "claimed" or ticket["type"] != "investigate":
            raise TicketError(f"{ticket_id} is not a claimed investigation")
        if ticket.get("notes"):
            ticket["status"] = "done"
        else:
            ticket["status"] = "failed"
            ticket["close_note"] = "the research agent stopped without recording any findings"
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


def append_to_body(ticket_id: str, heading: str, items: str) -> dict | None:
    """Add the lines of `items` that the ticket body does not already hold,
    under `heading`. Returns the ticket, or None when nothing new was added.
    The check and the write happen under one lock."""
    if not ID_RE.fullmatch(ticket_id):
        raise TicketInvalid([f"'{ticket_id}' is not a ticket id (expected like {ID_EXAMPLE})"])
    with _locked():
        ticket = load_ticket(ticket_id)
        if ticket["status"] not in ACTIVE_STATUSES:
            raise TicketError(f"{ticket_id} is {ticket['status']}, so nothing new can be added to it")
        held = {ln.strip() for ln in ticket["body"].splitlines()}
        fresh = [ln for ln in items.splitlines() if ln.strip() and ln.strip() not in held]
        if not fresh:
            return None
        body = f"{ticket['body']}\n\n{heading}\n" + "\n".join(fresh)
        if len(body) > BODY_MAX:
            raise TicketError(f"{ticket_id} is too full to take these items; they are in the notes on the reviewed ticket")
        errors = check_secrets(ticket["title"], body)
        if errors:
            raise TicketInvalid(errors)
        ticket["body"] = body
        ticket["updated_at"] = _now()
        _write_atomic(_ticket_path(ticket_id), ticket)
    return ticket


def verdict_line(v: dict | None) -> str:
    """One line for a stored verdict, for `ticket show` and the card."""
    if v is None:
        return "-"
    who = ", ".join(f"{r['reviewer']}: {r['result']}" for r in v["reviews"].values())
    return f"{v['result']} ({who}) on commit {v['commit']} at {v['at']}"


def record_verdict(branch: str, repo: str, result: str, commit: str, reviewer: str) -> list[dict]:
    """Record a review's result on the open, claimed or in-review tickets of a branch.
    Information only: the merge gate reads the saved review itself. Reviews of
    the same commit by the two reviewers are combined (FAIL wins); a review of
    a different commit replaces the old verdict. Returns the tickets changed."""
    errors = []
    if not BRANCH_RE.fullmatch(branch or ""):
        errors.append("branch must look like task/some-name")
    if not REPO_RE.fullmatch(repo or ""):
        errors.append("repo is not a valid repo name")
    if result not in VERDICT_RESULTS:
        errors.append(f"result must be one of: {', '.join(VERDICT_RESULTS)}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        errors.append("commit must be a full 40-character commit id")
    kind = (reviewer or "").split(" ")[0].lower()
    if not _is_str(reviewer) or not 1 <= len(reviewer) <= REVIEWER_MAX or kind not in REVIEW_KINDS:
        errors.append(f"reviewer must start with codex or claude and be 1 to {REVIEWER_MAX} characters")
    if errors:
        raise TicketInvalid(errors)
    changed = []
    with _locked():
        for t in list_tickets():
            if t["branch"] != branch or t["repo"] != repo or t["status"] not in ("open", "claimed", "review"):
                continue
            old = t["verdict"]
            reviews = dict(old["reviews"]) if old and old["commit"] == commit else {}
            reviews[kind] = {"result": result, "reviewer": reviewer}
            now = _now()
            failed = any(r["result"] == "FAIL" for r in reviews.values())
            t["verdict"] = {"result": "FAIL" if failed else "PASS",
                            "commit": commit, "reviewer": reviewer, "at": now, "reviews": reviews}
            if failed and commit not in t.get("fail_commits", []):
                t["fail_commits"] = (t.get("fail_commits", []) + [commit])[-FAIL_COMMITS_MAX:]
            t["updated_at"] = now
            _write_atomic(_ticket_path(t["id"]), t)
            changed.append(t)
    return changed


def close_ticket(ticket_id: str, outcome: str, note: str | None = None) -> dict:
    """Close an open, claimed or in-review ticket as done, failed or cancelled. Humans only."""
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
                      allowed_from=("open", "claimed", "review"), close_note=note)


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
