"""Agents must not launch Cursor (EM-35): guard_bash denies agent, cursor-agent
and `cursor agent`, and Cursor's bypass flags, without touching lookalikes."""

import shlex

import pytest

from test_guard_hooks import bash

CURSOR_MSG = "Agents may not launch Cursor"


@pytest.mark.parametrize("command", [
    "agent -p hi", "agent", "/home/u/.local/bin/agent -p x", "~/.local/bin/cursor-agent -p x",
    "cursor-agent", "cursor agent -p x", "'agent' -p x", "true; agent -p x", "true && agent",
    "false || agent", "echo hi | agent", "sleep 1 & agent", "true\nagent -p x",
    "if agent -p x; then true; fi", "(agent -p x)", "echo $(agent -p x)", "echo `agent -p x`",
    "FOO=1 agent", "env agent", "env -i VAR=1 agent", "command agent", "exec agent",
    "nohup agent", "time agent", "setsid agent", "sudo -u me agent", "timeout 60 agent",
    "xargs agent", "nice -n 5 agent", "env nohup timeout 5 agent",
    "bash -c 'agent -p x'", "sh -c 'agent -p x'", "eval agent -p x", "cd /tmp && agent -p x",
    "agent -f", "agent --force", "agent -pf", "agent -fp", "cursor-agent --force -p x",
    "cursor agent -f", "stdbuf -o L agent -p hi", "stdbuf --output L agent", "ionice -t agent", "sudo -n agent", "timeout -- 60 agent", "env -- agent", "sudo -- agent", "env -iu FOO agent", "timeout -vs TERM 60 agent", "timeout -sTERM 60 agent",
    "timeout --signal=TERM 60 agent", "timeout --foreground 60 agent", "timeout -v 60 agent", "xargs -rn 1 agent", "sudo -nu root agent", "sudo -nuroot agent", "nice --adjustment 5 agent", "sudo -E -H agent", "sudo -i agent", "xargs -r agent", "stdbuf -i0 -oL agent", "exec -a NAME agent",
    "sudo -p prompt agent", "xargs -a list agent",
    "env -S 'agent -p hi'", "env --split-string 'agent -p hi'", "env --split-string=agent",
    "env -Sagent", "env -S 'bash -c' 'exec agent -p hi'",
    "env --split-string='bash -c' 'exec agent -p hi'", "env -i -S 'agent x'", "env -iS 'agent -p hi'", "env -iSagent", "env -uSHELL agent", "env -CSdir agent", "env -i0vS agent",
    "env -S '-i agent -p hi'", "env -S 'agent\\_-p\\_hi'", "env -S '-u FOO\\_agent'","env --unset FOO agent -p hi", "env -u FOO agent", "sudo --user me agent",
    "cursor --user-data-dir /tmp/x agent -p hi","timeout -s TERM 60 agent", "timeout --signal TERM 60 agent",
    "timeout -k 5 60 agent", "timeout --kill-after 5 60 agent", "AGENT_RULES_OFF=1 agent -p x",
])
def test_cursor_launch_blocked(tmp_path, command):
    assert CURSOR_MSG in bash(command, tmp_path)


@pytest.mark.parametrize("command", [
    "agent --sandbox disabled", "agent --sandbox=disabled", "agent --approve-mcps",
])
def test_cursor_bypass_flags_blocked(tmp_path, command):
    assert "safety sandbox" in bash(command, tmp_path)


def test_env_split_string_unmodelled_escape_refused(tmp_path):
    assert "too unusual" in bash("env -S '-u FOO\\c ignored' agent", tmp_path)


@pytest.mark.parametrize("command", [
    "env --split 'agent -p hi'", "env --split=agent", "env --uns FOO agent",
    "xargs --max-args 1 agent", "/usr/bin/time --format %E agent",
    "timeout --sig TERM 60 agent", "env -a NAME agent", "env --ignore-signal agent", "env --default-signal=INT agent", "env -S '# ignored' agent -p hi",
])
def test_env_unmodelled_options_refused(tmp_path, command):
    assert "too unusual" in bash(command, tmp_path)


def test_deeply_nested_agent_blocked(tmp_path):
    cmd = "agent"
    for _ in range(8):
        cmd = "bash -c " + shlex.quote(cmd)
    assert bash(cmd, tmp_path)


@pytest.mark.parametrize("command", [
    "ssh-agent -s", "gpg-agent --version", "grep agent file", "grep -r agent .", "echo agent", "timeout -- 60 true", "env -- true", "env -iu FOO true", "cat agent.md",
    "ls agents/", "git log --grep=agent", "git branch -f x", "rm -f x", "ln -f a b",
    "cursor .", "cursor --version", "claude -p x", "codex exec x",
    "scripts/ask-codex.sh q", "scripts/ask-claude.sh q", "scripts/review.sh",
    'ticket note ID-1 "agent is slow"', 'git commit -m "block agent launch"',
    "python3 agent.py", 'ticket new --title "Block agents"',
])
def test_cursor_lookalikes_allowed(tmp_path, command):
    assert bash(command, tmp_path) is None


def test_unbalanced_quote_with_agent_fails_closed(tmp_path):
    assert bash('agent -p "x', tmp_path)
    assert bash('agent\necho "', tmp_path)
