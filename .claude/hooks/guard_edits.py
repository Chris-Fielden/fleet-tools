#!/usr/bin/env python3
"""PreToolUse hook for file edits, shared by Claude Code and Codex.

Claude Code sends Edit/Write/NotebookEdit calls with a file_path. Codex sends
apply_patch calls where the file names are inside the patch text. Either
way, this blocks edits in the main checkout (edits belong in a task
worktree), edits to .env files, and content that looks like a real secret.
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RULES_OFF, allow_silently, deny, read_input, repo_info  # noqa: E402
from _common import fleet_home_dir, is_under_fleet  # noqa: E402

# Patterns that look like real credentials. Kept short on purpose: better to
# catch the obvious cases reliably than to flag every long string.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "an Anthropic API key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "an API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
]

CHANGELOG_MSG = ("CHANGELOG.md is written only at ship, from the ticket title, by the "
                 "release tagger. Don't edit it on a task branch: it conflicts with "
                 "every other branch's release entry. Put the wording in the ticket title.")

ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]+)?$")

# Codex apply_patch headers, e.g. "*** Update File: src/app.py"
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def parse_patch(patch, cwd):
    """Return (absolute paths touched, text being added) from an apply_patch body."""
    paths = []
    for m in PATCH_FILE.finditer(patch):
        p = (m.group(1) or m.group(2)).strip()
        paths.append(p if os.path.isabs(p) else os.path.join(cwd, p))
    added = "\n".join(line[1:] for line in patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    return paths, added


def worktree_ticket_problem(info):
    """Why edits in this task worktree are refused, or None. Only when the human turned on
    `require_ticket_for_edits` in settings.json, and only for a repo the ticket store knows
    (it holds a ticket for it): the worktree's branch must belong to a ticket that is claimed
    or in review. A chat that never filed one (`ticket claim-area`) is refused. Hooks see paths
    only, so this cannot tell who is editing; it only checks the branch. Whatever cannot be read
    is not a reason to refuse (the switch is the human's, and a broken store must not freeze all work)."""
    home = fleet_home_dir()
    try:
        with open(os.path.join(home, "settings.json"), encoding="utf-8") as fh:
            if json.load(fh).get("require_ticket_for_edits") is not True:
                return None
    except (OSError, ValueError, AttributeError):
        return None
    repo = os.path.basename(info["main_root"])
    known = False
    for path in glob.glob(os.path.join(home, "tickets", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                t = json.load(fh)
            if t.get("repo") != repo:
                continue
            known = True
            if t.get("branch") == info["branch"] and t.get("status") in ("claimed", "review"):
                return None
        except (OSError, ValueError, AttributeError):
            continue
    if not known:
        return None
    return (f"No active ticket owns branch {info['branch'] or '(detached)'} in {repo}. Parallel work "
            "goes through tickets: run `ticket claim-area --repo ... --branch task/<name> --title ... "
            "--body ... --area <path glob>` and wait until the dispatcher claims it.")


def main():
    if RULES_OFF:
        allow_silently()

    event = read_input()
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    patch = tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or ""
    if isinstance(patch, str) and "*** Begin Patch" in patch:
        paths, new_text = parse_patch(patch, cwd)       # Codex
    else:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        paths = [path] if path else []                   # Claude Code
        new_text = tool_input.get("content") or tool_input.get("new_string") or ""

    if not paths:
        allow_silently()

    for pattern, label in SECRET_PATTERNS:
        if pattern.search(new_text):
            deny(f"That content looks like {label}. Never write real secrets into "
                 "files. Reference the secret by name (for example an environment "
                 "variable or a Kubernetes Secret) and let the human supply the value.")

    for raw in paths:
        if is_under_fleet(raw, cwd):
            deny("Don't edit files in the fleet's private folder (~/.fleet): it holds "
                 "the real tickets and the Kaneo key. Use the ticket command instead.")
        path = raw.replace("\\", "/")
        if ENV_FILE.search(path) and not path.endswith(".env.example"):
            deny("Don't create or edit .env files. Secrets stay out of the repo. "
                 "You may edit .env.example with placeholder values.")
        full = path if os.path.isabs(path) else os.path.join(cwd, path)
        cl_info = repo_info(full)
        # Only the root file: docs/CHANGELOG.md and the like are not guarded.
        if (cl_info is not None and cl_info["is_worktree"]
                and os.path.realpath(full) == os.path.join(os.path.realpath(cl_info["root"]), "CHANGELOG.md")):
            deny(CHANGELOG_MSG)
        info = repo_info(path)
        if info is not None and not info["is_worktree"]:
            deny(f"{path} is in the main checkout, which belongs to the human. Edits "
                 "happen in a task worktree. Ask the human to run "
                 "`scripts/new-task.sh <task-name>` and start you in "
                 "`.worktrees/<task-name>`.")
        if info is not None and info["is_worktree"]:
            problem = worktree_ticket_problem(info)
            if problem:
                deny(problem)

    allow_silently()


if __name__ == "__main__":
    main()
