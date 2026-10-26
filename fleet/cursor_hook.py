#!/usr/bin/env python3
"""The hook a fleet Cursor review installs in its throwaway copy (BD-136).

Stdlib only; run as a script, never imported by agents:

  cursor_hook.py json STATE_DIR          print the .cursor/hooks.json to install
  cursor_hook.py shell|write STATE_DIR   deny (exit 2); the review may not run or write anything
  cursor_hook.py read STATE_DIR          allow, and keep only the model name from the payload

A hook payload also carries the account email, so it is never stored or printed: the read hook
keeps one cleaned model name in STATE_DIR/model.txt and one word per event in STATE_DIR/events.log.
"""
import json
import os
import re
import sys

MODEL_OK = re.compile(r"[^A-Za-z0-9._:-]")


def hooks_json(state_dir: str) -> dict:
    me = os.path.abspath(__file__)

    def cmd(kind: str) -> str:
        return f"{sys.executable} -I {me} {kind} {state_dir}"

    return {"version": 1, "hooks": {
        "beforeShellExecution": [{"command": cmd("shell"), "failClosed": True}],
        "preToolUse": [{"command": cmd("write"), "matcher": "Write|Edit|StrReplace|MultiEdit|Delete",
                        "failClosed": True}],
        "beforeReadFile": [{"command": cmd("read"), "failClosed": True}],
    }}


def _event(state_dir: str, word: str) -> None:
    with open(os.path.join(state_dir, "events.log"), "a", encoding="utf-8") as fh:
        fh.write(word + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("json", "shell", "write", "read"):
        print(__doc__, file=sys.stderr)
        return 64
    mode, state_dir = argv[1], argv[2]
    if mode == "json":
        print(json.dumps(hooks_json(state_dir)))
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
    try:
        model = json.loads(raw).get("model")
        if isinstance(model, str) and model:
            with open(os.path.join(state_dir, "model.txt"), "w", encoding="utf-8") as fh:
                fh.write(MODEL_OK.sub("", model)[:80] + "\n")
    except (ValueError, AttributeError, OSError):
        pass   # no model recorded: the review is not counted
    print(json.dumps({"permission": "allow"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
