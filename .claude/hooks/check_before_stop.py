#!/usr/bin/env python3
"""Stop hook: runs when Claude thinks it is finished.

If Claude changed anything in a task worktree, this runs scripts/check.sh.
When the checks fail, or work is left uncommitted, it sends Claude back to
fix it instead of letting it stop. To avoid an endless loop, it gives up
after a few attempts in a row and warns the human instead.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RULES_OFF, git, read_input, repo_info  # noqa: E402

MAX_ATTEMPTS = 3          # how many times in a row we send Claude back
CHECK_TIMEOUT = 540       # seconds; the hook itself times out at 600
OUTPUT_LINES = 40         # how much check output to show Claude


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def warn_human(message):
    print(json.dumps({"systemMessage": message}))
    sys.exit(0)


def attempts_file(session_id):
    safe = "".join(c for c in (session_id or "unknown") if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"agent-rules-stop-{safe}")


def bump_attempts(session_id, stop_hook_active):
    """Count consecutive blocked stops. A fresh stop (not caused by our own
    previous block) starts the count over."""
    path = attempts_file(session_id)
    count = 0
    if stop_hook_active and os.path.exists(path):
        try:
            with open(path) as f:
                count = int(f.read().strip() or 0)
        except (OSError, ValueError):
            count = 0
    count += 1
    with open(path, "w") as f:
        f.write(str(count))
    return count


def reset_attempts(session_id):
    try:
        os.remove(attempts_file(session_id))
    except OSError:
        pass


def base_branch(root):
    for name in ("main", "master"):
        if git(["rev-parse", "--verify", "--quiet", name], root) is not None:
            return name
    return None


def main():
    if RULES_OFF:
        sys.exit(0)

    event = read_input()
    session_id = event.get("session_id", "")
    cwd = event.get("cwd") or os.getcwd()

    info = repo_info(cwd)
    if info is None or not info["is_worktree"]:
        sys.exit(0)  # only task worktrees are gated
    root = info["root"]

    dirty = bool(git(["status", "--porcelain"], root))
    base = base_branch(root)
    ahead = 0
    if base:
        ahead = int(git(["rev-list", "--count", f"{base}..HEAD"], root) or 0)
    if not dirty and ahead == 0:
        reset_attempts(session_id)
        sys.exit(0)  # nothing changed, nothing to check

    attempt = bump_attempts(session_id, bool(event.get("stop_hook_active")))
    if attempt > MAX_ATTEMPTS:
        reset_attempts(session_id)
        warn_human(f"Agent rules: checks still failing (or work uncommitted) after "
                   f"{MAX_ATTEMPTS} tries in {root}. Claude stopped; please take a look.")

    check = os.path.join(root, "scripts", "check.sh")
    if not os.path.isfile(check):
        block("scripts/check.sh is missing from this worktree, so the work can't be "
              "verified. Tell the human.")

    try:
        result = subprocess.run(["bash", check], cwd=root, capture_output=True,
                                text=True, timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        block(f"scripts/check.sh took longer than {CHECK_TIMEOUT}s. Find out why "
              "(a hanging test?) and fix it.")

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip().splitlines()
        tail = "\n".join(output[-OUTPUT_LINES:])
        block("scripts/check.sh failed, so the task isn't done. Fix the cause "
              "(don't weaken the checks), then finish again.\n\nLast output:\n" + tail)

    if dirty:
        block("Checks pass, but you have uncommitted changes. Commit them on your "
              "task branch with a clear message, then finish.")

    reset_attempts(session_id)
    sys.exit(0)


if __name__ == "__main__":
    main()
