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
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
REAL_FLEET = os.path.join(REAL_HOME, ".fleet")


def run_hook(name, tool_input, cwd, env_extra=None, tool_name=None):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_RULES_OFF"}
    env.update(env_extra or {})
    event = {"tool_input": tool_input, "cwd": str(cwd)}
    if tool_name:
        event["tool_name"] = tool_name
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


@pytest.mark.parametrize("tool_name, tool_input", [
    ("Read", {"file_path": f"{REAL_FLEET}/kaneo.json"}),
    ("Read", {"file_path": "~/.fleet/kaneo-map.json"}),
    ("Grep", {"path": f"{REAL_FLEET}/tickets", "pattern": "BD"}),
    ("Glob", {"pattern": "~/.fleet/**/*.json"}),
    ("Glob", {"pattern": f"{REAL_FLEET}/*"}),
])
def test_reads_of_fleet_blocked(tmp_path, tool_name, tool_input):
    reason = run_hook("guard_reads.py", tool_input, tmp_path, tool_name=tool_name)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("tool_name, tool_input", [
    ("Read", {"file_path": "fleet/ticket.py"}),
    ("Grep", {"path": ".", "pattern": "fleet_home"}),
    ("Grep", {"pattern": "fleet_home"}),
    ("Glob", {"pattern": "tests/**/*.py"}),
    ("Glob", {"pattern": "**/*.py"}),
])
def test_normal_reads_allowed(tmp_path, tool_name, tool_input):
    assert run_hook("guard_reads.py", tool_input, tmp_path, tool_name=tool_name) is None


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


# --- regression cases from the Codex review --------------------------------------

@pytest.mark.parametrize("command", [
    "env dispatch --once",
    "env -i FLEET_HOME=/tmp/x dispatch --once",
    "command ship BD-4",
    "exec idea something",
    "nohup dispatch --once &",
    "nice -n 10 alert repo text",
    "timeout 5 dispatch --once",
    "sudo -u chris ship BD-1",
    "bash bin/ticket close BD-1 --done",
    "python3 bin/ticket sync",
    "bash -c 'dispatch --once'",
    "sh -c \"ticket approve BD-2\"",
    "'dispatch' --once",
    "\"ship\" BD-4",
    "echo `dispatch --dry-run`",
    "cd /tmp\ndispatch --once",
    "xargs -n1 idea < ideas.txt",
])
def test_wrapped_human_only_commands_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


@pytest.mark.parametrize("command", [
    "find ~ -name kaneo.json",
    f"find {REAL_HOME} -name '*.json'",
    "grep -r api_key ~",
    "grep -R key /",
    "rg key ~",
    "tar czf /tmp/x.tgz ~",
    "du -sh ~",
    "ls -R ~",
])
def test_tree_walks_over_home_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("command", [
    "find . -name '*.py'",
    "grep -rn idea fleet/",
    "rg fleet_home fleet tests",
    "ls ~",
    "ls -la ~/projects",
    "du -sh .",
])
def test_tree_walks_inside_project_allowed(tmp_path, command):
    assert bash(command, tmp_path) is None


def test_shell_symlink_into_fleet_blocked(tmp_path):
    link = tmp_path / "sneaky"
    link.symlink_to(REAL_FLEET)  # creates only the link; the target isn't touched
    reason = bash("cat sneaky/kaneo.json", tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("tool_name, tool_input, cwd", [
    ("Glob", {"pattern": ".fleet/**/*.json"}, REAL_HOME),
    ("Glob", {"pattern": "**/*.json", "path": REAL_HOME}, None),
    ("Glob", {"pattern": "**/kaneo.json"}, REAL_HOME),
    ("Grep", {"pattern": "api_key", "path": REAL_HOME}, None),
    ("Grep", {"pattern": "api_key", "path": "~"}, None),
    ("Grep", {"pattern": "api_key", "path": "/"}, None),
    ("Grep", {"pattern": "api_key"}, REAL_HOME),
])
def test_searches_reaching_fleet_blocked(tmp_path, tool_name, tool_input, cwd):
    reason = run_hook("guard_reads.py", tool_input, cwd or tmp_path, tool_name=tool_name)
    assert reason and "~/.fleet" in reason
