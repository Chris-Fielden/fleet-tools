"""The dispatcher's settings, rules and reconcile step (Part A).

Plain Python, no AI. The rules in `decide` are shared with Part B, which
will launch the first eligible ticket that `decide` returns.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import risk as rk
from . import startup_block
from . import ticket as tk
from .settings import (  # noqa: F401  (re-exported: callers use dp.load_settings etc.)
    DEFAULTS, MIN_WATCH_INTERVAL, SettingsError, load_settings, settings_path)
from zoneinfo import ZoneInfo

LAUNCH_TYPES = ("build", "fix")
MAIN_BRANCHES = ("main", "master")
QUEUE_SKIPS = ("the on-call slot is busy", "waiting its turn")   # on-call skips that are just the queue
TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")  # what scripts/new-task.sh accepts


DESIGN_RE = re.compile(r"\bdesign\b", re.I)   # what a findings note must mention
FINDINGS_HEAD = "Findings from investigation {id}:"


class AlreadyRunning(Exception):
    pass


class WatchRestart(Exception):
    """--watch saw new code on main and wants to be re-executed."""


# --- locations, log, lock ----------------------------------------------------

def log_path() -> Path:
    return tk.fleet_home() / "dispatch.log"


def code_head() -> str | None:
    """HEAD of the fleet-tools checkout this code runs from, or None."""
    try:
        out = _git(Path(__file__).resolve().parent.parent, "rev-parse", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def log(kind: str, message: str) -> None:
    """Append one line. Callers pass ids, branches, agent names and reasons
    only; never ticket titles or bodies."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {kind} {message}".replace("\n", " ")
    path = log_path()
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


@contextlib.contextmanager
def single_instance():
    """Hold $FLEET_HOME/dispatch.lock, or raise AlreadyRunning."""
    path = tk.fleet_home() / "dispatch.lock"
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning() from None
        try:
            lock.truncate(0)   # so `fleet stop` knows whom to signal
            lock.write(str(os.getpid()))
            lock.flush()
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Clock:
    """The time source of the watch loop; tests swap in a fake one."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def local_day(now: datetime, tz: str = DEFAULTS["timezone"]) -> str:
    """The calendar day (YYYY-MM-DD) of `now` in timezone `tz`: when the daily caps reset."""
    return now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def launches_path() -> Path:
    return tk.fleet_home() / "dispatch-launches.json"


def launches_today(day: str) -> int:
    """Launches the watch loop has attempted on this local day (YYYY-MM-DD, see local_day). Kept
    in a file so restarting the watcher does not reset the safety cap. A
    missing file is 0; an unreadable one raises OSError (callers fail closed)."""
    try:
        data = json.loads(launches_path().read_text(encoding="utf-8"))
        count, saved = data["count"], data["day"]
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OSError(f"cannot read {launches_path()}: {exc}") from None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise OSError(f"{launches_path()} has a bad count")
    try:
        datetime.strptime(saved, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise OSError(f"{launches_path()} has a bad day") from None
    return count if saved == day else 0


def add_launches(day: str, n: int = 1) -> None:
    path = launches_path()
    tk.refuse_real_store_in_tests(path)
    total = launches_today(day) + n
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"day": day, "count": total}, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- settings: see fleet/settings.py (re-exported here) ----------------------

def research_problem(settings: dict) -> str | None:
    """Why no research agent can be launched right now, or None. Only Claude's
    guard hooks are wired into the research folder, so a Codex builder (whose
    guards live in .codex) is refused rather than run unguarded."""
    if settings["builder"] != "claude":
        return (f"investigations need builder claude (the research folder only gets Claude's "
                f"guard hooks), not {settings['builder']}")
    return research_dir_problem(settings)


def research_dir_problem(settings: dict) -> str | None:
    """Why research agents can't work in research_dir, or None. It must not be
    inside the fleet's private folder (the ticket store and the Kaneo key), nor
    contain it: the guard hooks only watch what an agent does through its own
    tools, and a research agent there would sit next to the secrets."""
    here = settings["research_dir"].resolve()
    ancestor = here
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if _git(ancestor, "rev-parse", "--show-toplevel").returncode == 0:
        return f"research_dir {settings['research_dir']} is inside a git repo; it must not be one"
    homes = {tk.fleet_home().resolve(), (tk._real_home() / ".fleet").resolve()}
    for private in sorted(homes):
        if here == private or here.is_relative_to(private) or private.is_relative_to(here):
            return f"research_dir {settings['research_dir']} overlaps the fleet's private folder {private}"
    return None


# --- git helpers (read-only) -------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-pager", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


def main_branch(repo: Path) -> str | None:
    for name in MAIN_BRANCHES:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return None


def repo_problem(repo: Path, projects_dir: Path) -> str | None:
    """Why this repo can't take new work, or None if it can."""
    if repo.resolve().parent != projects_dir.resolve():
        return f"repo path {repo} is not directly inside {projects_dir}"
    if not repo.is_dir():
        return f"repo folder {repo} does not exist"
    top = _git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo.resolve():
        return f"{repo} is not a git repo"
    if not (repo / "scripts" / "new-task.sh").is_file():
        return f"{repo} has no scripts/new-task.sh"
    head = _git(repo, "symbolic-ref", "--short", "HEAD")
    current = head.stdout.strip() if head.returncode == 0 else None
    if current not in MAIN_BRANCHES:
        return f"{repo} is on {current or 'no branch'}, not main or master"
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        return f"{repo} has uncommitted changes"
    return None


def branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


# --- the rules ---------------------------------------------------------------

@dataclass
class Decision:
    ticket: dict
    skip: str | None = None
    agent: str | None = None
    model: str | None = None
    name: str | None = None  # branch without "task/"
    repo_path: Path | None = None
    research: bool = False  # an investigate ticket: no task folder, no branch
    oncall: bool = False    # an on-call fix ticket: own slot, branches from the failed tip
    base: str | None = None  # oncall: the failed branch tip the new branch starts from
    prompt: str | None = None  # a prep or reconcile agent: its whole starting prompt

    @property
    def eligible(self) -> bool:
        return not self.skip

    def log_skip(self, fallback: str = "not launched: no reason recorded") -> str:
        """Log this decision as a skip with a real reason (never None or empty)."""
        if not self.skip:
            self.skip = fallback
        log("DECISION", f"{self.ticket['id']} skip: {self.skip}")
        return self.skip

    @property
    def worktree(self) -> str:
        return f"{self.ticket['repo']}/.worktrees/{self.name}"

    def line(self) -> str:
        if self.skip:
            return f"{self.ticket['id']}  skip: {self.skip}"
        model = self.model or "default"
        if self.research:
            return f"{self.ticket['id']}  would launch {self.agent} ({model}) to investigate (read-only)"
        if self.oncall:
            return f"{self.ticket['id']}  would launch on-call {self.agent} ({model}) in {self.worktree}"
        return f"{self.ticket['id']}  would launch {self.agent} ({model}) in {self.worktree}"


# --- occupancy: the one place that says what is taken ------------------------

def branch_holders(tickets: list[dict]) -> list[dict]:
    """Tickets that own their branch and task folder: claimed or in review, on-call
    ones included. Every "is this branch free" check uses this."""
    return [t for t in tickets if t["status"] in ("claimed", "review") and t["type"] != "investigate"]


def capacity_holders(tickets: list[dict]) -> list[dict]:
    """Tickets that fill the normal `max_running` capacity: the claimed ones. A ticket
    in review is waiting for the human, not for an agent, so it keeps its branch but
    not its place; on-call ones have their own slot."""
    return [t for t in branch_holders(tickets) if not t.get("oncall") and t["status"] == "claimed"]


def oncall_slot_busy(tickets: list[dict], running: frozenset | set | None) -> str | None:
    """Why the on-call slot is taken, or None if it is free. Busy while any on-call
    ticket is claimed or in review, or while the herdr agent of any on-call ticket is
    still running, whatever the ticket's status. `running` is the set of lowercase
    ids of agents that have not stopped; None means herdr could not be asked, and
    then the slot is busy (fail closed)."""
    if running is None:
        return "herdr could not be asked which agents are running"
    for t in tickets:
        if t.get("oncall"):
            if t["status"] in ("claimed", "review"):
                return f"{t['id']} is {t['status']}"
            if t["id"].lower() in running:
                return f"the agent of {t['id']} is still running"
    return None


# --- investigate first: security-sensitive build and fix tickets ---------------

def investigation_id(t: dict) -> str | None:
    """The investigation a build or fix ticket waits for: a build's parent, a fix's own field
    (its parent is the ticket it fixes)."""
    return t.get("parent") if t["type"] == "build" else t.get("investigation")


def has_design(inv: dict) -> bool:
    return any(DESIGN_RE.search(n["text"]) for n in inv.get("notes", []))


