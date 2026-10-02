"""The dispatcher's settings, rules and reconcile step (Part A).

Plain Python, no AI. The rules in `decide` are shared with Part B, which
will launch the first eligible ticket that `decide` returns.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ticket as tk

DEFAULTS = {
    "builder": "claude",
    "models": {"claude": "sonnet", "codex": None},
    "max_running": 1,
    "projects_dir": "~/projects",
    "herdr_workspace": "fleet",
    "auto_investigate": True,
    "max_investigations_per_day": 3,
}
LAUNCH_TYPES = ("build", "fix")
MAIN_BRANCHES = ("main", "master")
TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")  # what scripts/new-task.sh accepts


class SettingsError(Exception):
    pass


class AlreadyRunning(Exception):
    pass


# --- locations, log, lock ----------------------------------------------------

def settings_path() -> Path:
    return tk.fleet_home() / "dispatch.json"


def log_path() -> Path:
    return tk.fleet_home() / "dispatch.log"


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


# --- settings ----------------------------------------------------------------

def load_settings() -> dict:
    """Defaults, overridden by $FLEET_HOME/dispatch.json if present."""
    settings = copy.deepcopy(DEFAULTS)
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = None
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from None
    if raw is not None:
        try:
            given = json.loads(raw)
        except ValueError as exc:
            raise SettingsError(f"{path} is not valid JSON: {exc}") from None
        _merge(settings, given, path)
    try:
        settings["projects_dir"] = Path(settings["projects_dir"]).expanduser().absolute()
    except RuntimeError:
        raise SettingsError(f"projects_dir '{settings['projects_dir']}' starts with ~ "
                            "but that home folder can't be found") from None
    return settings


def _merge(settings: dict, given, path: Path) -> None:
    if not isinstance(given, dict):
        raise SettingsError(f"{path} must contain a JSON object")
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise SettingsError(f"{path}: unknown setting(s): {', '.join(unknown)}")
    if "builder" in given:
        if given["builder"] not in tk.AGENTS:
            raise SettingsError(f"{path}: builder must be one of: {', '.join(tk.AGENTS)}")
        settings["builder"] = given["builder"]
    if "models" in given:
        models = given["models"]
        if not isinstance(models, dict):
            raise SettingsError(f"{path}: models must be an object like {{\"claude\": \"sonnet\"}}")
        for agent, model in models.items():
            if agent not in tk.AGENTS:
                raise SettingsError(f"{path}: models has unknown agent '{agent}'")
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise SettingsError(f"{path}: the model for {agent} must be a name or null")
            settings["models"][agent] = model
    if "max_running" in given:
        n = given["max_running"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise SettingsError(f"{path}: max_running must be a whole number, 1 or more")
        settings["max_running"] = n
    if "auto_investigate" in given:
        if not isinstance(given["auto_investigate"], bool):
            raise SettingsError(f"{path}: auto_investigate must be true or false")
        settings["auto_investigate"] = given["auto_investigate"]
    if "max_investigations_per_day" in given:
        n = given["max_investigations_per_day"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise SettingsError(f"{path}: max_investigations_per_day must be a whole number, 0 or more")
        settings["max_investigations_per_day"] = n
    for key in ("projects_dir", "herdr_workspace"):
        if key in given:
            if not isinstance(given[key], str) or not given[key].strip():
                raise SettingsError(f"{path}: {key} must be a non-empty string")
            settings[key] = given[key]


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
    claimed = [t for t in tickets if t["status"] == "claimed" and t["type"] != "investigate"]
    decisions = []
    picked = False
    open_tickets = sorted((t for t in tickets if t["status"] == "open" and t["type"] != "investigate"),
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
    waiting = sorted((t for t in tickets if t["type"] == "investigate" and t["status"] in ("proposed", "open")),
                     key=lambda t: (t["created_by"] != "human", *tk.oldest_first_key(t)))
    decisions = []
    picked = False
    for t in waiting:
        d = Decision(ticket=t, name=t["id"].lower(), agent=agent,
                     model=settings["models"].get(agent), research=True)
        if t["status"] == "proposed" and not settings["auto_investigate"]:
            d.skip = "auto_investigate is off; approve it to investigate"
        elif used >= settings["max_investigations_per_day"]:
            d.skip = f"daily limit reached ({used} of {settings['max_investigations_per_day']} investigations started today)"
        elif picked:
            d.skip = "only one investigation is started per run"
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
    """Set claimed tickets whose branch was merged to done. Yields the changed
    tickets."""
    for t in sorted((t for t in tickets if t["status"] == "claimed" and t["type"] != "investigate"),
                    key=tk.oldest_first_key):
        repo = settings["projects_dir"] / t["repo"]
        if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
            log("RECONCILE_SKIP", f"{t['id']} repo folder {repo} not found")
            continue
        if not is_merged(repo, t["branch"], t.get("base_commit")):
            log("RECONCILE_SKIP", f"{t['id']} {t['branch']} not merged")
            continue
        try:
            new = tk.set_status(t["id"], "done", allowed_from=("claimed",))
        except tk.TicketError as exc:
            log("RECONCILE_SKIP", f"{t['id']} {exc}")
            continue
        log("RECONCILE_DONE", f"{t['id']} {t['branch']} merged; ticket done")
        yield new
