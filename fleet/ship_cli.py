"""The `ship` command: merge a ticket's work (in review, or still claimed) with its repo's own
scripts/finish-task.sh, which keeps every check and prompt it has."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import dispatch as dp
from . import launch as lc
from . import prep
from . import ticket as tk


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"ship: {message}", file=sys.stderr)
        raise SystemExit(2)


def _fail(message: str, code: int = 1) -> int:
    print(f"ship: {message}", file=sys.stderr)
    return code


def _reconcile() -> int:
    """Run `dispatch --reconcile` (the sibling bin/dispatch) so the card moves
    to Done at once."""
    dispatch = Path(__file__).resolve().parent.parent / "bin" / "dispatch"
    return subprocess.run([sys.executable, str(dispatch), "--reconcile"]).returncode


def _gate(repo: Path, wt: Path, branch: str, *fields: str) -> subprocess.CompletedProcess:
    """`ticket gate` the way the merge script calls it: the exact same gate."""
    ticket = Path(__file__).resolve().parent.parent / "bin" / "ticket"
    base = dp.main_branch(repo) or "main"
    return subprocess.run(
        [sys.executable, str(ticket), "gate", "--base", base, "--branch", branch, "--dir", str(repo),
         "--worktree", str(wt), "--repo", repo.name, *fields], capture_output=True, text=True)


def _confirm(question: str) -> bool:
    """Ask on the terminal; anything but an explicit yes (or no terminal) is no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


class _GateBroken(Exception):
    pass


def _field(repo: Path, wt: Path, branch: str, name: str) -> str:
    """One gate field. The gate exits 1 when it has review problems, which is
    normal here; a crash (other exit code, or anything on stderr) is not."""
    done = _gate(repo, wt, branch, "--field", name)
    if done.returncode not in (0, 1) or done.stderr.strip():
        detail = done.stderr.strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise _GateBroken(f"ticket gate --field {name} failed ({detail[0]}); nothing was merged")
    return done.stdout.strip()


def _review_if_needed(repo: Path, name: str, branch: str, script: Path) -> str | None:
    """When the gate wants reviews and lacks them, run them now: both reviewers
    without asking at high risk, or (after a yes) the usual one when the tip
    has no review at all. The merge script still applies the gate afterwards.
    Returns a one-line problem, or None. Anything else the gate dislikes is
    left for the merge script to report: this never decides whether a merge
    is allowed."""
    wt = repo / ".worktrees" / name
    review = repo / "scripts" / "review.sh"
    if not wt.is_dir() or not review.is_file():
        return None
    try:
        ok, risk = _field(repo, wt, branch, "ok"), _field(repo, wt, branch, "risk")
        if ok == "yes":
            return None
        has_reviews = risk != "high" and _field(repo, wt, branch, "reviews")
    except _GateBroken as exc:
        return str(exc)
    env = dict(os.environ)
    if risk == "high":
        print(f"ship: {branch} is high risk and lacks passing reviews; running both reviewers")
        env["REVIEWER"] = "both"
    else:
        # Below high risk, only the case where the tip has no review at all
        # (small fixes committed after the review) is offered; a review that
        # exists but is not a PASS is for the human to look at.
        if has_reviews:
            return None
        if not _confirm(f"ship: {branch} has no review of its latest commit. Run scripts/review.sh now?"):
            return None
    env.setdefault("FLEET_TICKET", str(Path(__file__).resolve().parent.parent / "bin" / "ticket"))
    try:
        done = subprocess.run(["bash", str(review)], cwd=wt, env=env)
    except OSError as exc:
        return f"could not run {review}: {exc}"
    if done.returncode != 0:
        return f"the reviews of {branch} did not complete (exit {done.returncode}); nothing was merged"
    return None


def _conflict_agent(repo: Path, t: dict, settings: dict) -> str | None:
    """The merge script stops with the merge half done when the branch conflicts with main.
    If that is what happened, put main back as it was and start a short reconcile agent.
    Returns a one-line message, or None when there was no merge conflict."""
    probe = dp._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if probe.returncode != 0:
        return None
    # The script refuses a main checkout with changes, so aborting restores exactly what was there.
    if dp._git(repo, "merge", "--abort").returncode != 0:
        return (f"{t['branch']} conflicts with main, and the half-done merge in {repo} "
                "could not be aborted: run git merge --abort there")
    try:
        started = prep.start_reconcile(t, settings, lambda m: print(f"ship: {m}", file=sys.stderr))
    except (lc.LaunchError, OSError, subprocess.TimeoutExpired) as exc:
        return (f"{t['branch']} conflicts with main (the merge was aborted); "
                f"no reconcile agent could be started ({exc})")
    return (f"{t['branch']} conflicts with main (the merge was aborted); {started}. "
            "You will get a ping when it is ready: ship it again then")