def needs_investigation(t: dict, rules: dict) -> bool:
    return t["type"] in LAUNCH_TYPES and not t.get("oncall") and bool(rk.high_paths(t["body"], rules))


def investigation_gate(t: dict, by_id: dict, rules: dict) -> tuple[str, str]:
    """Is this ticket free to launch? ('ok' | 'need' | 'wait' | 'blocked', why). A build or
    fix ticket whose body names high-risk paths (risk.high_paths) needs a done
    investigate ticket whose notes hold a design: 'need' means there is none yet (the
    dispatcher files one), 'wait' that it is still being worked, 'blocked' that it can't
    finish by itself (failed, cancelled, or done without a design)."""
    if not needs_investigation(t, rules):
        return "ok", ""
    inv_id = investigation_id(t)
    if inv_id is None:
        return "need", "touches security-sensitive paths; an investigation comes first"
    inv = by_id.get(inv_id)
    if inv is None:
        return "blocked", f"investigation {inv_id} not found"
    if inv["type"] != "investigate":
        return "blocked", f"{inv_id} is not an investigate ticket"
    if inv["status"] == "done":
        if has_design(inv):
            return "ok", ""
        return "blocked", f"investigation {inv['id']} is done but has no findings note with a design"
    if inv["status"] in ("failed", "cancelled"):
        return "blocked", f"investigation {inv['id']} is {inv['status']}; a human must decide"
    return "wait", f"waiting for investigation {inv['id']} ({inv['status']})"


def _investigation_body(t: dict) -> str:
    head = (f"Investigate {t['id']} before it is built. Do not build anything. Record, with "
            "`ticket note`, findings that contain a DESIGN (the approach, in steps) and the "
            "FAILURE MODES a test must cover. The ticket's full spec follows.\n\n"
            f"Spec of {t['id']} ({t['type']}, repo {t['repo']}):\n")
    room = tk.BODY_MAX - len(head)
    spec = t["body"] if len(t["body"]) <= room else t["body"][:room - 40] + f"\n[cut; see {t['id']}]"
    return head + spec


def findings_note(inv: dict) -> str:
    head = FINDINGS_HEAD.format(id=inv["id"])
    notes = sorted(inv.get("notes", []), key=lambda n: not DESIGN_RE.search(n["text"]))  # the design survives a cut
    text = head + "\n" + "\n\n".join(n["text"] for n in notes)
    return text if len(text) <= tk.NOTE_MAX else text[:tk.NOTE_MAX - 12] + "\n[cut]"


def ensure_investigations(tickets: list[dict], warn=lambda m: None) -> tuple[list[str], set[str]]:
    """Before launching: file the missing investigation of each open build or fix ticket
    that needs one (no human approval; it starts open), and append the findings of a done
    one to the ticket as a note, once. Returns (one line per change, ids of tickets to
    hold back this run because that failed: fail closed)."""
    try:
        rules = rk.load_rules()
    except rk.RiskError as exc:
        warn(f"investigate-first: {exc}")
        return [], set()
    by_id = {t["id"]: t for t in tickets}
    lines, held = [], set()
    for t in sorted((t for t in tickets if t["status"] == "open" and t["type"] in LAUNCH_TYPES
                     and not t.get("oncall")), key=tk.oldest_first_key):
        state, _ = investigation_gate(t, by_id, rules)
        branch = f"task/investigate-{t['id'].lower()}"
        try:
            if state == "need":
                # One filed earlier whose attaching failed is reused, not filed again.
                inv = next((i for i in tickets if i["type"] == "investigate" and i["branch"] == branch
                            and i["status"] not in ("failed", "cancelled")), None)
                if inv is None:
                    inv = tk.create_ticket({
                        "type": "investigate", "repo": t["repo"], "branch": branch,
                        "title": f"Investigate before building: {t['title']}"[:100],
                        "body": _investigation_body(t)}, open_now=True)
                    log("INVESTIGATION_FILED", f"{inv['id']} for {t['id']}")
                    lines.append(f"{t['id']}  filed investigation {inv['id']}; it launches after that is done")
                tk.attach_investigation(t["id"], inv["id"])
            elif state == "ok" and needs_investigation(t, rules):
                inv = by_id[investigation_id(t)]
                head = FINDINGS_HEAD.format(id=inv["id"])
                if not any(n["text"].startswith(head) for n in t.get("notes", [])):
                    tk.add_note(t["id"], findings_note(inv))
                    log("FINDINGS_APPENDED", f"{inv['id']} -> {t['id']}")
                    lines.append(f"{t['id']}  findings of {inv['id']} added as a note")
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"investigate-first: {t['id']}: {exc}")
            held.add(t["id"])
    return lines, held


