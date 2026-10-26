#!/usr/bin/env python3
"""The hook a fleet Cursor review installs in its throwaway copy (BD-136).

Stdlib only; run as a script, never imported by agents:

  cursor_hook.py json STATE_DIR ROOT          print the .cursor/hooks.json to install
  cursor_hook.py shell STATE_DIR ROOT         deny (exit 2); the review may not run anything
  cursor_hook.py tool STATE_DIR ROOT          preToolUse, for EVERY tool: deny all but Read, Grep, Glob
                                              and LS; those only when every path argument is inside ROOT
                                              (a tool with no checkable path, e.g. semantic search, web,
                                              MCP, is denied)
  cursor_hook.py read STATE_DIR ROOT          allow a read INSIDE the review copy ROOT only (symlinks
                                              resolved), and keep only the model name from the payload

A hook payload also carries the account email, so it is never stored or printed: the read hook
keeps one cleaned model name in STATE_DIR/model.txt and one word per event in STATE_DIR/events.log.
"""
import json
import os
import re
import shlex
import sys

MODEL_OK = re.compile(r"[^A-Za-z0-9._:-]")


def hooks_json(state_dir: str, root: str) -> dict:
    me = os.path.abspath(__file__)

    def cmd(kind: str) -> str:
        return shlex.join([sys.executable, "-I", me, kind, state_dir, root])

    return {"version": 1, "hooks": {
        "beforeShellExecution": [{"command": cmd("shell"), "failClosed": True}],
        "preToolUse": [{"command": cmd("tool"), "failClosed": True}],
        "beforeReadFile": [{"command": cmd("read"), "failClosed": True}],
    }}


def _event(state_dir: str, word: str) -> None:
    with open(os.path.join(state_dir, "events.log"), "a", encoding="utf-8") as fh:
        fh.write(word + "\n")


def inside(path, root: str) -> bool:
    """Is `path` (absolute, or relative to ROOT) inside ROOT once symlinks are resolved?"""
    if not isinstance(path, str) or not path or "\0" in path:
        return False
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, os.path.expanduser(path)))
    return full == root or full.startswith(root + os.sep)


PATH_KEYS = ("path", "file_path", "target_directory", "directory", "dir", "paths", "glob", "glob_pattern",
             "target_file", "target_notebook")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def tool_allowed(payload, root: str) -> bool:
    """preToolUse: Read (its path is checked again by beforeReadFile), and Grep/Glob/LS with at
    least one path argument, every path argument inside ROOT. Anything else is denied."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("tool_name")
    args = payload.get("tool_input")
    if args is None:
        args = {k: v for k, v in payload.items() if k not in ("tool_name", "model")}
    if not isinstance(name, str) or not isinstance(args, dict):
        return False
    name = name.strip().lower()
    if name not in ("read", "grep", "glob", "ls"):
        return False
    paths = [s for k in PATH_KEYS for s in _strings(args.get(k))]
    # any other value that looks like a path (absolute, home or parent-relative) is checked too
    for k, v in args.items():
        if k not in PATH_KEYS:
            paths += [s for s in _strings(v) if s.startswith(("/", "~")) or ".." in s.split("/")]
    if not all(inside(p, root) for p in paths):
        return False
    return bool(paths) or name == "read"


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("json", "shell", "tool", "read"):
        print(__doc__, file=sys.stderr)
        return 64
    mode, state_dir, root = argv[1], argv[2], argv[3]
    if mode == "json":
        print(json.dumps(hooks_json(state_dir, root)))
        return 0
    raw = sys.stdin.read()   # always drained, never kept
    try:
        _event(state_dir, mode)
    except OSError:
        pass
    if mode == "shell":
        print(json.dumps({"permission": "deny", "user_message": "read-only fleet review",
                          "agent_message": "this review is read-only: no shell, no writes"}))
        return 2
    if mode == "tool":
        try:
            ok = tool_allowed(json.loads(raw), root)
        except (ValueError, AttributeError, TypeError):
            ok = False
        if ok:
            print(json.dumps({"permission": "allow"}))
            return 0
        try:
            _event(state_dir, "tool-denied")
        except OSError:
            pass
        print(json.dumps({"permission": "deny", "user_message": "read-only fleet review",
                          "agent_message": "this review is read-only: only Read, and Grep/Glob/LS "
                                           "inside the reviewed folder, are allowed"}))
        return 2
    path = None
    try:
        payload = json.loads(raw)
        model = payload.get("model")
        path = payload.get("file_path") or payload.get("path")
        if isinstance(model, str) and model:
            name = MODEL_OK.sub("", model)[:80]
            mfile = os.path.join(state_dir, "model.txt")
            seen = open(mfile, encoding="utf-8").read().split() if os.path.exists(mfile) else []
            if name and name not in seen:   # every distinct model, so a switch mid-run is visible
                with open(mfile, "a", encoding="utf-8") as fh:
                    fh.write(name + "\n")
    except (ValueError, AttributeError, OSError):
        pass   # no model recorded: the review is not counted
    if not inside(path, root):   # no path we can check is no read: fail closed
        _event(state_dir, "read-denied")
        print(json.dumps({"permission": "deny", "user_message": "outside the review copy",
                          "agent_message": "only files inside the reviewed folder can be read"}))
        return 2
    print(json.dumps({"permission": "allow"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