def _ship_one(ticket_id: str, in_list: bool = False) -> int:
    """Ship one ticket; prints what happened and returns the exit code."""
    try:
        t = tk.load_ticket(ticket_id)
    except tk.TicketInvalid as exc:
        return _fail(f"unknown ticket ID: {exc}")
    except tk.TicketError as exc:
        return _fail(str(exc))
    if t["status"] not in ("claimed", "review"):
        return _fail(f"{t['id']} is {t['status']}; only a ticket in review (or still claimed) can be shipped")
    if in_list and t["status"] != "review":
        return _fail(f"{t['id']} is {t['status']}, not in review any more, so it is no longer ready")
    if t["id"] in prep.locked_ids():
        return _fail(f"an agent is still working on {t['id']}; wait for its ping")
    repo_name, branch = t.get("repo"), t.get("branch")
    if not repo_name or not branch:
        return _fail(f"{t['id']} has no repo and branch to ship")
    if not branch.startswith("task/") or len(branch) == len("task/"):
        return _fail(f"{t['id']} branch {branch} is not a task/<name> branch")
    name = branch.removeprefix("task/")

    try:
        settings = dp.load_settings()
    except dp.SettingsError as exc:
        return _fail(str(exc), 2)
    projects_dir = settings["projects_dir"]
    repo = projects_dir / repo_name
    if repo.resolve().parent != projects_dir.resolve() or not repo.is_dir():
        return _fail(f"repo folder {repo} not found directly inside {projects_dir}")
    script = repo / "scripts" / "finish-task.sh"
    if not script.is_file():
        return _fail(f"{repo} has no scripts/finish-task.sh")

    problem = _review_if_needed(repo, name, branch, script)
    if problem:
        return _fail(problem)

    # Same terminal, same prompts: nothing is answered or skipped on the
    # human's behalf. FLEET_HOME and the rest of the environment carry over.
    try:
        done = subprocess.run(["bash", str(script), name], cwd=repo, env=dict(os.environ))
    except OSError as exc:
        return _fail(f"could not run {script}: {exc}")
    if done.returncode != 0:
        if t["status"] == "review":
            conflict = _conflict_agent(repo, t, settings)
            if conflict:
                return _fail(conflict)
        return _fail(f"finish-task refused {branch} (exit {done.returncode}); nothing was reconciled")

    # finish-task also exits 0 when the human answers no at its prompt, so ask
    # git, not the exit code, whether the merge happened.
    if not dp.is_merged(repo, branch, t.get("base_commit")):
        return _fail(f"{t['id']} was not merged (cancelled?); it is still {t['status']}")
    later = "; run `dispatch --reconcile` to move the card to Done"
    try:
        _reconcile()
        status = tk.load_ticket(t["id"])["status"]
    except (OSError, tk.TicketError) as exc:
        print(f"ship: {t['id']} merged, but the card was not updated ({exc}){later}")
        return 0
    if status == "done":
        print(f"ship: {t['id']} merged and marked done")
    else:
        # e.g. a running `dispatch --watch` holds the lock; it reconciles on its next pass
        print(f"ship: {t['id']} merged, but is still {status}{later}")
    return 0


def _ship_list() -> int:
    """Show the ship list and ship each ticket in order, with the merge script's own
    confirmation for each. Stops at the first failure and says why."""
    try:
        entries = prep.read_list()
    except OSError as exc:
        return _fail(f"cannot read the ship list: {exc}")
    if not entries:
        return _fail(f"there is no ship list ({prep.list_path()}); the prep pass writes it")
    print("Ship list:")
    for _, line in entries:
        print(f"  {line}")
    shipped = 0
    for n, (ticket_id, _) in enumerate(entries, 1):
        try:
            status = tk.load_ticket(ticket_id)["status"]
        except (tk.TicketError, tk.TicketInvalid) as exc:
            return _fail(f"stopped at {ticket_id}: {exc}")
        if status == "done":
            print(f"ship: {ticket_id} is already done; skipping")
            continue
        print(f"\nship: {ticket_id} ({n} of {len(entries)})")
        if _ship_one(ticket_id, in_list=True) != 0:
            return _fail(f"stopped at {ticket_id}; the rest of the list was not shipped")
        shipped += 1
    print(f"ship: the list is done ({shipped} shipped)")
    return 0


def main(argv=None) -> int:
    p = _Parser(prog="ship")
    p.add_argument("id", nargs="?", help="ticket id, like BD-17")
    p.add_argument("--list", action="store_true", help="ship every ticket on the ship list, in order")
    args = p.parse_args(argv)
    if args.list == bool(args.id):
        p.error("give a ticket id, or --list, not both and not neither")
    who = tk.current_creator()
    if who != "human":
        return _fail(f"refusing to run inside an agent (running as {who}); only a human ships")
    return _ship_list() if args.list else _ship_one(args.id)
