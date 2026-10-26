#!/usr/bin/env python3
"""The hook a fleet Cursor review installs in its throwaway copy (BD-136).

Stdlib only; run as a script, never imported by agents:

  cursor_hook.py json STATE_DIR ROOT          print the .cursor/hooks.json to install
  cursor_hook.py shell|write STATE_DIR ROOT   deny (exit 2); the review may not run or write anything
  cursor_hook.py read STATE_DIR ROOT          allow a read INSIDE the review copy ROOT only (symlinks
                                              resolved), and keep only the model name from the payload

A hook payload also carries the account email, so it is never stored or printed: the read hook
keeps one cleaned model name in STATE_DIR/model.txt and one word per event in STATE_DIR/events.log.
"""
import json
import os
import re
import sys

MODEL_OK = re.compile(r"[^A-Za-z0-9._:-]")


def hooks_json(state_dir: str, root: str) -> dict:
    me = os.path.abspath(__file__)

    def cmd(kind: str) -> str:
        return f"{sys.executable} -I {me} {kind} {state_dir} {root}"

    return {"version": 1, "hooks": {
        "beforeShellExecution": [{"command": cmd("shell"), "failClosed": True}],
        "preToolUse": [{"command": cmd("write"), "matcher": "Write|Edit|StrReplace|MultiEdit|Delete",
                        "failClosed": True}],
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


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("json", "shell", "write", "read"):
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
    if mode in ("shell", "write"):
        print(json.dumps({"permission": "deny", "user_message": "read-only fleet review",
                          "agent_message": "this review is read-only: no shell, no writes"}))
        return 2
    path = None
    try:
        payload = json.loads(raw)
        model = payload.get("model")
        path = payload.get("file_path") or payload.get("path")
        if isinstance(model, str) and model:
            with open(os.path.join(state_dir, "model.txt"), "w", encoding="utf-8") as fh:
                fh.write(MODEL_OK.sub("", model)[:80] + "\n")
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
