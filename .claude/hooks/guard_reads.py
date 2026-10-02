#!/usr/bin/env python3
"""PreToolUse hook for Claude Code's file-reading tools (Read, Grep, Glob).

Blocks reading anything in the fleet's private folder (~/.fleet), which holds
the real tickets, the Kaneo API key and the Kaneo card map. Agents get ticket
details through `ticket show` and `ticket list` instead. Everything else is
left to Claude Code's normal permission rules.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RULES_OFF, allow_silently, deny, is_under_fleet, read_input  # noqa: E402


def main():
    if RULES_OFF:
        allow_silently()

    event = read_input()
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and is_under_fleet(value, cwd):
            deny("Don't read the fleet's private folder (~/.fleet) directly: it holds "
                 "the real tickets and the Kaneo key. Use `ticket show` or "
                 "`ticket list` instead.")
    # A Glob pattern can point into the folder by itself, e.g. "~/.fleet/**".
    pattern = tool_input.get("pattern")
    if isinstance(pattern, str) and (pattern.startswith("~") or os.path.isabs(pattern)):
        base = pattern.split("*", 1)[0]
        if is_under_fleet(base, cwd):
            deny("Don't search the fleet's private folder (~/.fleet). Use "
                 "`ticket show` or `ticket list` instead.")

    allow_silently()


if __name__ == "__main__":
    main()