def _judge(t: dict, tickets: dict, claimed: list[dict], settings: dict, holders: list[dict],
           rules: dict | None = None) -> Decision:
    d = Decision(ticket=t, name=t["branch"].removeprefix("task/"))

    # 1. Type
    if t["type"] not in LAUNCH_TYPES:
        d.skip = "handled in phase 4"
        return d
    if t["id"] in startup_block.blocked_ids():
        d.skip = BLOCKED_SKIP
        return d
    # 1b. Investigate first (security-sensitive paths); no rules to judge by: fail closed
    if rules is None:
        d.skip = "the risk rules could not be read, so investigate-first can't be checked"
        return d
    state, why = investigation_gate(t, tickets, rules)
    if state != "ok":
        d.skip = why if state != "need" else why + " (the dispatcher files one)"
        return d
    # 2. Capacity
    if len(claimed) >= settings["max_running"]:
        d.skip = "at capacity"
        return d
    # 3. Repo
    projects_dir = settings["projects_dir"]
    repo = projects_dir / t["repo"]
    d.repo_path = repo
    problem = repo_problem(repo, projects_dir)
    if problem:
        d.skip = problem
        return d
    # 4. Branch
    if not TASK_NAME_RE.fullmatch(d.name):
        d.skip = (f"branch {t['branch']} can't be used by scripts/new-task.sh (the name after "
                  "task/ must be 2 to 41 lowercase letters, numbers or dashes, "
                  "starting with a letter or number)")
        return d
    for other in holders:
        if other["branch"] == t["branch"]:
            d.skip = f"branch {t['branch']} is in use by claimed ticket {other['id']}"
            return d
    if t["type"] != "fix" and branch_exists(repo, t["branch"]):
        d.skip = f"branch {t['branch']} already exists in {t['repo']}"
        return d
    # 5. Who works it
    if t["type"] == "build":
        agent = settings["builder"]
    else:
        parent = tickets.get(t["parent"])
        if parent is None:
            d.skip = f"parent ticket {t['parent']} not found"
            return d
        if parent["assignee"] is None:
            d.skip = f"parent ticket {parent['id']} has no assignee"
            return d
        agent = parent["assignee"]
    d.agent = agent
    d.model = settings["models"].get(agent)
    return d


def decide(tickets: list[dict], settings: dict) -> list[Decision]:
    """One Decision per open ticket: urgent ones first, then normal ones,
    oldest first within each (whoever filed them). Only the first eligible
    one stays eligible (rule 6); later ones are marked as waiting."""
    by_id = {t["id"]: t for t in tickets}
    # Investigations are read-only: they neither fill the capacity nor hold a
    # branch. They have their own rules (decide_investigations).
    # A ticket in review still holds its branch, but no longer a place in the capacity.
    # On-call fix tickets have their own slot (decide_oncall): they neither fill
    # the normal capacity nor wait in the normal queue, but they still hold their branch.
    holders, claimed = branch_holders(tickets), capacity_holders(tickets)
    try:
        rules = rk.load_rules()
    except rk.RiskError:
        rules = None
    decisions = []
    picked = False
    open_tickets = sorted((t for t in tickets if t["status"] == "open" and t["type"] != "investigate"
                           and not t.get("oncall")),
                          key=lambda t: (t.get("priority") != "urgent", *tk.oldest_first_key(t)))
    for t in open_tickets:
        d = _judge(t, by_id, claimed, settings, holders, rules)
        if d.eligible:
            if picked:
                d.skip = "only one ticket is started per run"
            picked = True
        decisions.append(d)
    return decisions


def started_today(tickets: list[dict], now: datetime | None = None) -> int:
    """How many investigations were started today (UTC)."""
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return sum(1 for t in tickets
               if t["type"] == "investigate" and t.get("started_at", "").startswith(today))


BLOCKED_SKIP = "not launched: startup blocked, waiting for a human"


