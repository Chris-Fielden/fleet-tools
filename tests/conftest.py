import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "ticket"
AGENT_VARS = ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def _fake(path: Path, exit_code: int) -> Path:
    path.write_text(f"#!/bin/sh\ncat >/dev/null\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Temporary HOME and FLEET_HOME, a gitleaks that finds nothing, no agent
    vars. Deliberately not named `env`: a `parametrize("env", ...)` replaces a
    fixture of that name, which once let tests write to the real ~/.fleet."""
    for var in AGENT_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FLEET_HOME", str(tmp_path / "fleet"))
    # Never reach a real herdr: tests that need one point this at the fake.
    monkeypatch.setenv("FLEET_HERDR", str(tmp_path / "no-such-herdr"))
    monkeypatch.setenv("FLEET_GITLEAKS", str(_fake(tmp_path / "gl-clean", 0)))


@pytest.fixture
def env(tmp_path):
    """The temporary folder everything above lives in."""
    return tmp_path


@pytest.fixture
def leaky_gitleaks(env, monkeypatch):
    monkeypatch.setenv("FLEET_GITLEAKS", str(_fake(env / "gl-leak", 1)))


def run(*args, extra_env=None):
    """Run bin/ticket as a subprocess with the current (test) environment."""
    e = dict(os.environ)
    e.update(extra_env or {})
    return subprocess.run([sys.executable, str(BIN), *args], capture_output=True, text=True, env=e)


def new_args(**over):
    f = {"type": "build", "repo": "fleet-tools", "branch": "task/demo",
         "title": "Do a thing", "body": "Details."}
    f.update(over)
    out = []
    for k, v in f.items():
        if v is not None:
            out += [f"--{k}", v]
    return out
