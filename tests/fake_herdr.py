#!/usr/bin/env python3
"""A fake herdr for tests: records its arguments, prints canned JSON.

FAKE_HERDR_LOG   file that gets one JSON line of arguments per call
FAKE_HERDR_STATE status `agent list` reports for the agent (default: working)
FAKE_HERDR_WORKSPACE label of the one workspace it knows (default: fleet)
FAKE_HERDR_WORLD  optional JSON file {"workspaces": [...], "tabs": [...], "agents": [...]}
                  that `workspace list/create`, `tab list/close` and `agent list` read
                  and change, in herdr's real reply shapes
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")

cmd = " ".join(args[:2])
world_file = os.environ.get("FAKE_HERDR_WORLD")
world = json.load(open(world_file)) if world_file else None
if world is not None and cmd in ("workspace list", "workspace create", "tab list", "tab close", "tab create", "tab focus", "agent list",
                                                   "agent start"):
    if cmd == "workspace list":
        out = {"result": {"workspaces": world["workspaces"]}}
    elif cmd == "workspace create":
        ws = {"workspace_id": f"w{len(world['workspaces']) + 20}", "label": args[args.index("--label") + 1]}
        world["workspaces"].append(ws)
        out = {"result": {"workspace": ws}}
    elif cmd == "tab list":
        wanted = args[args.index("--workspace") + 1] if "--workspace" in args else None
        out = {"result": {"tabs": [t for t in world["tabs"] if wanted in (None, t["workspace_id"])]}}
    elif cmd == "agent start":  # the new agent is working at once, in the tab of its pane
        pane = args[args.index("--pane") + 1]
        world["agents"].append({"name": args[2], "agent_status": "working",
                                "tab_id": pane.replace(":p", ":t")})
        out = {"result": {"agent": {"name": args[2]}}}
    elif cmd == "tab create":
        n = len(world["tabs"]) + 50
        ws = args[args.index("--workspace") + 1]
        world["tabs"].append({"tab_id": f"{ws}:t{n}", "label": args[args.index("--label") + 1],
                              "workspace_id": ws, "focused": False, "agent_status": "unknown"})
        out = {"result": {"tab": {"tab_id": f"{ws}:t{n}"}, "root_pane": {"pane_id": f"{ws}:p{n}"}}}
    elif cmd == "tab focus":
        for t in world["tabs"]:
            t["focused"] = t["tab_id"] == args[2]
        out = {"result": {}}
    elif cmd == "tab close":
        world["tabs"] = [t for t in world["tabs"] if t["tab_id"] != args[2]]
        world["agents"] = [a for a in world["agents"] if a.get("tab_id") != args[2]]
        out = {"result": {}}
    else:
        out = {"result": {"agents": world["agents"]}}
    json.dump(world, open(world_file, "w"))
elif cmd == "status server":   # FAKE_HERDR_SERVER: a file that exists while the server "runs"
    print("status: running" if os.path.exists(os.environ.get("FAKE_HERDR_SERVER", "")) else "status: stopped")
    sys.exit(0)
elif args[:1] == ["server"] and len(args) == 1:
    open(os.environ["FAKE_HERDR_SERVER"], "w").close()
    sys.exit(0)
elif cmd == "pane run":
    if os.environ.get("FAKE_HERDR_RUN_AS"):   # really run the command, through a stand-in program
        import shlex
        import subprocess
        subprocess.Popen([sys.executable, os.environ["FAKE_HERDR_RUN_AS"], *shlex.split(args[3])],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    out = {"result": {}}
elif cmd == "workspace create":
    out = {"result": {"workspace": {"workspace_id": "w9", "label": args[args.index("--label") + 1]}}}
elif cmd == "tab list":
    out = {"result": {"tabs": []}}
elif cmd == "workspace list":
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
        {"name": n, "agent_status": os.environ.get("FAKE_HERDR_STATE", "working")} for n in names]}}
elif cmd in ("agent prompt", "tab close"):
    out = {"result": {}}
else:
    print(json.dumps({"error": f"fake herdr: unsupported command {cmd}"}), file=sys.stderr)
    sys.exit(1)
print(json.dumps(out))
