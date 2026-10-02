"""Tests for the rules-kit hooks that keep agents out of the fleet's private
folder (~/.fleet) and away from the human-only fleet commands.

Each test runs a hook exactly as Claude Code would: as a separate program,
with the tool call as JSON on standard input. Nothing here reads or writes the
real ~/.fleet; the paths are only built as strings.
"""

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"
REAL_FLEET = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".fleet")


def run_hook(name, tool_input, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_RULES_OFF"}
    env.update(env_extra or {})
    event = {"tool_input": tool_input, "cwd": str(cwd)}
    out = subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(event),
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    if not out.stdout.strip():
        return None
    return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def bash(command, cwd, **kw):
    return run_hook("guard_bash.py", {"command": command}, cwd, **kw)


# --- human-only fleet commands -------------------------------------------------

@pytest.mark.parametrize("command", [
    "idea look into a UPS",
    "alert fleet-tools the sync is down",
    "dispatch --once",
    "dispatch --dry-run",
    "ship BD-4",
    "bin/idea something",
    "./bin/alert repo text",
    "~/.local/bin/dispatch --once",
    "FLEET_HOME=/tmp/x idea something",
    "cd somewhere && dispatch --once",
    "true; ship BD-1",
    "echo $(dispatch --dry-run)",
    "ticket close BD-1 --done",
    "ticket approve BD-2",
    "ticket sync",
    "bin/ticket sync --dry-run",
])
def test_human_only_commands_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


@pytest.mark.parametrize("command", [
    "ticket show BD-1",
    "ticket list",
    "ticket new --type build --repo r --branch task/x --title t --body b",
    "grep -rn idea README.md",
    "echo 'a good idea'",
    "python3 -m pytest -q tests/test_shortcuts.py",
    "scripts/check.sh",
    "git log --oneline -3",
    "cat docs/dispatcher-spec.md",
])
def test_normal_commands_allowed(tmp_path, command):
    assert bash(command, tmp_path) is None


# --- the .fleet folder in shell commands -----------------------------------------

@pytest.mark.parametrize("command", [
    "cat ~/.fleet/kaneo.json",
    "ls $HOME/.fleet/tickets",
    f"cat {REAL_FLEET}/kaneo-map.json",
    "rm ~/.fleet/tickets/BD-1.json",
    "mv x ~/.fleet/tickets/",
    "sed -i s/a/b/ ~/.fleet/tickets/BD-1.json",
    "echo hi > ~/.fleet/x",
    "python3 -c \"open('/home/someone/.fleet/kaneo.json').read()\"",
    "cd ~ && cat .fleet/kaneo.json",
    "FLEET_HOME=~/.fleet scripts/check.sh",
])
def test_fleet_folder_blocked_in_shell(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("command", [
    "FLEET_HOME=$(mktemp -d) scripts/check.sh",
    "HOME=$(mktemp -d) FLEET_HOME=$(mktemp -d) scripts/check.sh",
    "ls ~/projects/fleet-tools",
    "cat fleet/ticket.py",
    "echo my.fleetwood",
])
def test_fleet_lookalikes_allowed(tmp_path, command):
    assert bash(command, tmp_path) is None


# --- edits and reads -------------------------------------------------------------

@pytest.mark.parametrize("path", [
    f"{REAL_FLEET}/tickets/BD-1.json",
    f"{REAL_FLEET}/kaneo.json",
    "~/.fleet/dispatch.json",
])
def test_edits_into_fleet_blocked(tmp_path, path):
    reason = run_hook("guard_edits.py", {"file_path": path, "content": "x"}, tmp_path)
    assert reason and "~/.fleet" in reason


def test_codex_patch_into_fleet_blocked(tmp_path):
    patch = f"*** Begin Patch\n*** Update File: {REAL_FLEET}/kaneo.json\n+x\n*** End Patch\n"
    reason = run_hook("guard_edits.py", {"command": patch}, tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("tool_input", [
    {"file_path": f"{REAL_FLEET}/kaneo.json"},
    {"file_path": "~/.fleet/kaneo-map.json"},
    {"path": f"{REAL_FLEET}/tickets", "pattern": "BD"},
    {"pattern": "~/.fleet/**/*.json"},
    {"pattern": f"{REAL_FLEET}/*"},
])
def test_reads_of_fleet_blocked(tmp_path, tool_input):
    reason = run_hook("guard_reads.py", tool_input, tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("tool_input", [
    {"file_path": "fleet/ticket.py"},
    {"path": ".", "pattern": "fleet_home"},
    {"pattern": "tests/**/*.py"},
])
def test_normal_reads_allowed(tmp_path, tool_input):
    assert run_hook("guard_reads.py", tool_input, tmp_path) is None


def test_symlink_into_fleet_blocked(tmp_path):
    link = tmp_path / "sneaky"
    link.symlink_to(REAL_FLEET)  # creates only the link; the target isn't touched
    reason = run_hook("guard_reads.py", {"file_path": str(link / "kaneo.json")}, tmp_path)
    assert reason and "~/.fleet" in reason


def test_faked_home_does_not_dodge_the_rule(tmp_path):
    reason = run_hook("guard_reads.py", {"file_path": f"{REAL_FLEET}/kaneo.json"}, tmp_path,
                      env_extra={"HOME": str(tmp_path)})
    assert reason and "~/.fleet" in reason


def test_settings_wire_the_read_guard():
    settings = json.loads((HOOKS.parent / "settings.json").read_text())
    matchers = {entry["matcher"]: entry for entry in settings["hooks"]["PreToolUse"]}
    assert "Read|Grep|Glob" in matchers
    assert "guard_reads.py" in json.dumps(matchers["Read|Grep|Glob"])
