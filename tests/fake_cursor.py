"""A fake Cursor CLI (`agent`) for tests: no network, no spend, no real Cursor.

install(bindir, **config) writes bindir/agent plus bindir/stub.json (the behaviour switches,
read at run time because the probe script runs it under `env -i`). Every call is logged as one
JSON line in bindir/stub.log: the arguments, whether a key was set (never its value), HOME, and
what the working folder held. It honours .cursor/hooks.json the way the real CLI does: it runs
the hook command for an event with a payload on stdin and treats exit 2 as a deny.
"""
import json
import stat
import sys
from pathlib import Path

SCRIPT = r'''#!{python}
import json, os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "stub.json")))
args = sys.argv[1:]
log = {{"args": [a for a in args if not a.startswith("You are a code reviewer")],
        "has_key": bool(os.environ.get("CURSOR_API_KEY")), "home": os.environ.get("HOME"),
        "cwd": os.getcwd(), "home_files": sorted(os.listdir(os.environ["HOME"])) if os.path.isdir(os.environ.get("HOME", "")) else None}}
log["tree"] = sorted(os.path.relpath(os.path.join(r, f), ".") for r, d, fs in os.walk(".") for f in fs)
with open(os.path.join(here, "stub.log"), "a") as fh:
    fh.write(json.dumps(log) + "\n")
if args[:1] == ["--version"]:
    print(cfg.get("version", "STUB 1.2.3")); sys.exit(0)
if args[:1] == ["models"]:
    print("big-model - Big\nmini-model - Mini"); sys.exit(0)
if args[:1] == ["status"]:
    print('{{"loggedIn":true}}'); sys.exit(0)
prompt = args[-1] if args else ""
mode = args[args.index("--mode") + 1] if "--mode" in args else "agent"
model = args[args.index("--model") + 1] if "--model" in args else ""
if "no-such-model-probe" in args:
    print('{{"type":"result","is_error":true}}'); sys.exit(1)
if cfg.get("exit"):
    sys.exit(cfg["exit"])
if cfg.get("needs_key") and not os.environ.get("CURSOR_API_KEY"):
    print(json.dumps({{"type": "result", "is_error": True, "result": "not logged in"}})); sys.exit(1)

def hook(event, payload):
    """Run the project hook for `event`; True when it allows."""
    if cfg.get("ignore_hooks"):
        return True
    try:
        hooks = json.load(open(".cursor/hooks.json"))["hooks"].get(event, [])
    except (OSError, ValueError):
        return True
    for h in hooks:
        p = subprocess.run(h["command"], shell=True, input=json.dumps(payload), capture_output=True, text=True)
        if p.returncode == 2 or (p.returncode and h.get("failClosed")):
            return False
    return True

payload_model = cfg.get("hook_model", model)
out = []
if "touch shell-marker" in prompt:
    if hook("beforeShellExecution", {{"command": "touch shell-marker", "model": payload_model}}) and mode != "ask":
        open("shell-marker", "w").close()
if "create a file named" in prompt.lower():
    name = prompt.lower().split("create a file named ")[1].split()[0]
    if hook("preToolUse", {{"tool_name": "Write", "model": payload_model}}) and (mode != "ask" or cfg.get("ask_writes")):
        open(name.rstrip(".,"), "w").write("x")
if "note.txt" in prompt:
    if hook("beforeReadFile", {{"file_path": "note.txt", "model": payload_model, "email": "someone@example.com"}}):
        out.append(open("note.txt").read())
if "every skill" in prompt:
    out.append("no skills" if not cfg.get("skill_leak") else "skill leak: " + prompt.split("named ")[1].split()[0])
if prompt.startswith("You are a code reviewer"):
    if not cfg.get("no_read_hook"):
        hook("beforeReadFile", {{"file_path": "x", "model": payload_model, "email": "someone@example.com"}})
    out.append(cfg.get("review", "VERDICT: PASS\nMUST FIX:\nSUMMARY: ok"))
    if cfg.get("leak"):
        out.append("contact someone@example.com key " + os.environ.get("CURSOR_API_KEY", ""))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "\n".join(out) or "ok", "duration_ms": 5}}))
'''


def install(bindir: Path, **config) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    agent = bindir / "agent"
    agent.write_text(SCRIPT.format(python=sys.executable))
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    (bindir / "stub.json").write_text(json.dumps(config))
    (bindir / "stub.log").touch()
    return bindir


def calls(bindir: Path) -> list:
    return [json.loads(line) for line in (bindir / "stub.log").read_text().splitlines()]