def decide_investigations(tickets: list[dict], settings: dict, now: datetime | None = None) -> list[Decision]:
    """One Decision per waiting investigate ticket (proposed, or open once a
    human approved it): human-filed first, then oldest first. Only the first
    eligible one stays eligible, so one investigation starts per run.

    A proposed ticket needs `auto_investigate`; an approved one does not.
    Every start counts against `max_investigations_per_day`."""
    agent = settings["builder"]
    used = started_today(tickets, now)
    folder_problem = research_problem(settings)
    waiting = sorted((t for t in tickets if t["type"] == "investigate" and t["status"] in ("proposed", "open")),
                     key=lambda t: (t["created_by"] != "human", *tk.oldest_first_key(t)))
    decisions = []
    picked = False
    stuck = startup_block.research_hold()   # one stuck start holds every research launch
    for t in waiting:
        d = Decision(ticket=t, name=t["id"].lower(), agent=agent,
                     model=settings["models"].get(agent), research=True)
        if stuck:
            d.skip = f"{BLOCKED_SKIP} ({stuck}; fleet unblock {stuck})"
        elif folder_problem:
            d.skip = folder_problem
        elif t["status"] == "proposed" and not settings["auto_investigate"]:
            d.skip = "auto_investigate is off; approve it to investigate"
        elif used >= settings["max_investigations_per_day"]:
            d.skip = f"daily limit reached ({used} of {settings['max_investigations_per_day']} investigations started today)"
        elif picked:
            d.skip = "only one investigation is started per run"
        else:
            picked = True
        decisions.append(d)
    return decisions


