"""Tests for the rules-kit hooks that keep agents out of the fleet's private
folder (~/.fleet) and away from the human-only fleet commands.

Each test runs a hook exactly as Claude Code would: as a separate program,
with the tool call as JSON on standard input. Nothing here reads or writes the
real ~/.fleet; the paths are only built as strings.
"""

import shlex
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
    hook_output = json.loads(out.stdout)["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny"
    assert hook_output["hookEventName"] == "PreToolUse"
    return hook_output["permissionDecisionReason"]


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
    "ticket priority BD-1 urgent",
    "env -u CLAUDECODE bin/ticket priority BD-1 urgent",
    "fleet ticket priority BD-1 urgent",
    "bin/ticket sync --dry-run",
    "fleet ship BD-4",
    "fleet approve BD-2",
    "fleet ticket approve BD-2",
    "fleet ticket close BD-1 --done",
    "fleet ticket sync",
    "fleet ticket risk BD-1 --raise high",
    "fleet alert repo the sync is down",
    "fleet idea look into a UPS",
    "fleet dispatch --once",
    "fleet start",
    "fleet stop",
    "fleet restart",
    "fleet autostart on",
    "fleet launcher on",
    "fleet ops",
    "~/.local/bin/fleet ship BD-1",
    "python3 bin/fleet ship BD-1",
    "env FLEET_HOME=/tmp/x fleet ship BD-1",
    "true; fleet ship BD-1",
    "echo $(fleet approve BD-1)",
    "bash -c 'fleet ticket close BD-1'",
    "sudo fleet start",
    "'fleet' ship BD-1",
])
def test_human_only_commands_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


