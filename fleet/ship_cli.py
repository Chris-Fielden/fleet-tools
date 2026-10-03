"""The `ship` command: merge a claimed ticket's work with its repo's own
scripts/finish-task.sh, which keeps every check and prompt it has."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import dispatch as dp
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


def main(argv=None) -> int:
    p = _Parser(prog="ship")
    p.add_argument("id", help="ticket id, like BD-17")
    args = p.parse_args(argv)

    who = tk.current_creator()
    if who != "human":
        return _fail(f"refusing to run inside an agent (running as {who}); only a human ships")
    try:
        t = tk.load_ticket(args.id)
    except tk.TicketInvalid as exc:
        return _fail(f"unknown ticket ID: {exc}")
    except tk.TicketError as exc:
        return _fail(str(exc))
    if t["status"] != "claimed":
        return _fail(f"{t['id']} is {t['status']}, not claimed; only a claimed ticket can be shipped")
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

    # Same terminal, same prompts: nothing is answered or skipped on the
    # human's behalf. FLEET_HOME and the rest of the environment carry over.
    try:
        done = subprocess.run(["bash", str(script), name], cwd=repo, env=dict(os.environ))
    except OSError as exc:
        return _fail(f"could not run {script}: {exc}")
    if done.returncode != 0:
        return _fail(f"finish-task refused {branch} (exit {done.returncode}); nothing was reconciled")

    if _reconcile() != 0:
        return _fail(f"{branch} finished, but dispatch --reconcile failed; run it yourself")
    after = tk.load_ticket(t["id"])
    if after["status"] == "done":
        print(f"ship: {t['id']} merged and marked done")
        return 0
    # finish-task also exits 0 when the human answers no at its prompt.
    return _fail(f"{t['id']} was not merged (cancelled?); it is still {after['status']}")
