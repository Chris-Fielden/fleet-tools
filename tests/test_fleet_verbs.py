"""`fleet <verb>`: every verb reaches the right command, `fleet help` lists them,
and `fleet ops` builds the tmux session with a loud prompt."""

import importlib

import pytest

from fleet import usage_cli
from fleet import service as sv


def _spy(monkeypatch, module, name):
    seen = []
    monkeypatch.setattr(module, name, lambda argv=None: seen.append(list(argv)) or 7)
    return seen


@pytest.mark.parametrize("line, module, func, expected", [
    (["ship", "BD-1"], "ship_cli", "main", ["BD-1"]),
    (["ship", "--list"], "ship_cli", "main", ["--list"]),
    (["ticket", "show", "BD-1"], "ticket_cli", "main", ["show", "BD-1"]),
    (["ticket", "close", "BD-1", "--done"], "ticket_cli", "main", ["close", "BD-1", "--done"]),
    (["approve", "BD-2"], "ticket_cli", "main", ["approve", "BD-2"]),
    (["alert", "repo", "it is down"], "shortcuts_cli", "alert_main", ["repo", "it is down"]),
    (["idea", "a", "thing"], "shortcuts_cli", "idea_main", ["a", "thing"]),
])
def test_forwarded_verbs_reach_the_old_command(monkeypatch, line, module, func, expected):
    seen = _spy(monkeypatch, importlib.import_module(f"fleet.{module}"), func)
    assert usage_cli.main(line) == 7
    assert seen == [expected]


def test_help_lists_every_verb(capsys):
    with pytest.raises(SystemExit):
        usage_cli.main(["help"])
    out = capsys.readouterr().out
    for verb in ("ship", "ticket", "approve", "alert", "idea", "usage", "start", "stop", "restart",
                 "status", "logs", "autostart", "ops", "help", "doctor", "launcher", "heartbeat-check"):
        assert any(line.split()[:1] == [verb] for line in out.splitlines()), verb


def test_service_verbs_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(sv, "cmd_start", lambda pause=False: calls.append("start") or 0)
    monkeypatch.setattr(sv, "cmd_stop", lambda: calls.append("stop") or 0)
    monkeypatch.setattr(sv, "cmd_status", lambda: calls.append("status") or 0)
    monkeypatch.setattr(sv, "cmd_logs", lambda n: calls.append("logs") or 0)
    monkeypatch.setattr(usage_cli.us, "collect", lambda: calls.append("usage") or [])
    for verb in ("start", "stop", "restart", "status", "logs", "usage"):
        assert usage_cli.main([verb]) == 0
    assert calls == ["start", "stop", "stop", "start", "status", "logs", "usage"]


def test_unknown_verb_is_an_error():
    with pytest.raises(SystemExit) as exc:
        usage_cli.main(["frobnicate"])
    assert exc.value.code == 2


@pytest.fixture
def fake_tmux(env, monkeypatch):
    log = env / "tmux.log"
    script = env / "fake-tmux"
    state = env / "tmux-sessions"
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "case \"$1\" in\n"
        # like tmux: an exact name only with a leading =, otherwise a prefix matches too
        f"  has-session) case \"$3\" in =*) grep -qx \"${{3#=}}\" {state};; *) grep -q \"^$3\" {state};; esac 2>/dev/null; exit $?;;\n"
        f"  new-session) n=; while [ $# -gt 0 ]; do [ \"$1\" = -s ] && n=$2; shift; done; echo $n >> {state};;\n"
        "esac\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("FLEET_TMUX", str(script))
    monkeypatch.delenv("TMUX", raising=False)
    return log


def test_ops_creates_two_windows_then_reattaches(fake_tmux, capsys):
    assert usage_cli.main(["ops"]) == 0
    first = fake_tmux.read_text().splitlines()
    assert any(l.startswith("new-session -d -s fleet-herdr") for l in first)
    assert any(l.startswith("new-session -d -s fleet -n ops") and "--rcfile" in l for l in first)
    assert any(l.startswith("new-window -t =fleet: -n herdr") and "env -u TMUX" in l
               and "attach -r -t =fleet-herdr" in l for l in first)
    assert first[-1] == "attach -t =fleet"
    assert "created" in capsys.readouterr().out
    fake_tmux.write_text("")
    assert usage_cli.main(["ops"]) == 0
    again = fake_tmux.read_text().splitlines()
    assert not any(l.startswith(("new-session", "new-window")) for l in again)
    assert again[-1] == "attach -t =fleet"
    assert "already running" in capsys.readouterr().out


def test_ops_prompt_is_loud(fake_tmux, env):
    usage_cli.main(["ops"])
    rc = (env / "fleet" / "ops.bashrc").read_text()
    assert "[LOIC ops]" in rc and "PS1=" in rc


def test_ops_says_how_to_install_tmux(monkeypatch, capsys):
    monkeypatch.setenv("FLEET_TMUX", "no-such-tmux-anywhere")
    assert usage_cli.main(["ops"]) == 1
    assert "sudo apt install -y tmux" in capsys.readouterr().err


def test_ops_refuses_inside_an_agent(fake_tmux, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert usage_cli.main(["ops"]) == 1
    assert not fake_tmux.exists()
    assert "agent" in capsys.readouterr().err
