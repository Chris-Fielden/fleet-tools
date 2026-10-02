#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool.

Runs before every shell command Claude wants to execute and blocks the
ones that break the repository rules (see CLAUDE.md). Anything not listed
here is left to Claude Code's normal permission prompts.
"""

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RULES_OFF, allow_silently, deny, read_input, repo_info  # noqa: E402
from _common import fleet_dir, is_under_fleet, real_home  # noqa: E402

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


HUMAN_ONLY_NAMES = {"idea", "alert", "dispatch", "ship"}
TICKET_HUMAN_ONLY_SUBCOMMANDS = {"close", "approve", "sync"}
# Programs that run another program: `env dispatch`, `command ship`, `sudo -u me idea`.
WRAPPERS = {"env", "command", "exec", "nohup", "time", "setsid", "stdbuf", "sudo",
            "doas", "nice", "ionice", "timeout", "xargs", "builtin", "chronic", "unbuffer"}
WRAPPER_OPTS_WITH_VALUE = {"-u", "-g", "-n", "-c", "-s", "-k", "-S", "-C", "-I", "-L", "-P"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
PYTHONS = re.compile(r"^python(\d+(\.\d+)?)?$")
# Programs that walk a whole folder tree. Pointed at your home folder (or
# above it), they would read ~/.fleet along the way.
TREE_WALKERS = {"find", "rg", "ag", "fd", "fdfind", "tar", "zip", "rsync", "du", "tree"}
RECURSIVE_FLAG = re.compile(r"^(-[a-zA-Z]*[rRa][a-zA-Z]*|--recursive|--archive|-d\s*recurse)$")
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&", "{", "}"}


def _segments(command):
    """Split a shell command into simple commands, as lists of words. Quotes
    are honoured, so 'dispatch' and "dispatch" count as dispatch."""
    command = command.replace("`", " ; ").replace("$(", " ; ( ")
    out = []
    for line in command.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|(){}<>")
        lexer.whitespace_split = True
        current = []
        for tok in lexer:
            if tok in SEPARATORS:
                if current:
                    out.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            out.append(current)
    return out


def _unwrap(words):
    """Strip VAR=value settings and wrapper programs. Returns (program, args),
    or a nested command string for `bash -c "..."` as ("-c", [string])."""
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^\w+=", w):
            i += 1
            continue
        name = os.path.basename(w)
        if name in WRAPPERS:
            i += 1
            if name == "timeout":
                while i < len(words) and words[i].startswith("-"):
                    i += 1
                i += 1  # the duration
                continue
            while i < len(words) and (words[i].startswith("-") or re.match(r"^\w+=", words[i])):
                i += 2 if words[i] in WRAPPER_OPTS_WITH_VALUE else 1
            continue
        if name in SHELLS or PYTHONS.match(name):
            rest = words[i + 1:]
            if "-c" in rest:
                j = rest.index("-c")
                return ("-c", rest[j + 1:j + 2]) if name in SHELLS else (name, rest)
            k = 0
            while k < len(rest) and rest[k].startswith("-"):
                k += 1
            if k < len(rest):  # `bash bin/ticket ...`, `python3 bin/dispatch ...`
                return os.path.basename(rest[k]), rest[k + 1:]
            return name, rest
        return name, words[i + 1:]
    return None, []


def _resolve(token, cwd):
    if token == "~" or token.startswith("~/"):
        token = real_home() + token[1:]
    if not os.path.isabs(token):
        token = os.path.join(cwd, token)
    return os.path.realpath(token)


def _is_ancestor_of_fleet(path):
    fleet = fleet_dir()
    return fleet.startswith(path.rstrip(os.sep) + os.sep) or path == os.sep


def scan_command(command, cwd, depth=0):
    """Return a reason to block, or None. Looks at every simple command in the
    line, including ones behind wrappers, in $( ), backticks and bash -c."""
    if depth > 3:
        return None
    try:
        segments = _segments(command)
    except ValueError:
        return None  # unbalanced quotes: bash would refuse it anyway
    for words in segments:
        name, args = _unwrap(words)
        if name == "-c":
            reason = scan_command(args[0] if args else "", cwd, depth + 1)
            if reason:
                return reason
            continue
        if name in HUMAN_ONLY_NAMES:
            return ("human", name)
        if name == "ticket" and args and args[0] in TICKET_HUMAN_ONLY_SUBCOMMANDS:
            return ("human", f"ticket {args[0]}")
        # Any word that is a path into ~/.fleet, symlinks included.
        paths = []
        for w in words:
            value = w.split("=", 1)[1] if re.match(r"^\w+=", w) else w
            if value.startswith(("/", "~", ".")) or "/" in value:
                if is_under_fleet(value, cwd):
                    return ("fleet", None)
                if not value.startswith("-"):
                    paths.append(value)
        # Whole-tree searches started at your home folder or above.
        recursive = name in TREE_WALKERS or (
            name in {"grep", "egrep", "fgrep", "cp", "scp", "ls", "chmod", "chown"}
            and any(RECURSIVE_FLAG.match(a) for a in args))
        if recursive:
            roots = [_resolve(p, cwd) for p in paths] or [os.path.realpath(cwd)]
            if any(_is_ancestor_of_fleet(r) for r in roots):
                return ("tree", name)
    return None


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
    found = scan_command(command, cwd)
    m = FLEET_HUMAN_ONLY.search(command) or TICKET_HUMAN_ONLY.search(command)
    if (found and found[0] == "human") or m:
        what = found[1] if found and found[0] == "human" else (
            m.group(1) if m.re is FLEET_HUMAN_ONLY else f"ticket {m.group(1)}")
        deny(f"Only the human runs `{what}`. If you have an idea or found an urgent "
             "problem, say so in your summary, or file a proposal with `ticket new`. "
             "You may use `ticket show`, `ticket list` and `ticket new`. Tests must "
             "call fleet commands from inside pytest with a temporary FLEET_HOME.")
    if (found and found[0] == "fleet") or FLEET_PATH.search(command) or fleet_dir() in command:
        deny("Don't touch the fleet's private folder (~/.fleet) directly: it holds "
             "the real tickets and the Kaneo key. Use `ticket show`, `ticket list` "
             "or `ticket new` instead. In tests, set FLEET_HOME to a temporary folder.")
    if found and found[0] == "tree":
        deny(f"Don't run `{found[1]}` across your home folder or above: it would read "
             "the fleet's private folder (~/.fleet) on the way. Search inside the "
             "project folder instead.")

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
