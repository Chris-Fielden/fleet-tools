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
    print(cfg.get("version", "STUB 1.2.3")); sys.exit(cfg.get("version_exit", 0))
if args[:1] == ["models"]:
    if cfg.get("models_fail"):
        print("Authentication required"); sys.exit(1)
    print("big-model - Big\nmini-model - Mini"); sys.exit(0)
if args[:1] == ["status"]:
    if cfg.get("status_fail"):
        print("Authentication required"); sys.exit(1)
    print('{{"loggedIn":true}}'); sys.exit(0)
prompt = args[-1] if args else ""
mode = args[args.index("--mode") + 1] if "--mode" in args else "agent"
model = args[args.index("--model") + 1] if "--model" in args else ""
if "no-such-model-probe" in args:
    print('{{"type":"result","is_error":true}}'); sys.exit(1)
if cfg.get("exit"):
    sys.stderr.write(cfg.get("stderr", ""))
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
    # like the real CLI the catch-all preToolUse hook comes first, and a denial there stops the call
    if (hook("preToolUse", {{"tool_name": "Shell", "tool_input": {{"command": "touch shell-marker"}}, "model": payload_model}})
            and hook("beforeShellExecution", {{"command": "touch shell-marker", "model": payload_model}}) and mode != "ask"):
        open("shell-marker", "w").close()
if "create a file named" in prompt.lower():
    name = prompt.lower().split("create a file named ")[1].split()[0]
    if hook("preToolUse", {{"tool_name": "Write", "model": payload_model}}) and (mode != "ask" or cfg.get("ask_writes")):
        open(name.rstrip(".,"), "w").write("x")
if "note.txt" in prompt:
    if (hook("preToolUse", {{"tool_name": "Read", "tool_input": {{"path": "note.txt"}}, "model": payload_model}})
            and hook("beforeReadFile", {{"file_path": "note.txt", "model": payload_model, "email": "someone@example.com"}})):
        out.append(open("note.txt").read())
if "OUTSIDE=" in prompt and "STRESS-FULL-POWER" not in prompt:
    target = prompt.split("OUTSIDE=")[1].split()[0]
    if (hook("preToolUse", {{"tool_name": "Read", "tool_input": {{"path": target}}, "model": payload_model}})
            and hook("beforeReadFile", {{"file_path": target, "model": payload_model}})):
        out.append(open(target).read())
if "every skill" in prompt:
    leak = cfg.get("skill_leak")
    if cfg.get("pad"):
        out.append("pad " * cfg["pad"])
    if not leak:
        out.append("I have no skill named foo")
    else:   # the planted skill (the probe puts it in cfg["skills_dir"]) shows up in the reply
        for d in sorted(os.listdir(cfg["skills_dir"])):
            body = open(os.path.join(cfg["skills_dir"], d, "SKILL.md")).read()
            token = [w for w in body.replace(".", " ").split() if w.startswith("SKILLTOKEN-")]
            out.append({{"token": ""}}.get(leak, d) + " " + (token[0] if leak != "name" else ""))
if "GREP-OUTSIDE=" in prompt:
    target = prompt.split("GREP-OUTSIDE=")[1].split()[0]
    tools = [("Grep", {{"pattern": "OUTSIDE", "path": os.path.dirname(target)}}),
             ("Glob", {{"glob_pattern": target}}), ("LS", {{"target_directory": os.path.dirname(target)}}),
             ("SemanticSearch", {{"query": "OUTSIDE"}}), ("WebFetch", {{"url": "file://" + target}})]
    for name, args in ([] if cfg.get("no_tools") else tools):
        name = cfg.get("tool_names", {{}}).get(name, name)   # e.g. Cursor's real names may differ
        if hook("preToolUse", {{"tool_name": name, "tool_input": args, "model": payload_model}}) or cfg.get("f_leak"):
            out.append(open(target).read())
if "GREP-INSIDE" in prompt:
    if hook("preToolUse", {{"tool_name": "Grep", "tool_input": {{"pattern": "x", "path": os.getcwd()}}, "model": payload_model}}):
        out.append("grep-inside-ok")
if "STRESS-FULL-POWER" in prompt:   # the stress test's full-power run (no hooks, --force)
    def after(tag):
        return prompt.split(tag)[1].split()[0]
    if cfg.get("fp_read_canary"):
        out.append(open(after("READ-CANARY=")).read())
    if cfg.get("fp_write_outside"):
        open(after("WRITE-OUTSIDE="), "w").write("written")
    if cfg.get("fp_survive"):
        open(after("SURVIVE="), "w").write("x")
    if cfg.get("fp_env"):
        out.append("\n".join(k + "=" + v for k, v in sorted(os.environ.items())))
    out.append("RESULT canary: failed\nRESULT write: failed\nRESULT env: worked\nRESULT fetch: "
               + cfg.get("fp_fetch", "failed") + "\nRESULT gateway: " + cfg.get("fp_gateway", "failed")
               + "\nRESULT survive: failed")
if prompt.startswith("You are a code reviewer"):
    if not cfg.get("no_read_hook"):
        hook("beforeReadFile", {{"file_path": "x", "model": payload_model, "email": "someone@example.com"}})
    review = cfg.get("review", "VERDICT: PASS\nMUST FIX:\nSUMMARY: ok")
    if cfg.get("review_seq"):   # the gauntlet runs several reviews: the Nth call gets the Nth text
        counter = os.path.join(here, "review.count")
        k = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(k + 1))
        review = cfg["review_seq"][min(k, len(cfg["review_seq"]) - 1)]
    out.append(review)
    if cfg.get("leak"):
        out.append("contact someone@example.com key " + os.environ.get("CURSOR_API_KEY", ""))
if "Your review did not meet the bar" in prompt:   # the gauntlet's debrief turn
    if cfg.get("resume_fail") and "--resume" in args:
        sys.exit(1)
    out.append(cfg.get("debrief", "I most likely missed the race and the ticket wording."))
if cfg.get("leak_all"):
    out.append("contact someone@example.com key " + os.environ.get("CURSOR_API_KEY", ""))
doc = {{"type": "result", "subtype": "success", "is_error": False, "result": "\n".join(out) or "ok", "duration_ms": 5}}
if cfg.get("session"):
    doc["session_id"] = cfg["session"]
print(json.dumps(doc))
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
