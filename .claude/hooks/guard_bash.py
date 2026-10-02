#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool.

Runs before every shell command Claude wants to execute and blocks the
ones that break the repository rules (see CLAUDE.md). Anything not listed
here is left to Claude Code's normal permission prompts.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RULES_OFF, allow_silently, deny, read_input, repo_info  # noqa: E402
from _common import fleet_dir  # noqa: E402

PROTECTED_BRANCHES = {"main", "master"}

# Git commands that change history or branches. Fine inside a task
# worktree, never allowed from the main checkout.
GIT_WRITE = re.compile(
    r"\bgit\s+(commit|merge|rebase|cherry-pick|revert|push|am|pull|"
    r"reset\s+--hard|checkout\s+(main|master)\b|switch\s+(main|master)\b)"
)

# Changing the cluster directly skips GitOps. Read-only commands stay allowed.
CLUSTER_WRITE = re.compile(
    r"\b(kubectl|k3s\s+kubectl|oc)\s+(apply|create|delete|edit|patch|replace|"
    r"scale|set|label|annotate|rollout\s+(restart|undo)|drain|cordon|taint)\b"
    r"|\bhelm\s+(install|upgrade|uninstall|delete|rollback)\b"
    r"|\bargocd\s+app\s+(sync|delete|set|create|rollback)\b"
)

FORCE_PUSH = re.compile(r"\bgit\s+push\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)")
PUSH_PROTECTED = re.compile(r"\bgit\s+push\b.*\b(main|master)\b")

# rm -rf aimed at something broad: /, ~, $HOME, .., ., or a bare *.
BROAD_RM = re.compile(
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*|-r\s+-f|-f\s+-r)"
    r"\s+(/|~|\$HOME|\.\.?(/)?|\*)(\s|$)"
)

HUMAN_ONLY_SCRIPTS = re.compile(r"finish-task\.sh")
# Flags that switch off an agent's own safety sandbox or permission prompts.
UNSAFE_AGENT_FLAGS = re.compile(
    r"--dangerously-skip-permissions|--dangerously-bypass-approvals-and-sandbox"
    r"|--dangerously-bypass-hook-trust|danger-full-access|(^|\s)--yolo(\s|$)"
)
WORKTREE_CLEANUP = re.compile(r"\bgit\s+(worktree\s+(remove|prune)|branch\s+-D)\b")
ENV_WRITE = re.compile(r"(>|>>|tee\s+(-a\s+)?)\s*\S*\.env(\.\S+)?(\s|$)")

# Fleet commands only the human runs. Matched where a command starts: at the
# beginning, or after ; & | ( $( or a backtick, optionally behind VAR=value
# settings or a path like bin/ or ~/.local/bin/.
_CMD_START = r"(?:^|[;&|(\n`]|\$\()\s*(?:\w+=\S*\s+)*(?:\S*/)?"
FLEET_HUMAN_ONLY = re.compile(_CMD_START + r"(idea|alert|dispatch|ship)(?=\s|$|[;&|)`])")
TICKET_HUMAN_ONLY = re.compile(_CMD_START + r"ticket\s+(close|approve|sync)\b")
# Any mention of the .fleet folder as a path: ~/.fleet, $HOME/.fleet,
# /home/<you>/.fleet, or a bare .fleet when standing in the home folder.
FLEET_PATH = re.compile(r"(^|[^\w.-])\.fleet(/|$|[\s'\";|&)])")


def main():
    if RULES_OFF:
        allow_silently()

    event = read_input()
    command = (event.get("tool_input") or {}).get("command", "")
    cwd = event.get("cwd") or os.getcwd()
    if not command:
        allow_silently()

    # Rules that apply everywhere.
    if HUMAN_ONLY_SCRIPTS.search(command):
        deny("Only the human runs scripts/finish-task.sh. Tell them the task is "
             "ready: summarize what changed, the check results, and the review.")
    if UNSAFE_AGENT_FLAGS.search(command):
        deny("Don't start an agent with its safety sandbox or permission prompts "
             "turned off. Use scripts/ask-codex.sh, scripts/ask-claude.sh or "
             "scripts/review.sh, which run the other agent read-only.")
    if CLUSTER_WRITE.search(command):
        deny("GitOps rule: don't change the cluster directly. Edit the YAML under "
             "k8s/, commit it on your task branch, and ArgoCD applies it after the "
             "human merges. Read-only commands (kubectl get/describe/logs/diff) are fine.")
    if FORCE_PUSH.search(command):
        deny("Force-pushing is not allowed. It can destroy other people's work.")
    if PUSH_PROTECTED.search(command):
        deny("Pushing to main/master is human-only, through scripts/finish-task.sh.")
    if BROAD_RM.search(command):
        deny("That rm -rf targets a broad path (/, ~, .., . or *). Delete specific "
             "files or folders instead.")
    if WORKTREE_CLEANUP.search(command):
        deny("Removing worktrees or force-deleting branches is done by the human "
             "(scripts/finish-task.sh cleans up after merging).")
    if ENV_WRITE.search(command):
        deny("Don't write .env files. Secrets never go in the repo; ask the human "
             "to put real values in place.")
    m = FLEET_HUMAN_ONLY.search(command)
    if m:
        deny(f"Only the human runs `{m.group(1)}`. If you have an idea or found an "
             "urgent problem, say so in your summary, or file a proposal with "
             "`ticket new`. Tests must call these commands with a temporary "
             "FLEET_HOME from inside pytest, not from the shell.")
    m = TICKET_HUMAN_ONLY.search(command)
    if m:
        deny(f"Only the human runs `ticket {m.group(1)}`. You may use `ticket show`, "
             "`ticket list` and `ticket new`.")
    if FLEET_PATH.search(command) or fleet_dir() in command:
        deny("Don't touch the fleet's private folder (~/.fleet) directly: it holds "
             "the real tickets and the Kaneo key. Use `ticket show`, `ticket list` "
             "or `ticket new` instead. In tests, set FLEET_HOME to a temporary folder.")

    # Rules that depend on where Claude is standing.
    info = repo_info(cwd)
    if info is None:
        allow_silently()  # not inside a git repo; nothing to protect here

    if not info["is_worktree"] and GIT_WRITE.search(command):
        deny("You're in the main checkout, which belongs to the human. Git changes "
             "happen in a task worktree. Ask the human to run "
             "`scripts/new-task.sh <task-name>` and start you in "
             "`.worktrees/<task-name>`.")
    if info["is_worktree"] and info["branch"] in PROTECTED_BRANCHES and GIT_WRITE.search(command):
        deny(f"This worktree is on '{info['branch']}'. Work on a task/<name> branch.")

    allow_silently()


if __name__ == "__main__":
    main()
