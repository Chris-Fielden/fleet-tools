"""Guards against tests writing to the real ~/.fleet (this once happened:
a parametrize("env", ...) replaced the isolation fixture)."""
import os
from pathlib import Path

import pytest

from fleet import dispatch as dp
from fleet import kaneo
from fleet import ticket as tk

REAL_HOME = tk._real_home()


def assert_temporary(path: Path):
    path = Path(path).resolve()
    assert not path.is_relative_to((REAL_HOME / ".fleet").resolve()), path
    assert path.is_relative_to(Path(os.environ["HOME"]).resolve().parent), path


# A parameter named `env` replaces a fixture named `env`; isolation must not
# depend on that name. Before the fix this failed: FLEET_HOME was unset.
@pytest.mark.parametrize("env", [{}, {"CLAUDECODE": "1"}])
def test_isolation_survives_a_parameter_named_env(env):
    assert "FLEET_HOME" in os.environ
    assert_temporary(tk.fleet_home())
    assert_temporary(os.environ["HOME"])
    assert Path.home() != REAL_HOME
    assert not any(v in os.environ for v in ("CLAUDECODE", "CODEX_SANDBOX"))


def test_every_test_gets_a_temporary_home_and_fleet_home():
    assert_temporary(tk.fleet_home())
    assert_temporary(Path.home())


@pytest.fixture
def pretend_real_home(tmp_path, monkeypatch):
    """A stand-in 'real' home, so the tripwire can be tried without any risk
    to the actual ~/.fleet."""
    fake = tmp_path / "realhome"
    (fake / ".fleet").mkdir(parents=True)
    monkeypatch.setattr(tk, "_real_home", lambda: fake)
    monkeypatch.setenv("FLEET_HOME", str(fake / ".fleet"))
    return fake / ".fleet"


GOOD = {"type": "build", "repo": "r", "branch": "task/x", "title": "t", "body": "b"}


def test_tripwire_refuses_to_create_a_ticket_in_the_real_store(pretend_real_home):
    with pytest.raises(tk.TicketError, match="refusing to write"):
        tk.create_ticket(dict(GOOD))
    assert list(pretend_real_home.iterdir()) == []


def test_tripwire_covers_every_writer(pretend_real_home):
    with pytest.raises(tk.TicketError):
        tk._write_atomic(pretend_real_home / "tickets" / "BD-1.json", {})
    with pytest.raises(tk.TicketError):
        dp.log("X", "y")
    with pytest.raises(tk.TicketError):
        with dp.single_instance():
            pass
    with pytest.raises(tk.TicketError):
        with kaneo._map_locked():
            pass
    with pytest.raises(tk.TicketError):
        kaneo.load_config()  # never even looks at a real kaneo.json
    assert list(pretend_real_home.iterdir()) == []


def test_tripwire_follows_symlinks_and_dotdot(pretend_real_home, tmp_path):
    link = tmp_path / "sneaky"
    link.symlink_to(pretend_real_home)
    with pytest.raises(tk.TicketError):
        tk.refuse_real_store_in_tests(link / "tickets")
    with pytest.raises(tk.TicketError):
        tk.refuse_real_store_in_tests(tmp_path / "x" / ".." / "realhome" / ".fleet")


def test_tripwire_allows_temporary_stores_and_non_test_runs(tmp_path, monkeypatch, pretend_real_home):
    tk.refuse_real_store_in_tests(tmp_path / "fleet")  # no error
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    tk.refuse_real_store_in_tests(pretend_real_home)   # not under pytest: no error


def test_tripwire_switch_reaches_subprocesses():
    # The helpers pass the test environment on, so bin/ticket run from a test
    # has the same tripwire (it is armed by PYTEST_CURRENT_TEST).
    import subprocess, sys
    out = subprocess.run([sys.executable, "-c", "import os; print('PYTEST_CURRENT_TEST' in os.environ)"],
                         capture_output=True, text=True, env=dict(os.environ))
    assert out.stdout.strip() == "True"
