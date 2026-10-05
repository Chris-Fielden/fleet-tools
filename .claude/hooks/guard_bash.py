#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool.

Runs before every shell command Claude wants to execute and blocks the
ones that break the repository rules (see CLAUDE.md). Anything not listed
here is left to Claude Code's normal permission prompts.

What these rules are for: stopping mistakes and casual misuse (an agent
running a fleet command, or a test that touches the real ~/.fleet). They are
not a wall against a determined program: anything running as your user can
find another route to your own files. The real wall is running agents as a
separate Linux user that cannot read ~/.fleet at all (planned for the tower).
When a command is too unusual to analyse safely, these rules refuse it.

Known limits (by design; the separate-user setup is the fix for these):
- Code the hook can't see: a script written and then run in a later step,
  `source`/`.` of a file, shell functions and aliases, and programs started
  from inside another language (for example Python's subprocess).
- Rarely used shell syntax: case statements, here-documents, arrays and
  arithmetic. Simple if/while/for, subshells, $( ) and backticks (quoted or
  not), eval and bash -c are understood. Past 16 possible folders or 6
  levels of nesting, the rules stop guessing and refuse anything risky.
- Mentions of ~/.fleet in plain text (for example a commit message) are
  refused too. That is deliberate: it is cheaper than telling text and paths
  apart, and the message says why.
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
TICKET_HUMAN_ONLY = re.compile(_CMD_START + r"(?:fleet\s+)?ticket\s+(close|approve|sync)\b")
# Verbs of the `fleet` umbrella command that only the human runs: the old
# human-only commands under their new names, plus everything that runs or
# changes the fleet itself.
FLEET_HUMAN_VERBS = {"ship", "approve", "alert", "idea", "dispatch", "start", "stop", "restart",
                     "autostart", "launcher", "ops", "status-serve"}
FLEET_VERB_HUMAN_ONLY = re.compile(
    _CMD_START + r"fleet\s+(" + "|".join(sorted(FLEET_HUMAN_VERBS)) + r")(?=\s|$|[;&|)`])")
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
REDIRECT_MARK = "\x02"  # words after this are redirection targets, not arguments
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&", "{", "}"}
# Shell keywords that can sit in front of a command: `if ship ...; then`.
KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "fi", "done"}


def _segments(command):
    """Split a shell command into simple commands, as lists of words. Quotes
    are honoured, so 'dispatch' and "dispatch" count as dispatch, and a
    newline inside quotes stays part of the quoted text."""
    command = command.replace("`", " ; ").replace("$(", " ; ( ")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    out, current, redirects = [], [], []
    pending_target = False
    for tok in lexer:
        # A redirection (> file, 2>> log, < in) can sit anywhere, even before
        # the program name. Its target still gets the path checks, but it is
        # never mistaken for the program.
        if set(tok) <= set("<>&|") and set(tok) & set("<>"):
            if current and current[-1].isdigit():
                current.pop()  # the 2 in 2>file
            pending_target = True
            continue
        if pending_target and not (set(tok) <= set(";&|(){}<>\n")):
            redirects.append(tok)
            pending_target = False
            continue
        pending_target = False
        # shlex glues neighbouring punctuation together (");" or "&&("), so any
        # all-punctuation token containing a separator character ends a command.
        if tok in SEPARATORS or (set(tok) <= set(";&|(){}<>\n") and set(tok) & set(";&|(){}\n")):
            if current or redirects:
                out.append(current + ([REDIRECT_MARK] + redirects if redirects else []))
            current, redirects = [], []
        else:
            current.append(tok)
    if current or redirects:
        out.append(current + ([REDIRECT_MARK] + redirects if redirects else []))
    return out


CHDIR_OPTS = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir"}}


def _unwrap(words, chdirs=None):
    """Strip VAR=value settings and wrapper programs. Returns (program, args),
    or a nested command string for `bash -c "..."` as ("-c", [string]).
    Folder changes made by wrappers (env -C DIR) are added to `chdirs`."""
    if chdirs is None:
        chdirs = []
    if REDIRECT_MARK in words:
        words = words[:words.index(REDIRECT_MARK)]
    i = 0
    while i < len(words):
        w = words[i]
        if w in KEYWORDS:
            i += 1
            continue
        if w == "for":  # for NAME in WORDS ... ; the loop body comes later
            return None, []
        if w == "eval":
            return ("-c", [" ".join(words[i + 1:])])
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
                opt = words[i]
                for flag in CHDIR_OPTS.get(name, ()):
                    if opt == flag and i + 1 < len(words):
                        chdirs.append(words[i + 1])
                    elif opt.startswith(flag + "=") or (len(flag) == 2 and opt.startswith(flag) and len(opt) > 2):
                        chdirs.append(opt.split("=", 1)[1] if "=" in opt else opt[2:])
                i += 2 if opt in WRAPPER_OPTS_WITH_VALUE or opt in CHDIR_OPTS.get(name, ()) else 1
            continue
        if name in SHELLS or PYTHONS.match(name):
            rest = words[i + 1:]
            flag = next((k for k, a in enumerate(rest)
                         if re.match(r"^-[a-zA-Z]*c[a-zA-Z]*$", a)), None)
            if flag is not None:
                return ("-c", rest[flag + 1:flag + 2]) if name in SHELLS else (name, rest)
            k = 0
            while k < len(rest) and rest[k].startswith("-"):
                k += 1
            if k < len(rest):  # `bash bin/ticket ...`, `python3 bin/dispatch ...`
                return os.path.basename(rest[k]), rest[k + 1:]
            return name, rest
        return name, words[i + 1:]
    return None, []


