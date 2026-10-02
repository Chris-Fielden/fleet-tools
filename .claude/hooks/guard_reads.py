#!/usr/bin/env python3
"""PreToolUse hook for Claude Code's file-reading tools (Read, Grep, Glob).

Blocks reading anything in the fleet's private folder (~/.fleet), which holds
the real tickets, the Kaneo API key and the Kaneo card map, and blocks
searches that start at your home folder (or above it), since they would walk
into ~/.fleet on the way. Agents get ticket details through `ticket show` and
`ticket list` instead. Everything else is left to Claude Code's normal
permission rules.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (RULES_OFF, allow_silently, deny, fleet_dir, is_under_fleet,  # noqa: E402
                     read_input, real_home)

GLOB_CHARS = "*?[{"


def resolve(path, cwd):
    if path == "~" or path.startswith("~/"):
        path = real_home() + path[1:]
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def is_ancestor_of_fleet(path):
    fleet = fleet_dir()
    return path == os.sep or fleet.startswith(path.rstrip(os.sep) + os.sep)


def main():
    if RULES_OFF:
        allow_silently()

    event = read_input()
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and is_under_fleet(value, cwd):
            deny("Don't read the fleet's private folder (~/.fleet) directly: it holds "
                 "the real tickets and the Kaneo key. Use `ticket show` or "
                 "`ticket list` instead.")

    # Grep and Glob search a whole folder tree, starting at `path` (or the
    # current folder). A Glob pattern may also carry folders of its own.
    path = tool_input.get("path")
    pattern = tool_input.get("pattern")
    searches = tool in ("Grep", "Glob") or isinstance(path, str)
    if searches:
        root = resolve(path, cwd) if isinstance(path, str) and path else os.path.realpath(cwd)
        if is_under_fleet(root, cwd):
            deny("Don't search the fleet's private folder (~/.fleet). Use "
                 "`ticket show` or `ticket list` instead.")
        if tool == "Glob" and isinstance(pattern, str):
            # The fixed part of the pattern, before any wildcard: "~/.fleet/**" -> "~/.fleet"
            cut = min([pattern.find(c) for c in GLOB_CHARS if c in pattern] or [len(pattern)])
            fixed = pattern[:cut].rsplit("/", 1)[0] if cut < len(pattern) else pattern
            base = resolve(fixed, root) if fixed else root
            if is_under_fleet(base, cwd) or ".fleet" in pattern.split("/"):
                deny("Don't search the fleet's private folder (~/.fleet). Use "
                     "`ticket show` or `ticket list` instead.")
            root = base
        if is_ancestor_of_fleet(root):
            deny("Don't search your whole home folder (or above it): that would read "
                 "the fleet's private folder (~/.fleet) on the way. Search inside the "
                 "project folder instead.")

    allow_silently()


if __name__ == "__main__":
    main()
