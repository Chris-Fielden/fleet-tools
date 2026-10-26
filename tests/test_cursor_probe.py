import os
import pty
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "cursor-probe.sh"

# A stand-in for the Cursor CLI: no network, no spend. It logs every call and
# prints a result event. STUB_VERSION sets what --version says.
FAKE_AGENT = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1" in
  --version) echo "${STUB_VERSION:-STUB 1.2.3}"; exit 0 ;;
  models) printf 'big-model - Big\\nmini-model - Mini\\n'; exit 0 ;;
  status) echo '{"loggedIn":true}'; exit 0 ;;
esac
case "$*" in *no-such-model-probe*) echo '{"type":"result","is_error":true}'; exit 1 ;; esac
echo '{"type":"result","subtype":"success","result":"ok","duration_ms":5}'
"""


@pytest.fixture
def stub(env):
    bindir = env / "bin"
    bindir.mkdir()
    agent = bindir / "agent"
    agent.write_text(FAKE_AGENT)
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    log = env / "stub.log"
    log.touch()
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "STUB_LOG": str(log),
            "PROBE_OUT_DIR": str(env / "out")}


def run_script(extra, tty=False):
    e = dict(os.environ)
    e.update(extra)
    if tty:
        master, slave = pty.openpty()
        try:
            return subprocess.run(["bash", str(SCRIPT)], stdin=slave, capture_output=True,
                                  text=True, env=e, timeout=60)
        finally:
            os.close(master)
            os.close(slave)
    return subprocess.run(["bash", str(SCRIPT)], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=e, timeout=60)


def test_runs_with_stub_version_and_no_terminal(stub, env):
    r = run_script(stub)
    assert r.returncode == 0, r.stderr
    report = next((env / "out").glob("probe-*.md")).read_text()
    for pid in ["P1", "P2", "P3", "P4", "P5a", "P5b", "P6", "P7", "P8", "P9",
                "P10", "P11", "P12a", "P12b", "P12c"]:
        assert f"| {pid} |" in report
    assert "STUB 1.2.3" in report
    assert "mini-model" in report          # cheapest model picked
    assert "duration_ms, result, subtype, type" in report
    assert "| P12c | bad model name | 1 |" in report


def test_refuses_without_terminal_or_stub(stub, env):
    r = run_script({**stub, "STUB_VERSION": "2026.01.01-real"})
    assert r.returncode == 3
    assert "not a terminal" in r.stderr
    assert not (env / "out").exists()
    # only the --version check ran: nothing else was spent
    assert (env / "stub.log").read_text().strip() == "--version"


def test_runs_on_a_terminal_with_a_real_looking_version(stub, env):
    r = run_script({**stub, "STUB_VERSION": "2026.01.01-real"}, tty=True)
    assert r.returncode == 0, r.stderr
    assert (env / "out" / "probe-2026.01.01-real.md").exists()


def test_probe_calls_are_safe(stub, env):
    run_script(stub)
    calls = (env / "stub.log").read_text().splitlines()
    probes = [c for c in calls if c.startswith("-p ")]
    assert len(probes) >= 12
    assert all("--model mini-model" in c for c in probes[:-1])
    banned = ("--approve-mcps", "worker", "acp", "persist", "--worktree")
    for c in calls:
        assert not any(b in c.split() for b in banned), c
    assert not any(str(ROOT) in c for c in calls)


def test_never_touches_real_home_or_fleet(stub, env):
    fleet = env / "fleet"
    run_script({**stub, "FLEET_HOME": str(fleet)})
    # the one thing it writes to the fleet store is the probe result file (BD-136)
    assert [p.name for p in fleet.rglob("*")] == ["cursor-probe-result.json"]


# --- BD-136: the review probes a-e and the result file ----------------------------------

import json  # noqa: E402

import fake_cursor  # noqa: E402

KEY = "fake-key-aaaa-bbbb"


@pytest.fixture
def rig(env):
    """A fake CLI with the key file in a fleet store whose folder is named like the real one,
    under a temporary home (so the agent guards, which protect that name, apply to it)."""
    home = env / "home"
    fleet = home / ("." + "fleet")
    (fleet / "secrets").mkdir(parents=True)
    kf = fleet / "secrets" / "cursor-api-key"
    kf.write_text(KEY + "\n")
    kf.chmod(0o600)
    bindir = env / "bin2"

    class Rig:
        pass
    r = Rig()
    r.bindir, r.fleet, r.keyfile, r.home = bindir, fleet, kf, home

    def go(**cfg):
        fake_cursor.install(bindir, **{"hook_model": "composer-2.5", "needs_key": True, **cfg})
        e = {"PATH": f"{bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "out"),
             "FLEET_HOME": str(fleet), "HOME": str(home), "PROBE_MODEL": "mini-model"}
        res = run_script(e)
        out = json.loads((fleet / "cursor-probe-result.json").read_text()) \
            if (fleet / "cursor-probe-result.json").exists() else None
        return res, out
    r.go = go
    return r


def test_all_probes_pass_and_result_file_is_written(rig):
    res, out = rig.go()
    assert res.returncode == 0, res.stderr
    assert out["probes"] == {k: "pass" for k in "abcde"}, out["why"]
    assert out["passed"] is True and out["cli_version"] == "STUB 1.2.3"
    text = (rig.fleet / "cursor-probe-result.json").read_text()
    assert KEY not in text and "@" not in text
    assert oct((rig.fleet / "cursor-probe-result.json").stat().st_mode & 0o777) == "0o600"
    # the key went only to the review-style runs, and each ran in an empty HOME
    ran = [c for c in fake_cursor.calls(rig.bindir) if "/rv-" in (c["home"] or "") and "--trust" in c["args"]]
    assert len(ran) == 4 and all(c["has_key"] and c["home_files"] == [] for c in ran)
    # the older probes (P1-P12) never get the key
    assert not [c for c in fake_cursor.calls(rig.bindir) if c["has_key"] and c not in ran]
    assert KEY not in json.dumps(fake_cursor.calls(rig.bindir))
    # the planted skill is removed again
    assert list((rig.home / ".claude" / "skills").glob("fleetprobe-*")) == []


@pytest.mark.parametrize("cfg,probe", [
    ({"ignore_hooks": True}, "a"),                 # shell and write got through
    ({"hook_model": "claude-4-sonnet"}, "b"),      # not a Cursor model
    ({"ask_writes": True}, "c"),                   # ask mode wrote a file
    ({"exit": 1}, "e"),                            # no auth in the empty HOME
    ({"skill_leak": True}, "e"),
])
def test_a_failing_probe_fails_the_result(rig, cfg, probe):
    _, out = rig.go(**cfg)
    assert out["probes"][probe] == "fail" and out["passed"] is False, out


def test_probe_d_needs_private_mode(rig):
    rig.keyfile.chmod(0o644)
    _, out = rig.go()
    assert out["probes"]["d"] == "fail" and "readable" in out["why"]["d"] and out["passed"] is False


def test_probe_d_needs_a_protected_folder(rig, env):
    other = env / "elsewhere"
    other.mkdir()
    (other / "k").write_text(KEY)
    (other / "k").chmod(0o600)
    fake_cursor.install(rig.bindir, hook_model="composer-2.5", needs_key=True)
    store = other / "store"
    res = run_script({"PATH": f"{rig.bindir}:{os.environ['PATH']}", "PROBE_OUT_DIR": str(env / "o"),
                      "FLEET_HOME": str(store), "HOME": str(rig.home), "CURSOR_KEY_FILE": str(other / "k")})
    out = json.loads((store / "cursor-probe-result.json").read_text())
    assert res.returncode == 0 and out["probes"]["d"] == "fail" and "protect" in out["why"]["d"]


def test_no_key_file_fails_every_probe_and_spends_nothing(rig):
    rig.keyfile.unlink()
    _, out = rig.go()
    assert out["probes"] == {k: "fail" for k in "abcde"} and out["passed"] is False
    assert not [c for c in fake_cursor.calls(rig.bindir) if "--trust" in c["args"] and c["has_key"]]
