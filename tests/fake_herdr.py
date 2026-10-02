#!/usr/bin/env python3
"""A fake herdr for tests: records its arguments, prints canned JSON.

FAKE_HERDR_LOG   file that gets one JSON line of arguments per call
FAKE_HERDR_STATE status `agent list` reports for the agent (default: working)
FAKE_HERDR_WORKSPACE label of the one workspace it knows (default: fleet)
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")

cmd = " ".join(args[:2])
if cmd == "workspace list":
    out = {"result": {"workspaces": [
        {"workspace_id": "w1", "label": os.environ.get("FAKE_HERDR_WORKSPACE", "fleet")}]}}
elif cmd == "tab create":
    out = {"result": {"tab": {"tab_id": "w1:t7"}, "root_pane": {"pane_id": "w1:p7"}}}
elif cmd == "agent start":
    out = {"result": {"agent": {"name": args[2]}}}
elif cmd == "agent list":
    names = [json.loads(line)[2] for line in open(os.environ["FAKE_HERDR_LOG"])
             if json.loads(line)[:2] == ["agent", "start"]]
    out = {"result": {"agents": [
        {"name": n, "status": os.environ.get("FAKE_HERDR_STATE", "working")} for n in names]}}
elif cmd in ("agent prompt", "tab close"):
    out = {"result": {}}
else:
    print(json.dumps({"error": f"fake herdr: unsupported command {cmd}"}), file=sys.stderr)
    sys.exit(1)
print(json.dumps(out))
