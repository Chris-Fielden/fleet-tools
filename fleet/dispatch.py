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

from . import ticket as tk
from .settings import (  # noqa: F401  (re-exported: callers use dp.load_settings etc.)
    DEFAULTS, MIN_WATCH_INTERVAL, SettingsError, load_settings, settings_path)

LAUNCH_TYPES = ("build", "fix")
MAIN_BRANCHES = ("main", "master")
TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")  # what scripts/new-task.sh accepts


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
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Clock:
    """The time source of the watch loop; tests swap in a fake one."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def launches_path() -> Path:
    return tk.fleet_home() / "dispatch-launches.json"


def launches_today(day: str) -> int:
    """Launches the watch loop has attempted on this UTC day (YYYY-MM-DD). Kept
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
        ["git", "--no-optional-locks", "-C", str(repo), *args],
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

    @property
    def eligible(self) -> bool:
        return self.skip is None

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


def _judge(t: dict, tickets: dict, claimed: list[dict], settings: dict) -> Decision:
    d = Decision(ticket=t, name=t["branch"].removeprefix("task/"))

    # 1. Type
    if t["type"] not in LAUNCH_TYPES:
        d.skip = "handled in phase 4"
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
    for other in claimed:
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
    # A ticket in review still holds its branch and its place in the capacity.
    # On-call fix tickets have their own slot (decide_oncall): they neither fill
    # the normal capacity nor wait in the normal queue.
    claimed = [t for t in tickets if t["status"] in ("claimed", "review") and t["type"] != "investigate"
               and not t.get("oncall")]
    decisions = []
    picked = False
    open_tickets = sorted((t for t in tickets if t["status"] == "open" and t["type"] != "investigate"
                           and not t.get("oncall")),
                          key=lambda t: (t.get("priority") != "urgent", *tk.oldest_first_key(t)))
    for t in open_tickets:
        d = _judge(t, by_id, claimed, settings)
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
    for t in waiting:
        d = Decision(ticket=t, name=t["id"].lower(), agent=agent,
                     model=settings["models"].get(agent), research=True)
        if folder_problem:
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


def oncall_today(tickets: list[dict], now: datetime | None = None) -> int:
    """On-call attempts made today (UTC): an on-call ticket counts from the
    moment it is filed, so a queued one is already spent. Counted from the
    stored tickets, so restarting the watcher cannot reset it."""
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return sum(1 for t in tickets if t.get("oncall") and t["created_at"].startswith(today))


def decide_oncall(tickets: list[dict], settings: dict, running: frozenset | set = frozenset()) -> list[Decision]:
    """One Decision per open on-call ticket, first in, first out. The on-call
    slot is separate from the normal capacity and holds one agent at a time: it
    is busy while an on-call ticket is claimed (one waiting in review has an
    agent that has stopped), and also while the agent of an on-call ticket that
    has since failed is still running in herdr (`running`: the lowercase ids
    of agents that have not stopped). Only the first eligible one stays eligible."""
    by_id = {t["id"]: t for t in tickets}
    busy = any(t.get("oncall") and (t["status"] == "claimed"
                                    or (t["status"] == "failed" and t["id"].lower() in running))
               for t in tickets)
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
        else:
            tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{parent['branch']}^{{commit}}")
            if tip.returncode != 0 or not tip.stdout.strip():
                d.skip = f"failed branch {parent['branch']} not found in {t['repo']}"
            else:
                d.base = tip.stdout.strip()
        if d.skip is None:
            if busy:
                d.skip = "the on-call agent is busy"
            elif picked:
                d.skip = "waiting its turn behind an earlier on-call ticket"
            else:
                picked = True
        decisions.append(d)
    return decisions


# --- reconcile ---------------------------------------------------------------

def is_merged(repo: Path, branch: str, since: str | None = None) -> bool:
    """True if main has a merge commit whose message is exactly `Merge <branch>`.
    With `since` (a commit id), only merges made after that commit count, so an
    old merge of a reused branch name doesn't match."""
    main = main_branch(repo)
    if main is None:
        return False
    out = _git(repo, "log", f"{since}..{main}" if since else main, "--merges", "--format=%B%x00")
    if out.returncode != 0:
        return False
    # Whole messages, NUL-separated: a subject match with extra text doesn't count.
    # Strip only the newlines git adds around each message, not spaces.
    # The merge script adds an "EMERGENCY OVERRIDE (risk ...)" paragraph when a
    # human merged without the required review; that is still this branch.
    plain = f"Merge {branch}"
    override = f"Merge {branch}\n\nEMERGENCY OVERRIDE (risk "
    return any(m.strip("\n") == plain or m.strip("\n").startswith(override)
               for m in out.stdout.split("\0"))


def reconcile(tickets: list[dict], settings: dict, warn=lambda m: None):
    """Set claimed (or in-review) tickets whose branch was merged to done. Yields the changed
    tickets."""
    for t in sorted((t for t in tickets if t["status"] in ("claimed", "review") and t["type"] != "investigate"),
                    key=tk.oldest_first_key):
        repo = settings["projects_dir"] / t["repo"]
        if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
            log("RECONCILE_SKIP", f"{t['id']} repo folder {repo} not found")
            continue
        if not is_merged(repo, t["branch"], t.get("base_commit")):
            log("RECONCILE_SKIP", f"{t['id']} {t['branch']} not merged")
            continue
        try:
            new = tk.set_status(t["id"], "done", allowed_from=("claimed", "review"))
        except tk.TicketError as exc:
            log("RECONCILE_SKIP", f"{t['id']} {exc}")
            continue
        log("RECONCILE_DONE", f"{t['id']} {t['branch']} merged; ticket done")
        yield new
