"""Shared helpers for the agent-rules hooks.

Every hook receives a JSON object from Claude Code on standard input
(stdin). These helpers read it and answer simple git questions, such as
"is this folder a task worktree or the main checkout?".
"""

import json
import os
import subprocess
import sys

# Set AGENT_RULES_OFF=1 in YOUR shell before starting claude to turn the
# rules off for that session (an escape hatch for the human, not the agent).
RULES_OFF = os.environ.get("AGENT_RULES_OFF") == "1"


def read_input():
    """Read the JSON event Claude Code sends to the hook."""
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}


def git(args, cwd):
    """Run a git command and return its output, or None if it failed."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def nearest_existing_dir(path):
    """Walk up from path until we find a folder that exists."""
    path = os.path.abspath(path)
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def repo_info(path):
    """Describe the git repository that contains `path`.

    Returns None if the path is not inside a git repository. Otherwise
    returns a dict with:
      root         top folder of this checkout
      is_worktree  True for a linked worktree (.worktrees/<task>), False for
                   the main checkout
      branch       current branch name ("" if detached)
    """
    folder = nearest_existing_dir(path)
    root = git(["rev-parse", "--show-toplevel"], folder)
    if root is None:
        return None
    git_dir = git(["rev-parse", "--absolute-git-dir"], folder)
    common_dir = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], folder)
    branch = git(["branch", "--show-current"], folder) or ""
    return {
        "root": root,
        # In a linked worktree, the private git dir differs from the shared one.
        "is_worktree": bool(git_dir and common_dir and git_dir != common_dir),
        "branch": branch,
    }


def deny(reason):
    """Block a tool call and tell Claude why (PreToolUse JSON format)."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def allow_silently():
    """Stay out of the way: no decision, normal permission rules apply."""
    sys.exit(0)
