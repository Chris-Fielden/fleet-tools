"""The agents' Bash allow rules (fleet/permissions.py, .claude/settings.json)."""

import json
import re
from pathlib import Path

import pytest

from fleet import permissions as pm
from fleet.launch import guard_hook_settings

from test_guard_hooks import bash

REPO = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((REPO / ".claude" / "settings.json").read_text())
SEPARATORS = re.compile(r"&&|\|\||;|\|&|\||&|\n")

HUMAN_ONLY = [
    "ship EM-1", "idea something", "alert repo text", "dispatch --once",
    "ticket close EM-1 --done", "ticket approve EM-1", "ticket sync", "ticket priority EM-1 high",
    "ticket fix-titles", "fleet ship EM-1", "fleet ticket close EM-1", "git push", "git push origin main",
    "scripts/finish-" + "task.sh x",
    "env ship EM-1", "timeout 5 ship EM-1", "bash -c 'ship EM-1'", "(ship EM-1)", "xargs ship",
    "/usr/local/bin/ship EM-1", "bash bin/ticket close EM-1",
]


def rules(items):
    out = []
    for r in items:
        m = re.fullmatch(r"Bash\((.*)\)", r, re.S)
        out.append(m.group(1) if m else None)
    return out


def matches(pattern, command):
    regex = ".*".join(re.escape(p) for p in pattern.split("*"))
    return re.fullmatch(regex, command, re.S) is not None


def approx_covered(allow, command):
    """APPROXIMATION of Claude Code's rule (every subcommand must match an allow rule).
    It splits on every separator, even inside quotes, and does not parse $(...) or backticks,
    so it only shows the rules do not trivially cover human-only commands, not real behaviour.
    It also ignores word boundaries on `*`, quoting and redirections; do not treat it as proof."""
    parts = [p.strip() for p in SEPARATORS.split(command) if p.strip()]
    return bool(parts) and all(any(matches(r, p) for r in rules(allow)) for p in parts)


def test_settings_equal_the_single_source():
    assert SETTINGS["permissions"] == {"allow": pm.ALLOW, "deny": pm.DENY}


def test_every_rule_is_narrow_bash():
    for r in pm.ALLOW + pm.RESEARCH_ALLOW:
        inner = rules([r])[0]
        assert inner, r
        assert inner not in ("*", "") and not inner.startswith("*")
        assert not re.match(r"(?:\S*/)?(python|node|bash|sh|env|npm|npx|pip|uv|yarn|pnpm)\d*\b", inner), r
        assert not r.startswith(("Agent", "Monitor"))
    assert set(pm.RESEARCH_ALLOW) <= set(pm.ALLOW)
    assert "Bash(git push *)" in SETTINGS["permissions"]["deny"]
    for flag in ("--no-verify", "--amend"):
        assert any(flag in d for d in SETTINGS["permissions"]["deny"]), flag


@pytest.mark.parametrize("command", HUMAN_ONLY)
def test_no_allow_rule_covers_a_human_only_command(command):
    assert not approx_covered(pm.ALLOW, command)
    assert not approx_covered(pm.RESEARCH_ALLOW, command)


@pytest.mark.parametrize("command", [
    "ship EM-1", "idea x", "alert r t", "dispatch --once", "ticket close EM-1", "ticket approve EM-1",
    "ticket sync", "ticket priority EM-1 high", "ticket fix-titles", "fleet ship EM-1",
    "env ship EM-1", "timeout 5 ship EM-1", "bash -c 'ship EM-1'", "(ship EM-1)",
    "/usr/local/bin/ship EM-1", "bash bin/ticket close EM-1", "xargs ship",
    "ticket note EM-1 hi; ship EM-1", "ticket show EM-1 && ticket close EM-1 --done",
])
def test_guard_still_blocks(command, tmp_path):
    assert bash(command, tmp_path)


def test_compound_with_human_only_command_is_not_covered():
    assert approx_covered(pm.ALLOW, "ticket note EM-1 done")
    assert not approx_covered(pm.ALLOW, "ticket note EM-1 done; ship EM-1")
    assert not approx_covered(pm.ALLOW, "ticket note EM-1 done\nticket close EM-1")
    assert not approx_covered(pm.ALLOW, "scripts/check.sh && git push")


def test_approximation_limits_are_known(tmp_path):
    # A quoted separator is split too (real Claude Code may not split it): the helper errs
    # towards "not covered".
    assert not approx_covered(pm.ALLOW, 'ticket note EM-1 "a; b"')
    # Substitutions are not parsed, so the helper cannot judge a hostile one hidden in an
    # allowed-looking command. The guard, not these rules, is what stops it.
    hostile = "ticket note EM-1 $(sh" + "ip EM-1)"
    assert approx_covered(pm.ALLOW, hostile)
    assert bash(hostile, tmp_path)


def test_research_hooks_mirror_repo_matchers():
    got = guard_hook_settings()["hooks"]["PreToolUse"]
    mine = SETTINGS["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in got] == [g["matcher"] for g in mine]
    assert set(SETTINGS) == {"hooks", "statusLine", "permissions"}


def test_research_folder_settings_carry_no_permissions_block():
    """Allow rules in a project file wait for workspace trust and raise a dialog (EM-25):
    the folder file has hooks and statusLine only; the rules go on the launch command."""
    got = guard_hook_settings()
    assert "PreToolUse" in got["hooks"] and "statusLine" in got
    assert set(got) == {"hooks", "statusLine"}


def test_research_rules_file_is_exactly_the_em22_rules(tmp_path, monkeypatch):
    from fleet.launch import research_rules_file
    monkeypatch.setenv("FLEET_HOME", str(tmp_path / "fleet"))
    path = Path(research_rules_file())
    assert path.parent == tmp_path / "fleet" and path.name == "research-settings.json"
    data = json.loads(path.read_text())
    assert data == {"permissions": {"allow": pm.RESEARCH_ALLOW, "deny": pm.DENY}}
    assert pm.RESEARCH_ALLOW == ["Bash(ticket note *)", "Bash(ticket show *)"]
    assert pm.DENY == ["Bash(git push *)", "Bash(git push)"]