@pytest.mark.parametrize("command", [
    "ticket show BD-1",
    "ticket list",
    "fleet help",
    "fleet status",
    "fleet logs -n 5",
    "fleet usage",
    "fleet doctor",
    "fleet ticket show BD-1",
    "fleet ticket list",
    "fleet ticket note BD-1 done",
    "fleet ticket new --type build --repo r --branch task/x --title t --body b",
    "echo fleet ship is human only",
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


# --- second review round -----------------------------------------------------------

def test_bare_symlink_to_a_fleet_file_blocked(tmp_path):
    (tmp_path / "sneaky").symlink_to(os.path.join(REAL_FLEET, "kaneo.json"))
    reason = bash("cat sneaky", tmp_path)
    assert reason and "~/.fleet" in reason


@pytest.mark.parametrize("command", [
    'grep -R key "$HOME"',
    "grep -R key ${HOME}",
    "cd ~ && grep -R key .",
    "cd $HOME; find . -name '*.json'",
    'cd "$SOMEWHERE" && rg key',
    'grep -r key "$DIR"',
])
def test_home_searches_via_variables_and_cd_blocked(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and ("~/.fleet" in reason or "Only the human" in reason)


@pytest.mark.parametrize("command", [
    "bash -lc 'env dispatch --once'",
    "bash -ec 'ship BD-1'",
    "sh -xc \"ticket sync\"",
])
def test_combined_shell_flags_scanned(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


def test_multiline_quoted_string_still_scanned(tmp_path):
    reason = bash("git commit -m 'line one\nline two' && dispatch --once", tmp_path)
    assert reason and "Only the human runs" in reason
    assert bash("git commit -m 'line one\nline two'", tmp_path) is None


def test_cd_into_fleet_blocked(tmp_path):
    reason = bash("cd ~/.fleet && ls", tmp_path)
    assert reason and "~/.fleet" in reason


def test_project_local_dot_fleet_folder_allowed(tmp_path):
    (tmp_path / ".fleet").mkdir()
    assert run_hook("guard_reads.py", {"pattern": ".fleet/*.json"}, tmp_path, tool_name="Glob") is None


@pytest.mark.parametrize("command", [
    "cd fleet && grep -rn idea .",
    "cd tests; ls -R .",
    "grep -r fleet_home fleet tests",
])
def test_cd_inside_project_allowed(tmp_path, command):
    (tmp_path / "fleet").mkdir()
    (tmp_path / "tests").mkdir()
    assert bash(command, tmp_path) is None


# --- third review round ------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "if command ship BD-4; then true; fi",
    "while dispatch --once; do sleep 60; done",
    "! ticket sync",
    "{ idea something; }",
    "eval 'dispatch --once'",
    "eval dispatch --once",
])
def test_keywords_and_eval_do_not_hide_commands(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


@pytest.mark.parametrize("command", [
    "cd ~ && (cd /tmp); rg --hidden key",
    "cd ~; (cd /tmp && true); grep -r key .",
])
def test_subshell_cd_does_not_hide_home_search(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "~/.fleet" in reason


def test_plain_if_and_loops_still_allowed(tmp_path):
    assert bash("if test -f README.md; then cat README.md; fi", tmp_path) is None
    assert bash("for f in tests/*.py; do python3 -m py_compile $f; done", tmp_path) is None


# --- fourth review round -----------------------------------------------------------

@pytest.mark.parametrize("command", [
    'echo "$(command ship BD-4)"',
    'echo "`dispatch --once`"',
    'echo "result: $(env idea x) done"',
    'x="$(echo "$(ticket sync)")"',
])
def test_quoted_substitutions_are_scanned(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


def test_single_quoted_substitution_is_just_text(tmp_path):
    assert bash("echo '$(date)' \"$(git rev-parse HEAD)\"", tmp_path) is None


@pytest.mark.parametrize("command", [
    "cd ~ && (cd /tmp); bash -c 'rg --hidden key'",
    "cd ~ && (cd /tmp); echo \"$(grep -r key .)\"",
])
def test_nested_scans_get_every_folder(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "~/.fleet" in reason


def test_many_cds_stay_fast_and_bounded(tmp_path):
    import time
    start = time.time()
    assert bash("cd . && " * 60 + "ls", tmp_path) is None
    assert time.time() - start < 5
    # Too many possible folders to track: risky commands are refused.
    dirs = " && ".join(f"(cd /tmp/d{i})" for i in range(20))
    reason = bash(dirs + " && cd /tmp/x && rg key", tmp_path)
    assert reason and "~/.fleet" in reason


# --- fifth review round ------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "> /tmp/ship.log ship BD-4",
    "2>/dev/null dispatch --once",
    "< /dev/null idea something",
    "echo hi; >>/tmp/x.log ticket sync",
])
def test_leading_redirections_do_not_hide_commands(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "Only the human runs" in reason


def test_redirect_into_fleet_still_blocked(tmp_path):
    reason = bash("echo x 2> ~/.fleet/x", tmp_path)
    assert reason and "~/.fleet" in reason


def test_ordinary_redirections_allowed(tmp_path):
    assert bash("python3 -m pytest -q > out.txt 2>&1", tmp_path) is None
    assert bash("sort < data.txt > sorted.txt", tmp_path) is None


@pytest.mark.parametrize("command", [
    "env -C ~ rg --hidden key .",
    "env --chdir=$HOME grep -r key .",
    "sudo -D ~ find . -name '*.json'",
    "tar -C ~ -cf /tmp/all.tar .",
    "env -C $DIR rg key",
])
def test_wrapper_folder_changes_are_followed(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "~/.fleet" in reason


def test_wrapper_folder_change_inside_project_allowed(tmp_path):
    (tmp_path / "tests").mkdir()
    assert bash("env -C tests rg key .", tmp_path) is None


def test_too_deep_nesting_fails_closed(tmp_path):
    cmd = "rg key ~"
    for _ in range(8):
        cmd = "bash -c " + shlex.quote(cmd)
    reason = bash(cmd, tmp_path)
    assert reason and "too unusual" in reason


@pytest.mark.parametrize("command", [
    "cd -- ~ && rg --hidden key .",
    "cd -P ~ && rg --hidden key .",
    "cd -L -- ~ && grep -r key .",
])
def test_cd_options_do_not_hide_destination(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason and "private folder" in reason


def test_redirect_checked_before_nested_shell_and_cd(tmp_path):
    (tmp_path / "sneaky").symlink_to(REAL_FLEET)
    for command in ("bash -c 'true' > sneaky", "cd . > sneaky", "sh -c true 2> sneaky"):
        reason = bash(command, tmp_path)
        assert reason and "private folder" in reason, command
    assert bash("bash -c 'true' > out.txt", tmp_path) is None


@pytest.mark.parametrize("command", [
    "cd - && rg key .",
    "pushd +1 && rg key .",
    "pushd -1 && rg key .",
])
def test_unknown_cd_destination_blocks_tree_search(tmp_path, command):
    reason = bash(command, tmp_path)
    assert reason  # denied: the search could start anywhere
