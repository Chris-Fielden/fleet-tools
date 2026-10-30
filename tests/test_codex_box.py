"""BD-148: Codex runs in the box (scripts/cursor-gauntlet.sh, scripts/cursor-stress.sh --box-run).

Fakes only: a fake `bwrap`, a fake Codex and Claude, no real model. Every test has its own temporary FLEET_HOME and HOME."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_cursor_gauntlet import AUTH, GOOD, KEY, SCORE, OTHER, rig, verdicts  # noqa: F401  (rig is a fixture)
from test_cursor_stress import FAKE_BWRAP, make_exe

ROOT = Path(__file__).resolve().parent.parent
STRESS = ROOT / "scripts" / "cursor-stress.sh"
TOKENS = [v for v in AUTH["tokens"].values()]
ROTATED = dict(AUTH, tokens=dict(AUTH["tokens"], access_token="fake-ROTATED-access-5555555555", refresh_token="fake-ROTATED-refresh-6666666666"),
               last_refresh="2026-10-30T02:00:00Z")


def codex_calls(rig):
    return [c for c in rig.log("codex") if "exec" in c["args"]]


def real_auth(rig):
    return rig.home / ".codex" / "auth.json"


# (1) --box-run needs no Cursor CLI

def test_box_run_works_with_no_cursor_cli_installed(rig, tmp_path):
    prog = make_exe(tmp_path / "prog", "#!/bin/sh\necho ran-in-box\n")
    work = tmp_path / "w"
    (work / "cwd").mkdir(parents=True)
    (work / "home").mkdir()
    e = dict(os.environ, PATH=f"{rig.boxbin}:{os.environ['PATH']}", STRESS_BOXES="bwrap", STRESS_AGENT="no-such-cursor-cli", FLEET_HOME=str(rig.fleet))
    res = subprocess.run(["bash", str(STRESS), "--box-run", "0", str(work / "cwd"), str(work / "home"), "30", str(prog)],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, env=e, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "ran-in-box" in res.stdout and "not found on PATH" not in res.stderr


def test_the_program_folder_is_bound_read_only_and_a_script_interpreter_with_it(rig, tmp_path):
    inst = tmp_path / "inst" / "bin"
    inst.mkdir(parents=True)
    node = make_exe(inst / "fakenode", "#!/bin/sh\necho interp\n")
    script = tmp_path / "scripts" / "tool"
    script.parent.mkdir()
    make_exe(script, f"#!{node}\n")
    work = tmp_path / "w"
    (work / "cwd").mkdir(parents=True)
    (work / "home").mkdir()
    e = dict(os.environ, PATH=f"{rig.boxbin}:{os.environ['PATH']}", STRESS_BOXES="bwrap", STRESS_AGENT="no-such-cursor-cli")
    res = subprocess.run(["bash", str(STRESS), "--box-run", "0", str(work / "cwd"), str(work / "home"), "30", str(script)],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, env=e, timeout=60)
    assert res.returncode == 0, res.stderr
    log = [json.loads(x) for x in (rig.boxbin / "bwrap.log").read_text().splitlines()][-1]["box"]
    ro = [log[i + 1] for i, a in enumerate(log) if a == "--ro-bind"]
    assert str(script.parent) in ro and str(inst.parent) in ro


# (2) what the fake Codex sees in the box

def test_codex_sees_an_empty_home_with_only_its_login_and_the_copy(rig):
    res = rig.go(models="codex")
    assert res.returncode == 0, res.stderr
    first = codex_calls(rig)[0]
    assert first["home_files"] == [".codex/auth.json"]
    assert first["auth_mode"] == "0o600" and json.loads(first["auth_seen"]) == AUTH
    assert first["home"] != str(rig.home) and first["cwd"].endswith("/box/repo") and first["home"].endswith("/box/home")
    assert first["has_pkg"] and "-s" in first["args"] and "read-only" in first["args"] and "--json" in first["args"]
    assert "CURSOR_API_KEY" not in first["env_keys"] and "FLEET_HOME" not in first["env_keys"]
    assert verdicts(rig.run_dir())["codex"] == "GO"


def test_codex_runs_with_no_cursor_cli_installed(rig):
    res = rig.go(models="codex", extra_env={"GAUNTLET_AGENT": "no-such-cursor-cli"})
    assert res.returncode == 0, res.stderr
    assert verdicts(rig.run_dir())["codex"] == "GO"


def test_the_box_home_is_deleted_afterwards(rig):
    rig.go(models="codex")
    assert not any(c["home"] and os.path.exists(c["home"]) for c in codex_calls(rig))


# (3) find .. inside the box scores normally; (4) the real home, fleet-tools, .fleet, the answer key do not

def test_a_trace_that_looks_above_the_copy_inside_the_box_scores_normally(rig):
    res = rig.go(models="codex", codex={"reads": ["ledgerline/gateway.py", ".."]})
    assert res.returncode == 0, res.stderr
    run = rig.run_dir()
    assert verdicts(run)["codex"] == "GO"
    assert json.loads((run / "codex-1.json").read_text())["invalid"] == []


@pytest.mark.parametrize("target", [
    "{home}/.agents/skills/find-bugs/SKILL.md",
    "~/.agents/skills/x",
    "/work/fleet-tools/CLAUDE.md",
    "~/." + "fleet/x",
    "tests/fixtures/gauntlet-key/answer-key.json",
    "../..",
    "{home}",
])
def test_a_boxed_trace_naming_the_real_home_or_a_protected_name_is_invalid_with_the_line_as_proof(rig, target):
    target = target.replace("{home}", str(rig.home))
    res = rig.go(models="codex", codex={"reads": ["ledgerline/gateway.py", target]})
    assert res.returncode == 0, res.stderr
    run = rig.run_dir()
    assert verdicts(run)["codex"].startswith("INVALID, no verdict (our side:")
    invalid = json.loads((run / "codex-1.json").read_text())["invalid"]
    assert invalid[0]["side"] == "ours" and "baseline left its copy" in invalid[0]["reason"] and target in invalid[0]["proof"]
    assert not (run / "codex-1.md").exists()


def test_the_old_rule_still_holds_for_a_run_not_in_the_box(rig):
    rig.go(models="claude-opus", claude={"reads": ["ledgerline/gateway.py", ".."]})
    run = rig.run_dir()
    assert verdicts(run)["claude-opus"].startswith("INVALID") and "baseline left its copy" in verdicts(run)["claude-opus"]


# (5) the login written back

def test_a_login_refreshed_in_the_box_is_written_back_atomically_mode_600(rig):
    real_auth(rig).chmod(0o644)
    res = rig.go(models="codex", codex={"rotate": json.dumps(ROTATED)})
    assert res.returncode == 0, res.stderr
    assert json.loads(real_auth(rig).read_text()) == ROTATED
    assert stat.S_IMODE(real_auth(rig).stat().st_mode) == 0o600
    assert not [p for p in real_auth(rig).parent.iterdir() if p.name.endswith(".tmp")]
    assert "written back" in res.stdout


def test_an_unchanged_login_leaves_the_real_file_untouched(rig):
    before = real_auth(rig).stat()
    text = real_auth(rig).read_text()
    os.utime(real_auth(rig), (1_000_000_000, 1_000_000_000))
    before = real_auth(rig).stat()
    res = rig.go(models="codex")
    assert res.returncode == 0, res.stderr
    after = real_auth(rig).stat()
    assert real_auth(rig).read_text() == text and after.st_mtime_ns == before.st_mtime_ns and after.st_ino == before.st_ino


def test_a_real_login_that_moved_on_and_is_newer_is_not_overwritten(rig, tmp_path):
    newer = dict(AUTH, tokens=dict(AUTH["tokens"], refresh_token="fake-NEWER-refresh-777777777"), last_refresh="2026-10-30T09:00:00Z")
    snap, box = tmp_path / "snap", tmp_path / "box-auth"
    snap.write_text(json.dumps(AUTH))
    box.write_text(json.dumps(ROTATED))                      # the box refreshed at 02:00
    real_auth(rig).write_text(json.dumps(newer))             # the human's own Codex refreshed at 09:00
    out = subprocess.run([sys.executable, "-I", str(SCORE), "auth-back", str(snap), str(box), str(real_auth(rig))],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "kept" and json.loads(real_auth(rig).read_text()) == newer


def test_a_broken_box_login_never_replaces_the_real_one(rig):
    text = real_auth(rig).read_text()
    res = rig.go(models="codex", codex={"rotate": "not json"})
    assert res.returncode == 0, res.stderr
    assert real_auth(rig).read_text() == text


# (6) a planted token is in no saved file

def test_no_login_token_is_in_any_saved_file_and_a_leaking_file_is_removed(rig):
    res = rig.go(models="codex", codex={"rotate": json.dumps(ROTATED), "plant": [
        TOKENS[1], "see " + ROTATED["tokens"]["access_token"], "and " + TOKENS[2]]})
    assert res.returncode == 0, res.stderr
    run = rig.run_dir()
    secrets = TOKENS + [ROTATED["tokens"]["access_token"], ROTATED["tokens"]["refresh_token"]]
    for f in run.rglob("*"):
        if f.is_file():
            t = f.read_text(errors="replace")
            assert not any(s in t for s in secrets), f
    assert "[token removed]" in (run / "codex-1.trace.jsonl").read_text()
    assert KEY not in res.stdout + res.stderr and not any(s in res.stdout + res.stderr for s in secrets)


def test_the_scrubber_removes_every_token_value_of_a_login_file(tmp_path):
    secrets = tmp_path / "secrets"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(AUTH))
    subprocess.run([sys.executable, "-I", str(SCORE), "secrets-add", str(auth), str(secrets)], check=True)
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert "2026-10-30T01:00:00Z" not in secrets.read_text()           # a timestamp is no secret
    out = subprocess.run([sys.executable, "-I", str(SCORE), "scrub"], input="a " + " b ".join(TOKENS) + " c", capture_output=True,
                         text=True, env=dict(os.environ, PSECRETS_FILE=str(secrets)))
    assert not any(t in out.stdout for t in TOKENS) and out.stdout.count("[token removed]") == len(TOKENS)


# (7) Codex's own sandbox cannot start: the preflight fails and nothing is spent

def test_codex_sandbox_start_failure_fails_the_preflight_and_spends_nothing(rig):
    res = rig.go(models="codex", codex={"sandbox_fail": True})
    assert res.returncode == 8
    assert "PREFLIGHT FAILED" in res.stderr and "sandbox" in res.stderr
    assert codex_calls(rig) == [] and (not rig.out.exists() or not [p for p in rig.out.iterdir() if p.is_dir()])


def test_the_preflight_says_which_self_test_ran(rig):
    res = rig.go(models="codex")
    assert res.returncode == 0, res.stderr
    pre = (rig.run_dir() / "preflight.txt").read_text()
    assert "runs Codex (fake-codex 1.0)" in pre and "(codex sandbox)" in pre


def test_a_missing_codex_login_file_fails_the_preflight(rig):
    real_auth(rig).unlink()
    res = rig.go(models="codex")
    assert res.returncode == 8 and "Codex login file" in res.stderr and codex_calls(rig) == []


# (8) --replace for N = 1, 2, 3

@pytest.mark.parametrize("n", [1, 2, 3])
def test_a_replacement_of_each_of_the_three_codex_attempts_is_allowed(rig, n):
    rig.go(models="codex", codex={"reads": ["../.."]} )
    run = rig.run_dir()
    # make it look like the first run did: attempt n failed on our side and has its record
    meta = json.loads((run / "codex-1.json").read_text())
    if n != 1:
        (run / f"codex-{n}.json").write_text(json.dumps(dict(meta, n=n)))
        (run / f"codex-{n}.trace.jsonl").write_text((run / "codex-1.trace.jsonl").read_text())
    (rig.others / "codex.json").write_text(json.dumps({"review_seq": [GOOD]}))
    res = rig.go(models="codex", args=("--replace", str(run), "codex", str(n)))
    assert res.returncode == 0, res.stderr
    assert json.loads((run / f"codex-{n}.json").read_text())["invalid"] == []
    assert (run / f"codex-{n}.replaced").exists()


# (9) the Claude baselines are unchanged

def test_claude_still_runs_outside_the_box_with_its_own_home(rig):
    res = rig.go(models="claude-opus")
    assert res.returncode == 0, res.stderr
    c = rig.log("claude")[0]
    assert c["home"] == str(rig.home) and c["cwd"].endswith("/claude-opus-1/repo") and "/box/" not in c["cwd"]
    assert verdicts(rig.run_dir())["claude-opus"] == "GO"
    assert not [x for x in (rig.boxbin / "bwrap.log").read_text().splitlines() if "claude" in x] if (rig.boxbin / "bwrap.log").exists() else True
