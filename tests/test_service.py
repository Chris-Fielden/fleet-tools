import json
import os
import signal
import subprocess
import sys

import pytest

from conftest import ROOT
from fleet import service as sv
from fleet import usage_cli

FAKE = ROOT / "tests" / "fake_herdr.py"
REAL_START_DIRECTLY = sv._start_directly   # never run for real unless a test says so


@pytest.fixture(autouse=True)
def rig(env, monkeypatch):
    monkeypatch.setattr(sv, "WAIT_SECONDS", 0.5)
    monkeypatch.setattr(sv, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(sv, "_start_directly", lambda: None)
    (env / "fleet").mkdir()
    world = env / "world.json"
    world.write_text(json.dumps({"workspaces": [], "tabs": [], "agents": []}))
    monkeypatch.setenv("FLEET_HERDR", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("FAKE_HERDR_LOG", str(env / "herdr.log"))
    monkeypatch.setenv("FAKE_HERDR_WORLD", str(world))
    monkeypatch.setenv("FAKE_HERDR_SERVER", str(env / "server-up"))
    for name in ("FLEET_SYSTEMCTL", "FLEET_LOGINCTL"):
        script = env / name
        script.write_text(f"#!/bin/sh\necho \"$0 $@\" >> {env}/{name}.log\n"
                          + ("echo no\n" if name == "FLEET_LOGINCTL" else ""))
        script.chmod(0o755)
        monkeypatch.setenv(name, str(script))
    monkeypatch.setenv("FLEET_TAILSCALE", str(env / "no-tailscale"))
    return world


def calls(env):
    p = env / "herdr.log"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def hold_lock(env):
    """A child that holds the dispatcher lock and looks like a watching dispatcher."""
    code = ("import fcntl,os,sys,time\nf=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX)\n"
            "f.truncate(0);f.write(str(os.getpid()));f.flush();print('ready',flush=True);time.sleep(60)")
    lock = str(env / "fleet" / "dispatch.lock")
    proc = subprocess.Popen([sys.executable, "-c", code, lock, "bin/dispatch", "--watch"],
                            stdout=subprocess.PIPE, text=True)
    proc.stdout.readline()
    return proc


def test_start_creates_everything_then_is_idempotent(env, monkeypatch, capsys):
    running = iter([False, True])
    monkeypatch.setattr(sv, "dispatcher_running", lambda: next(running, True))
    assert usage_cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "started in the background" in out and "dispatcher: started" in out
    assert (env / "server-up").exists()
    names = [c[:2] for c in calls(env)]
    assert ["workspace", "create"] in names and ["pane", "run"] in names
    labels = [c[c.index("--label") + 1] for c in calls(env) if c[:2] == ["workspace", "create"]]
    assert labels == ["fleet", "fleet-tools"]
    run = next(c for c in calls(env) if c[:2] == ["pane", "run"])
    assert run[-1].endswith("bin/dispatch --watch")

    monkeypatch.setattr(sv, "dispatcher_running", lambda: True)
    before = len(calls(env))
    assert usage_cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "herdr: already running" in out and "dispatcher: already running" in out
    changed = [c for c in calls(env)[before:]
               if c[:2] in (["pane", "run"], ["tab", "create"], ["workspace", "create"])]
    assert not changed


def test_start_fails_plainly_if_dispatcher_never_appears(monkeypatch, capsys):
    monkeypatch.setattr(sv, "dispatcher_running", lambda: False)
    monkeypatch.setattr(sv, "_start_directly", lambda: None)
    assert usage_cli.main(["start"]) == 1
    assert "did not start" in capsys.readouterr().err


def stand_in(env):
    path = env / "stand-in.py"
    path.write_text("import sys, time\nsys.path.insert(0, %r)\nfrom fleet import dispatch as dp\n"
                    "with dp.single_instance():\n    time.sleep(60)\n" % str(ROOT))
    return path


def test_start_succeeds_when_herdr_prints_text_but_the_dispatcher_runs(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERDR_RUN_AS", str(stand_in(env)))
    monkeypatch.setenv("FAKE_HERDR_RUN_TEXT", "ok, ran it (not json)")
    monkeypatch.setattr(sv, "WAIT_SECONDS", 5)
    assert usage_cli.main(["start"]) == 0
    assert "dispatcher: started" in capsys.readouterr().out
    assert sv.dispatcher_running()
    usage_cli.main(["stop"])


def test_start_warns_but_succeeds_when_herdr_fails_yet_the_dispatcher_is_alive(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERDR_RUN_AS", str(stand_in(env)))
    monkeypatch.setenv("FAKE_HERDR_RUN_FAIL", "1")
    monkeypatch.setattr(sv, "WAIT_SECONDS", 5)
    assert usage_cli.main(["start"]) == 0
    assert "but the dispatcher is running" in capsys.readouterr().err
    usage_cli.main(["stop"])


def test_start_retries_herdr_then_falls_back_to_a_direct_start(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERDR_RUN_FAIL", "1")
    direct = []
    state = {"up": False}
    monkeypatch.setattr(sv, "dispatcher_running", lambda: state["up"])
    monkeypatch.setattr(sv, "_start_directly", lambda: direct.append(1) or state.update(up=True))
    assert usage_cli.main(["start"]) == 0
    assert direct == [1]
    assert len([c for c in calls(env) if c[:2] == ["pane", "run"]]) == sv.START_ATTEMPTS
    assert "starting it directly" in capsys.readouterr().err


def test_start_sends_one_high_ping_when_the_dispatcher_stays_down(env, monkeypatch, capsys):
    monkeypatch.setattr(sv, "dispatcher_running", lambda: False)
    monkeypatch.setattr(sv, "_start_directly", lambda: None)
    sent = []
    monkeypatch.setattr(sv.notify, "send_alert", lambda *a: sent.append(a))
    assert usage_cli.main(["start"]) == 1
    assert len(sent) == 1 and sent[0][0] == "dispatcher_down"


def test_the_dispatcher_down_ping_is_high_priority(env, monkeypatch):
    import json as js
    from fleet import notify
    (env / "fleet" / "notify.json").write_text(js.dumps({"url": "http://127.0.0.1:9", "topic": "t"}))
    (env / "fleet" / "notify.json").chmod(0o600)
    cfg = notify.load_config()
    assert cfg["priorities"]["dispatcher_down"] == "high"
    got = {}
    monkeypatch.setattr(notify, "_post", lambda c, m, p: got.update(m=m, p=p) or True)
    assert notify.send_alert("dispatcher_down", "down", "why")
    assert got["p"] == "high" and got["m"]["priority"] == 4


def test_start_refuses_inside_an_agent(env, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert usage_cli.main(["start"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert calls(env) == []


def test_dispatcher_running_follows_the_lock(env):
    assert not sv.dispatcher_running()
    child = hold_lock(env)
    try:
        assert sv.dispatcher_running() and sv.dispatcher_pid() == child.pid
    finally:
        child.kill()
        child.wait()
    assert not sv.dispatcher_running()


def test_stop_interrupts_the_dispatcher_and_leaves_agents(env, capsys):
    child = hold_lock(env)
    try:
        assert usage_cli.main(["stop"]) == 0
        assert child.wait(timeout=5) != 0
    finally:
        child.kill()
    out = capsys.readouterr().out
    assert "dispatcher stopped" in out and "agents were left running" in out
    assert not [c for c in calls(env) if c[0] == "agent"]


def test_stop_when_not_running(capsys):
    assert usage_cli.main(["stop"]) == 0
    assert "was not running" in capsys.readouterr().out


def test_stop_will_not_signal_an_unidentified_process(env, capsys):
    child = hold_lock(env)
    (env / "fleet" / "dispatch.lock").write_text("1")   # pid 1 is not a dispatcher
    try:
        assert usage_cli.main(["stop"]) == 1
    finally:
        child.kill()
    assert "cannot tell which process" in capsys.readouterr().err


def test_status_prints_no_secrets(env, capsys):
    for name, body in (("kaneo.json", {"url": "http://127.0.0.1:9", "workspace_id": "w",
                                       "api_key": "SECRETKEY123"}),
                       ("notify.json", {"url": "http://127.0.0.1:9", "topic": "t",
                                        "token": "SECRETTOKEN456"})):
        (env / "fleet" / name).write_text(json.dumps(body))
        (env / "fleet" / name).chmod(0o600)
    assert usage_cli.main(["status"]) == 1   # herdr is not running
    out = capsys.readouterr().out
    assert "SECRET" not in out
    for word in ("herdr", "autopilot", "kaneo", "ntfy", "tailscale serve", "autostart"):
        assert word in out
    assert "unreachable" in out


def test_status_ok_with_fake_tailscale(env, monkeypatch, capsys):
    (env / "server-up").touch()
    child = hold_lock(env)
    ts = env / "ts"
    ts.write_text('#!/bin/sh\necho \'{"Web":{"box.ts.net:443":{"Handlers":'
                  '{"/":{"Proxy":"http://127.0.0.1:8080"}}}}}\'\n')
    ts.chmod(0o755)
    monkeypatch.setenv("FLEET_TAILSCALE", str(ts))
    (env / "world.json").write_text(json.dumps(
        {"workspaces": [{"workspace_id": "w1", "label": "fleet"}], "tabs": [], "agents": []}))
    try:
        assert usage_cli.main(["status"]) == 0
    finally:
        child.kill()
    out = capsys.readouterr().out
    assert "box.ts.net:443/ -> http://127.0.0.1:8080" in out and "not configured" in out


def test_logs_tail(env, capsys):
    (env / "fleet" / "dispatch.log").write_text("".join(f"line {i}\n" for i in range(50)))
    assert usage_cli.main(["logs", "-n", "3"]) == 0
    out = capsys.readouterr().out
    assert "line 49" in out and "line 47" in out and "line 46" not in out


def test_autostart_on_off_uses_the_fake_systemctl_only(env, capsys):
    assert usage_cli.main(["autostart", "on"]) == 0
    unit = env / "home" / ".config" / "systemd" / "user" / "fleet.service"
    text = unit.read_text()
    assert "bin/fleet" in text and "WantedBy=default.target" in text and "ExecStop=" in text
    assert "KillMode=process" in text   # stopping the unit must not kill herdr or the agents
    assert "sudo loginctl enable-linger" in capsys.readouterr().out
    log = (env / "FLEET_SYSTEMCTL.log").read_text()
    assert "--user enable fleet.service" in log and "daemon-reload" in log
    assert usage_cli.main(["autostart", "off"]) == 0
    assert not unit.exists()
    assert "--user disable fleet.service" in (env / "FLEET_SYSTEMCTL.log").read_text()


def test_autostart_off_when_never_on(env, capsys):
    assert usage_cli.main(["autostart", "off"]) == 0
    assert "already off" in capsys.readouterr().out
    assert not (env / "FLEET_SYSTEMCTL.log").exists()


def test_launcher_skips_a_missing_desktop_folder(env, capsys):
    assert usage_cli.main(["launcher", "on"]) == 0
    assert "no desktop icon" in capsys.readouterr().out
    assert sv.launcher_paths()[0].is_file() and not (env / "home" / "Desktop").exists()


def test_launcher_installs_in_both_places(env):
    (env / "home" / "Desktop").mkdir()
    assert usage_cli.main(["launcher", "on"]) == 0
    paths = sv.launcher_paths()
    assert len(paths) == 2 and all(p.is_file() for p in paths)
    text = paths[0].read_text()
    assert "Terminal=true" in text and "start" in text and "--pause" in text
    assert usage_cli.main(["launcher", "off"]) == 0
    assert not any(p.exists() for p in paths)


def test_restart_stops_then_starts(monkeypatch):
    order = []
    monkeypatch.setattr(sv, "stop_dispatcher", lambda: order.append("stop") or "stopped")
    monkeypatch.setattr(sv, "start_dispatcher", lambda *a: order.append("start") or True)
    monkeypatch.setattr(sv, "dispatcher_running", lambda: True)
    assert usage_cli.main(["restart"]) == 0
    assert order == ["stop", "start"]


def test_dispatch_lock_records_its_pid(env):
    from fleet import dispatch as dp
    with dp.single_instance():
        assert (env / "fleet" / "dispatch.lock").read_text() == str(os.getpid())
        assert sv.dispatcher_running()


def test_a_blank_command_override_is_a_plain_error(monkeypatch, capsys):
    monkeypatch.setenv("FLEET_HERDR", "  ")
    assert usage_cli.main(["start"]) == 1
    assert "FLEET_HERDR is not a valid command" in capsys.readouterr().err


def test_status_survives_odd_tailscale_output(env, monkeypatch, capsys):
    ts = env / "ts"
    ts.write_text("#!/bin/sh\necho '[1,2]'\n")
    ts.chmod(0o755)
    monkeypatch.setenv("FLEET_TAILSCALE", str(ts))
    usage_cli.main(["status"])
    assert "unexpected" in capsys.readouterr().out


def test_status_says_when_the_service_is_installed_but_not_enabled(env, capsys):
    usage_cli.main(["autostart", "on"])
    (env / "FLEET_SYSTEMCTL").write_text("#!/bin/sh\necho disabled\nexit 1\n")
    usage_cli.main(["status"])
    assert "service is disabled" in capsys.readouterr().out


def test_status_survives_a_server_that_hangs_up(monkeypatch):
    import http.client
    monkeypatch.setattr(sv.urllib.request.OpenerDirector, "open",
                        lambda *a, **k: (_ for _ in ()).throw(http.client.RemoteDisconnected("x")))
    up, detail = sv._reachable("http://127.0.0.1:9")
    assert not up and "RemoteDisconnected" in detail


def test_the_started_command_really_takes_the_lock_and_stop_ends_it(env, monkeypatch, capsys):
    """No stubbing of dispatcher_running: a stand-in runs under the real lock."""
    stand_in = env / "stand-in.py"
    stand_in.write_text("import sys, time\nsys.path.insert(0, %r)\nfrom fleet import dispatch as dp\n"
                        "with dp.single_instance():\n    time.sleep(60)\n" % str(ROOT))
    monkeypatch.setenv("FAKE_HERDR_RUN_AS", str(stand_in))
    monkeypatch.setattr(sv, "WAIT_SECONDS", 5)
    assert usage_cli.main(["start"]) == 0
    assert "dispatcher: started" in capsys.readouterr().out
    assert sv.dispatcher_running() and sv.dispatcher_pid()
    assert usage_cli.main(["start"]) == 0
    assert "dispatcher: already running" in capsys.readouterr().out
    assert usage_cli.main(["stop"]) == 0
    assert not sv.dispatcher_running()


def test_start_still_recovers_when_herdr_cannot_start_at_all(env, monkeypatch, capsys):
    """herdr failing before the dispatcher step (server, workspaces) must not skip the fallback."""
    monkeypatch.setenv("FLEET_HERDR", str(env / "no-such-herdr"))
    state = {"up": False}
    monkeypatch.setattr(sv, "dispatcher_running", lambda: state["up"])
    monkeypatch.setattr(sv, "_start_directly", lambda: state.update(up=True))
    assert usage_cli.main(["start"]) == 0
    assert "starting it directly" in capsys.readouterr().err


def test_start_pings_when_herdr_is_broken_and_the_dispatcher_stays_down(env, monkeypatch, capsys):
    monkeypatch.setenv("FLEET_HERDR", str(env / "no-such-herdr"))
    monkeypatch.setattr(sv, "dispatcher_running", lambda: False)
    monkeypatch.setattr(sv, "_start_directly", lambda: None)
    sent = []
    monkeypatch.setattr(sv.notify, "send_alert", lambda *a: sent.append(a))
    assert usage_cli.main(["start"]) == 1
    assert len(sent) == 1


def test_a_direct_start_says_so_and_names_its_log(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_HERDR_RUN_FAIL", "1")
    monkeypatch.setattr(sv, "ROOT", env)   # bin/dispatch under it is a stand-in
    (env / "bin").mkdir()
    (env / "bin" / "dispatch").write_text("import time\ntime.sleep(1)\n")
    state = {"up": False}
    monkeypatch.setattr(sv, "dispatcher_running", lambda: state["up"])
    real = REAL_START_DIRECTLY
    monkeypatch.setattr(sv, "_start_directly", lambda: real() or state.update(up=True))
    assert usage_cli.main(["start"]) == 0
    assert "started directly, without herdr" in capsys.readouterr().out
    assert (env / "fleet" / "dispatch-direct.log").exists()