def _expand(token):
    """Expand the home folder however it is written. Returns None when the
    word still contains a variable we can't know, like $DIR."""
    home = real_home()
    if token == "~" or token.startswith("~/"):
        token = home + token[1:]
    token = token.replace("${HOME}", home).replace("$HOME", home)
    return None if "$" in token else token


def _resolve(token, cwd):
    token = _expand(token) or token
    if token == "~" or token.startswith("~/"):
        token = real_home() + token[1:]
    if not os.path.isabs(token):
        token = os.path.join(cwd, token)
    return os.path.realpath(token)


def _is_ancestor_of_fleet(path):
    fleet = fleet_dir()
    return fleet.startswith(path.rstrip(os.sep) + os.sep) or path == os.sep


MAX_PLACES = 16  # more possible folders than this and we stop guessing
MAX_DEPTH = 6
FLEET_WORDS = re.compile(r"\b(idea|alert|dispatch|ship|ticket|fleet)\b|\.fleet")
_SUB = re.compile("\x01(\\d+)\x01")


def _matching_paren(s, i):
    """Index of the ) that closes a $( whose body starts at i."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError("unbalanced quote")
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise ValueError("unbalanced quote")
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced $(")


def _pull_quoted_subs(command):
    """Bash still runs $( ) and backticks inside double quotes. Swap each one
    for a marker and return (new command, list of the commands inside), so
    they can be checked on their own. Single-quoted text is left alone."""
    out, subs, i, n, in_dq = [], [], 0, len(command), False
    while i < n:
        c = command[i]
        if c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "'" and not in_dq:
            j = command.find("'", i + 1)
            if j < 0:
                raise ValueError("unbalanced quote")
            out.append(command[i:j + 1])
            i = j + 1
            continue
        if c == '"':
            in_dq = not in_dq
        elif in_dq and command.startswith("$(", i):
            j = _matching_paren(command, i + 2)
            subs.append(command[i + 2:j])
            out.append(f"\x01{len(subs) - 1}\x01")
            i = j + 1
            continue
        elif in_dq and c == "`":
            j = i + 1
            while j < n and command[j] != "`":
                j += 2 if command[j] == "\\" else 1
            if j >= n:
                raise ValueError("unbalanced backtick")
            subs.append(command[i + 1:j])
            out.append(f"\x01{len(subs) - 1}\x01")
            i = j + 1
            continue
        out.append(c)
        i += 1
    if in_dq:
        raise ValueError("unbalanced quote")
    return "".join(out), subs


def _merge_places(places, new):
    """Add folders without duplicates. Returns (places, still_known)."""
    merged = list(dict.fromkeys(places + new))
    if len(merged) > MAX_PLACES:
        return places, False
    return merged, True


def scan_command(command, places, depth=0, cwd_known=True):
    """Return a reason to block, or None. Looks at every simple command in the
    line, including ones behind wrappers, in $( ) and backticks (quoted or
    not), eval and bash -c. `places` is every folder the command might start in."""
    if isinstance(places, str):
        places = [places]
    if depth > MAX_DEPTH:
        return ("odd", None)
    try:
        command, subs = _pull_quoted_subs(command)
        segments = _segments(command)
    except ValueError:
        # Unbalanced quotes: too odd to analyse. Refuse if it mentions fleet
        # commands or the private folder at all; otherwise bash will reject it.
        if FLEET_WORDS.search(command):
            return ("odd", None)
        return None
    places = list(dict.fromkeys(places))
    for words in segments:
        # Commands inside quoted $( ) or backticks run first, from here.
        for w in words:
            for k in _SUB.findall(w):
                reason = scan_command(subs[int(k)], places, depth + 1, cwd_known)
                if reason:
                    return reason
        chdirs = []
        name, args = _unwrap(words, chdirs)
        if name == "tar":  # tar -C DIR / --directory=DIR
            for k, a in enumerate(args):
                if a in ("-C", "--directory") and k + 1 < len(args):
                    chdirs.append(args[k + 1])
                elif a.startswith("--directory="):
                    chdirs.append(a.split("=", 1)[1])
        # Where this one command runs: here, unless a wrapper moved it.
        here, here_known = places, cwd_known
        for d in chdirs:
            e = _expand(d)
            if e is None:
                here_known = False
            elif here_known:
                here = [_resolve(e, p) for p in here]
        if here_known and chdirs and any(is_under_fleet(p) for p in here):
            return ("fleet", None)
        # Redirections happen in the calling shell, before the command (or a
        # nested shell, or cd) runs, so check them here, ahead of any `continue`.
        if REDIRECT_MARK in words:
            for w in words[words.index(REDIRECT_MARK) + 1:]:
                if not w:
                    continue
                e = _expand(w)
                if e is None:
                    continue
                if cwd_known and any(is_under_fleet(e, p) for p in places):
                    return ("fleet", None)
                if not cwd_known and is_under_fleet(e, "/"):
                    return ("fleet", None)
        if name == "-c":
            reason = scan_command(args[0] if args else "", here, depth + 1, here_known)
            if reason:
                return reason
            continue
        prefix = ""
        if name == "fleet" and args:   # `fleet <verb>` is `<verb>`, `fleet ticket close` is `ticket close`
            if args[0] in FLEET_HUMAN_VERBS:
                return ("human", f"fleet {args[0]}")
            if args[0] == "ticket":
                name, args, prefix = "ticket", args[1:], "fleet "
        if name in HUMAN_ONLY_NAMES:
            return ("human", name)
        if name == "ticket" and args and args[0] in TICKET_HUMAN_ONLY_SUBCOMMANDS:
            return ("human", f"{prefix}ticket {args[0]}")
        if name == "ticket" and args and args[0] == "risk" and any(
                a.startswith("--rai") for a in args[1:]):
            return ("human", f"{prefix}ticket risk --raise")
        if name in ("cd", "pushd"):
            rest = list(args)
            while rest and rest[0].startswith("-") and rest[0] != "-" and not (
                    name == "pushd" and re.match(r"^-\d+$", rest[0])):
                if rest.pop(0) == "--":
                    break
            target = rest[0] if rest else "~"
            # `cd -` goes to $OLDPWD, which we can't know.
            # `pushd +N` / `-N` rotates to a stacked folder: also unknown.
            unknown = target == "-" or (name == "pushd" and re.match(r"^[+-]\d+$", target))
            expanded = None if unknown else _expand(target)
            if expanded is None:
                cwd_known = False
            elif cwd_known:
                # Keep the old folders too: a cd inside ( ) doesn't last.
                places, cwd_known = _merge_places(places, [_resolve(expanded, p) for p in places])
            if cwd_known and any(is_under_fleet(p) for p in places):
                return ("fleet", None)
            continue
        # Every operand, including bare names like `sneaky`, is checked as a
        # path. Symlinks are followed, so a link into ~/.fleet is caught.
        operands = []
        for w in words:
            value = w.split("=", 1)[1] if re.match(r"^\w+=", w) else w
            if not value or value.startswith("-") or value == REDIRECT_MARK:
                continue
            expanded = _expand(value)
            if expanded is None:
                operands.append(None)  # can't tell where it points
                continue
            if here_known and any(is_under_fleet(expanded, p) for p in here):
                return ("fleet", None)
            if not here_known and is_under_fleet(expanded, "/"):
                return ("fleet", None)
            operands.append(expanded)
        # Whole-tree searches started at your home folder or above, or from a
        # place we can't work out.
        recursive = name in TREE_WALKERS or (
            name in {"grep", "egrep", "fgrep", "cp", "scp", "ls", "chmod", "chown"}
            and any(RECURSIVE_FLAG.match(a) for a in args))
        if recursive:
            paths = []
            for a in args:
                if a.startswith("-"):
                    continue
                o = _expand(a)
                if o is None or o.startswith(("/", ".", "~")) or "/" in o or (
                        here_known and any(os.path.exists(os.path.join(p, o)) for p in here)):
                    paths.append(o)
            if any(o is None for o in paths) or not here_known:
                return ("tree", name)
            roots = ([_resolve(o, p) for o in paths for p in here]
                     or [os.path.realpath(p) for p in here])
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
    found = scan_command(command, [cwd])
    m = (FLEET_HUMAN_ONLY.search(command) or TICKET_HUMAN_ONLY.search(command)
         or FLEET_VERB_HUMAN_ONLY.search(command))
    if (found and found[0] == "human") or m:
        what = found[1] if found and found[0] == "human" else (
            f"fleet {m.group(1)}" if m.re is FLEET_VERB_HUMAN_ONLY else
            m.group(1) if m.re is FLEET_HUMAN_ONLY else f"ticket {m.group(1)}")
        deny(f"Only the human runs `{what}`. If you have an idea or found an urgent "
             "problem, say so in your summary, or file a proposal with `ticket new`. "
             "You may use `ticket show`, `ticket list` and `ticket new`. Tests must "
             "call fleet commands from inside pytest with a temporary FLEET_HOME.")
    if (found and found[0] == "fleet") or FLEET_PATH.search(command) or fleet_dir() in command:
        deny("Don't touch the fleet's private folder (~/.fleet) directly: it holds "
             "the real tickets and the Kaneo key. Use `ticket show`, `ticket list` "
             "or `ticket new` instead. In tests, set FLEET_HOME to a temporary folder.")
    if found and found[0] == "odd":
        deny("That command is too unusual for the safety rules to check (unbalanced "
             "quotes or very deep nesting). Write it more simply.")
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