def oncall_today(tickets: list[dict], now: datetime | None = None, tz: str = DEFAULTS["timezone"]) -> int:
    """On-call attempts made today (local day in `tz`): an on-call ticket counts from the
    moment it is filed, so a queued one is already spent. Counted from the
    stored tickets, so restarting the watcher cannot reset it."""
    today = local_day(now or datetime.now(timezone.utc), tz)
    n = 0
    for t in tickets:
        if not t.get("oncall"):
            continue
        try:
            filed = datetime.strptime(t["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        n += local_day(filed, tz) == today
    return n


def decide_oncall(tickets: list[dict], settings: dict, running: frozenset | set | None) -> list[Decision]:
    """One Decision per open on-call ticket, first in, first out. The on-call slot is
    separate from the normal capacity and holds one agent at a time; whether it is
    taken is decided only by oncall_slot_busy (`running`: see there; None = herdr
    could not be asked, which counts as busy). Only the first eligible one stays
    eligible."""
    by_id = {t["id"]: t for t in tickets}
    busy = oncall_slot_busy(tickets, running)
    holders = branch_holders(tickets)
    waiting = sorted((t for t in tickets if t.get("oncall") and t["status"] == "open"),
                     key=tk.oldest_first_key)
    projects_dir = settings["projects_dir"]
    decisions, picked = [], False
    for t in waiting:
        repo = projects_dir / t["repo"]
        d = Decision(ticket=t, name=t["branch"].removeprefix("task/"), repo_path=repo, oncall=True,
                     agent=settings["builder"], model=settings["models"].get(settings["builder"]))
        parent = by_id.get(t["parent"])
        if repo.resolve().parent != projects_dir.resolve() or not repo.is_dir():
            d.skip = f"repo folder {repo} not found"
        elif _git(repo, "rev-parse", "--show-toplevel").returncode != 0:
            d.skip = f"{repo} is not a git repo"
        elif not TASK_NAME_RE.fullmatch(d.name):
            d.skip = f"branch {t['branch']} can't be used as a task folder name"
        elif branch_exists(repo, t["branch"]):
            d.skip = f"branch {t['branch']} already exists in {t['repo']}"
        elif parent is None:
            d.skip = f"failed ticket {t['parent']} not found"
        elif any(h["branch"] == t["branch"] and h["id"] != t["id"] for h in holders):
            d.skip = f"branch {t['branch']} is held by another ticket"
        else:
            # The commit the failed ticket stopped at, saved when it failed; the
            # parent's branch may have moved on while this waited for the slot.
            base = t.get("oncall_base")
            if base is None:
                tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{parent['branch']}^{{commit}}")
                base = tip.stdout.strip() if tip.returncode == 0 else None
            if base is None or _git(repo, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
                d.skip = f"commit to start from not found in {t['repo']} (failed branch {parent['branch']})"
            else:
                d.base = base
        if d.skip is None:
            if busy:
                d.skip = f"{QUEUE_SKIPS[0]}: {busy}"
            elif picked:
                d.skip = QUEUE_SKIPS[1] + " behind an earlier on-call ticket"
            else:
                picked = True
        decisions.append(d)
    return decisions


# --- reconcile ---------------------------------------------------------------

def merge_commit(repo: Path, branch: str, since: str | None = None) -> str | None:
    """The newest merge commit in main whose message is exactly `Merge <branch>`, or None.
    With `since` (a commit id), only merges made after that commit count, so an
    old merge of a reused branch name doesn't match."""
    main = main_branch(repo)
    if main is None:
        return None
    out = _git(repo, "log", f"{since}..{main}" if since else main, "--merges", "--format=%H%x01%B%x00")
    if out.returncode != 0:
        return None
    # Whole messages, NUL-separated: a subject match with extra text doesn't count.
    # Strip only the newlines git adds around each message, not spaces.
    # The merge script adds an "EMERGENCY OVERRIDE (risk ...)" paragraph when a
    # human merged without the required review; that is still this branch.
    plain = f"Merge {branch}"
    override = f"Merge {branch}\n\nEMERGENCY OVERRIDE (risk "
    for entry in out.stdout.split("\0"):
        sha, _, message = entry.strip("\n").partition("\x01")
        message = message.strip("\n")
        if sha and (message == plain or message.startswith(override)):
            return sha
    return None


def is_merged(repo: Path, branch: str, since: str | None = None) -> bool:
    """True if main has a merge commit whose message is exactly `Merge <branch>`."""
    return merge_commit(repo, branch, since) is not None


def delivered_by_partner(repo: Path, t: dict, tickets: list[dict]) -> dict | None:
    """A stacked ticket whose commits reached main through another ticket's merge: the partner
    ticket, or None. Never decided on "no new commits" alone. All of these must hold: the branch
    has commits of its own past the ticket's base commit; its tip is already in main; and some
    other ticket of the repo (not itself) has its own `Merge <branch>` in main whose merged side
    contains this branch's tip and whose main side did not (the merge is what brought it in: it
    was stacked on this one). The partner's branch itself may be gone: finish-task deletes it
    after the merge."""
    main, base = main_branch(repo), t.get("base_commit")
    if main is None or not base or not t.get("branch"):
        return None

    def tip(branch: str) -> str:
        out = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        return out.stdout.strip() if out.returncode == 0 else ""

    mine = tip(t["branch"])
    if not mine or mine == base:
        return None
    if _git(repo, "merge-base", "--is-ancestor", base, mine).returncode != 0:
        return None
    if _git(repo, "merge-base", "--is-ancestor", mine, main).returncode != 0:
        return None   # its commits are not all in main yet
    for other in sorted(tickets, key=tk.oldest_first_key):
        if other["id"] == t["id"] or other.get("repo") != t.get("repo") or not other.get("branch") \
                or other["branch"] == t["branch"]:
            continue
        merge = merge_commit(repo, other["branch"], other.get("base_commit"))
        if not merge:
            continue
        # The partner's merge must be what brought the tip into main: on its merged side and not
        # already on the main side (a branch that merely caught up with main has commits of no one's).
        on_merged_side = _git(repo, "merge-base", "--is-ancestor", mine, f"{merge}^2").returncode == 0
        already_in_main = _git(repo, "merge-base", "--is-ancestor", mine, f"{merge}^1").returncode == 0
        if on_merged_side and not already_in_main:
            return other
    return None


def reconcile(tickets: list[dict], settings: dict, warn=lambda m: None):
    """Set claimed (or in-review) tickets whose branch was merged to done; a stacked ticket whose
    commits arrived through its partner's merge is closed as delivered. Yields the changed
    tickets."""
    for t in sorted((t for t in tickets if t["status"] in ("claimed", "review") and t["type"] != "investigate"),
                    key=tk.oldest_first_key):
        repo = settings["projects_dir"] / t["repo"]
        if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
            log("RECONCILE_SKIP", f"{t['id']} repo folder {repo} not found")
            continue
        note = None
        if not is_merged(repo, t["branch"], t.get("base_commit")):
            partner = delivered_by_partner(repo, t, tickets)
            if partner is None:
                log("RECONCILE_SKIP", f"{t['id']} {t['branch']} not merged")
                continue
            note = f"delivered: its commits are in {main_branch(repo) or 'main'} through {partner['id']} ({partner['branch']})"
        try:
            new = tk.set_status(t["id"], "done", allowed_from=("claimed", "review"), close_note=note)
        except tk.TicketError as exc:
            log("RECONCILE_SKIP", f"{t['id']} {exc}")
            continue
        log("RECONCILE_DONE", f"{t['id']} {t['branch']} merged; ticket done")
        yield new
