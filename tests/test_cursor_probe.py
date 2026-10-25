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
    assert not fleet.exists()
