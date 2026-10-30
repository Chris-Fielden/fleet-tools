"""The containment hook a Cursor probe, stress run or gauntlet attempt installs (fleet/cursor_hook.py)."""
import json
import shlex
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "fleet" / "cursor_hook.py"
EMAIL = "someone@example.com"


def hook_run(mode, state, root, payload):
    return subprocess.run(["python3", "-I", str(HOOK), mode, str(state), str(root)], input=json.dumps(payload),
                          capture_output=True, text=True)


def test_the_read_hook_allows_only_files_inside_the_copy(tmp_path):
    state, root, outside = tmp_path / "state", tmp_path / "copy", tmp_path / "outside.txt"
    state.mkdir()
    root.mkdir()
    outside.write_text("secret")
    (root / "ok.txt").write_text("x")
    (root / "link").symlink_to(outside)
    for path, allowed in [("ok.txt", True), (str(root / "ok.txt"), True), (str(outside), False),
                          ("../outside.txt", False), ("link", False), ("/etc/passwd", False), (None, False)]:
        r = hook_run("read", state, root, {"model": "composer-2.5", "file_path": path})
        assert (r.returncode == 0) is allowed, path
        assert ("allow" in r.stdout) is allowed
    assert (state / "model.txt").read_text().strip() == "composer-2.5"


def test_the_tool_hook_denies_all_but_read_and_checked_searches(tmp_path):
    state, root, outside = tmp_path / "state", tmp_path / "copy", tmp_path / "outside"
    state.mkdir()
    root.mkdir()
    outside.mkdir()
    (root / "sub").mkdir()
    (root / "link").symlink_to(outside)

    def tool(name, args=None):
        p = {"model": "composer-2.5", "tool_name": name}
        if args is not None:
            p["tool_input"] = args
        return hook_run("tool", state, root, p).returncode == 0

    # denied: not on the list, whatever its arguments
    for name in ("Write", "Edit", "Delete", "Shell", "SemanticSearch", "WebSearch", "WebFetch",
                 "MCP:thing", "Task", "NoSuchTool"):
        assert not tool(name, {"path": str(root)}), name
    assert not hook_run("tool", state, root, {"model": "x"}).returncode == 0   # no tool name
    assert not hook_run("tool", state, root, ["x"]).returncode == 0            # not an object
    # allowed: Read, and Grep/Glob/LS with every path inside the copy
    assert tool("Read", {"path": "sub/x.py"}) and tool("Read")
    assert tool("Grep", {"pattern": "x", "path": str(root)}) and tool("grep", {"pattern": "x", "path": "sub"})
    assert tool("Glob", {"glob_pattern": "**/*.py", "target_directory": str(root / "sub")})
    assert tool("LS", {"target_directory": "."})
    # denied: a path outside, through .., through a symlink, a second outside path, or no path at all
    assert not tool("Grep", {"pattern": "x", "path": str(outside)})
    assert not tool("Grep", {"pattern": "x", "path": "../outside"})
    assert not tool("Grep", {"pattern": "x", "path": "link"})
    assert not tool("Glob", {"glob_pattern": str(outside / "*.txt")})
    assert not tool("Glob", {"glob_pattern": "**/*", "target_directory": str(outside)})
    assert not tool("Grep", {"pattern": "x", "path": "sub", "glob": "../*"})
    assert not tool("Grep", {"pattern": "x", "paths": ["sub", str(outside)]})
    assert not tool("Grep", {"pattern": "x", "extra": "/etc/passwd", "path": "sub"})
    assert not tool("Grep", {"pattern": "x"}) and not tool("LS", {}) and not tool("Grep")
    assert not tool("Read", {"path": str(outside / "f")})
    events = (state / "events.log").read_text()
    assert events.count("tool-denied") > 10 and "tool-denied-grep" in events and "tool-denied-semanticsearch" in events


def test_the_hooks_json_runs_the_tool_hook_for_every_tool(tmp_path):
    doc = json.loads(subprocess.run(["python3", "-I", str(HOOK), "json", str(tmp_path), str(tmp_path)],
                                    capture_output=True, text=True, check=True).stdout)
    pre = doc["hooks"]["preToolUse"]
    assert len(pre) == 1 and "matcher" not in pre[0] and pre[0]["failClosed"] is True and " tool " in pre[0]["command"]
    assert all(h["failClosed"] for hs in doc["hooks"].values() for h in hs)


def test_the_hook_keeps_the_model_only(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    hook = HOOK
    payload = json.dumps({"model": "composer-2.5", "email": EMAIL, "file_path": "x"})
    ok = subprocess.run(["python3", "-I", str(hook), "read", str(state), str(tmp_path)], input=payload,
                        capture_output=True, text=True)
    assert ok.returncode == 0 and "allow" in ok.stdout and EMAIL not in ok.stdout
    for mode in ("shell", "tool"):
        bad = subprocess.run(["python3", "-I", str(hook), mode, str(state), str(tmp_path)], input=payload,
                             capture_output=True, text=True)
        assert bad.returncode == 2 and "deny" in bad.stdout
    assert (state / "model.txt").read_text().strip() == "composer-2.5"
    assert EMAIL not in "".join(p.read_text() for p in state.iterdir())



def test_hook_commands_are_shell_quoted(tmp_path):
    weird = tmp_path / "a b;c"
    cmd = json.loads(subprocess.run(["python3", "-I", str(HOOK), "json", str(weird), str(weird)],
                                    capture_output=True, text=True).stdout)["hooks"]["beforeReadFile"][0]["command"]
    assert shlex.split(cmd)[-2:] == [str(weird), str(weird)]
