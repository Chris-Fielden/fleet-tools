"""`fleet start|stop|restart|status|logs|autostart|launcher`: run the fleet.

herdr can run headless (`herdr server`), so `fleet start` launches that in the
background when it is not running, makes sure the fleet and fleet-tools
workspaces exist, and starts `dispatch --watch` in a tab of fleet-tools.
Everything is idempotent. `fleet stop` stops only the dispatcher; agents are
left alone. Nothing here prints a secret, and nothing runs sudo.

Programs it calls can be swapped for tests: $FLEET_HERDR, $FLEET_SYSTEMCTL,
$FLEET_LOGINCTL and $FLEET_TAILSCALE.
"""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import kaneo
from . import launch as lc
from . import notify
from . import settings as st
from . import ticket as tk

TOOLS_WORKSPACE = "fleet-tools"
DISPATCH_TAB = "dispatch"
UNIT_NAME = "fleet.service"
LAUNCHER_NAME = "fleet.desktop"
ROOT = Path(__file__).resolve().parent.parent

# How long to wait for things that start in the background; tests shorten them.
WAIT_SECONDS = 15.0
POLL_SECONDS = 0.25


class ServiceError(Exception):
    pass


# --- helpers -----------------------------------------------------------------

def _command(env_var: str, default: str) -> list[str]:
    try:
        return shlex.split(os.environ.get(env_var) or default)
    except ValueError:
        raise ServiceError(f"${env_var} is not a valid command line") from None


def _run(cmd: list[str], timeout: float = 15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"{cmd[0]} failed: {type(exc).__name__}") from None


def _wait_for(condition, seconds: float | None = None) -> bool:
    deadline = time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _refuse_agent() -> None:
    who = tk.current_creator()
    if who != "human":
        raise ServiceError(f"refusing to run inside an agent (running as {who}); only a human runs the fleet")


# --- herdr -------------------------------------------------------------------

def herdr_running() -> bool:
    try:
        proc = _run(_command("FLEET_HERDR", "herdr") + ["status", "server"])
    except ServiceError:
        return False
    if proc.returncode != 0:
        return False
    return any(line.strip() == "status: running" for line in proc.stdout.splitlines())


def start_herdr() -> bool:
    """Make sure the herdr server is running. True if this call started it."""
    if herdr_running():
        return False
    log = tk.fleet_home() / "herdr-server.log"
    tk.refuse_real_store_in_tests(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log, "ab") as out:
            subprocess.Popen(_command("FLEET_HERDR", "herdr") + ["server"], stdin=subprocess.DEVNULL,
                             stdout=out, stderr=out, start_new_session=True)
    except OSError as exc:
        raise ServiceError(f"could not start herdr: {exc.strerror or type(exc).__name__}") from None
    if not _wait_for(herdr_running):
        raise ServiceError(f"herdr did not come up in {WAIT_SECONDS:g}s (see {log})")
    return True


def _dispatch_tabs(workspace: str) -> list[dict]:
    tabs = lc.herdr("tab", "list", "--workspace", workspace).get("result")
    tabs = tabs.get("tabs") if isinstance(tabs, dict) else None
    if not isinstance(tabs, list):
        raise ServiceError("herdr tab list did not return .result.tabs as a list")
    return [t for t in tabs if isinstance(t, dict) and t.get("label") == DISPATCH_TAB]


def _close_tabs(workspace: str) -> int:
    n = 0
    for tab in _dispatch_tabs(workspace):
        if isinstance(tab.get("tab_id"), str):
            lc.herdr("tab", "close", tab["tab_id"])
            n += 1
    return n


# --- the dispatcher ----------------------------------------------------------

def _lock_path() -> Path:
    return tk.fleet_home() / "dispatch.lock"


def dispatcher_running() -> bool:
    """True if some dispatch holds the dispatcher lock. Never keeps the lock."""
    path = _lock_path()
    if not path.is_file():
        return False
    try:
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def dispatcher_pid() -> int | None:
    """The pid of the running `dispatch --watch`, or None if it cannot be
    identified (the pid is checked against the process's command line)."""
    try:
        pid = int(_lock_path().read_text().strip())
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    words = [a.decode(errors="replace") for a in argv]
    if "--watch" in words and any(Path(w).name == "dispatch" or w.endswith("dispatch_cli") for w in words):
        return pid
    return None


def start_dispatcher(workspace: str) -> bool:
    """Start `dispatch --watch` in a tab of `workspace`. True if started."""
    if dispatcher_running():
        return False
    _close_tabs(workspace)   # a leftover tab of a dispatcher that is gone
    args = ["tab", "create", "--workspace", workspace, "--cwd", str(ROOT), "--label", DISPATCH_TAB, "--no-focus"]
    for name in ("FLEET_HOME", "FLEET_HERDR"):
        if os.environ.get(name):
            args += ["--env", f"{name}={os.environ[name]}"]
    created = lc.herdr(*args)
    pane = lc._result(created, "root_pane", "pane_id")
    lc.herdr("pane", "run", pane, shlex.join([sys.executable, str(ROOT / "bin" / "dispatch"), "--watch"]))
    if not _wait_for(dispatcher_running):
        raise ServiceError(f"dispatch --watch did not start (look at the '{DISPATCH_TAB}' tab "
                           f"of workspace {TOOLS_WORKSPACE})")
    return True


def stop_dispatcher() -> str:
    """Stop `dispatch --watch` cleanly (SIGINT, like Ctrl+C). Agents are left alone."""
    if not dispatcher_running():
        return "dispatcher was not running"
    pid = dispatcher_pid()
    if pid is None:
        raise ServiceError("a dispatcher holds the lock but I cannot tell which process it is; "
                           "stop it by hand (Ctrl+C in its tab)")
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        raise ServiceError(f"could not signal dispatcher {pid}: {exc.strerror}") from None
    if not _wait_for(lambda: not dispatcher_running()):
        raise ServiceError(f"dispatcher {pid} did not stop in {WAIT_SECONDS:g}s")
    return f"dispatcher stopped (was pid {pid})"


# --- start, stop, restart ----------------------------------------------------

def cmd_start(pause: bool = False) -> int:
    settings = st.load_settings()
    started = start_herdr()
    print("herdr: " + ("started in the background" if started else "already running"))
    for name in dict.fromkeys((settings["herdr_workspace"], TOOLS_WORKSPACE)):
        print(f"workspace {name}: {lc.ensure_workspace(name)}")
    tools = lc.ensure_workspace(TOOLS_WORKSPACE)
    if start_dispatcher(tools):
        print(f"dispatcher: started in tab '{DISPATCH_TAB}' of {TOOLS_WORKSPACE}")
    else:
        print("dispatcher: already running")
    if pause and sys.stdin.isatty():
        input("\nPress Enter to close. ")
    return 0


def cmd_stop() -> int:
    print(stop_dispatcher())
    try:
        if herdr_running():
            closed = _close_tabs(lc.find_workspace(TOOLS_WORKSPACE))
            if closed:
                print(f"closed the '{DISPATCH_TAB}' tab")
    except (lc.LaunchError, ServiceError):
        pass   # no workspace or tab to tidy: the dispatcher is what matters
    print("agents were left running")
    return 0


# --- status ------------------------------------------------------------------

def _host(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return parts.netloc.rpartition("@")[2] or "?"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _reachable(url: str) -> tuple[bool, str]:
    """GET without any credentials. Any HTTP answer means the server is up."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=4) as resp:
            body = resp.read(4096)
            return True, f"HTTP {resp.status}" + _healthy_suffix(body)
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"unreachable ({type(reason).__name__})"


def _healthy_suffix(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("healthy"), bool):
        return ", healthy" if data["healthy"] else ", reports unhealthy"
    return ""


def _service_row(label: str, load, url_suffix: str = "") -> tuple[str, str, str]:
    try:
        cfg = load()
    except (kaneo.NotConfigured, notify.NotConfigured):
        return ("off", label, "not configured")
    except (kaneo.KaneoError, notify.NotifyError, st.SettingsError) as exc:
        return ("fail", label, str(exc))
    up, detail = _reachable(cfg["url"] + url_suffix)
    return ("ok" if up else "fail", label, f"{_host(cfg['url'])}: {detail}")


def _tailscale_rows() -> list[tuple[str, str, str]]:
    try:
        proc = _run(_command("FLEET_TAILSCALE", "tailscale") + ["serve", "status", "--json"])
    except ServiceError as exc:
        return [("warn", "tailscale serve", str(exc))]
    if proc.returncode != 0:
        return [("warn", "tailscale serve", "tailscale did not answer")]
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return [("warn", "tailscale serve", "tailscale printed something unreadable")]
    rows = []
    for host, conf in sorted((data.get("Web") or {}).items()):
        for path, handler in sorted(((conf or {}).get("Handlers") or {}).items()):
            target = handler.get("Proxy") or handler.get("Path") or handler.get("Text") and "(text)" or "?"
            if "@" in target:
                target = "(hidden)"
            rows.append(("ok", "tailscale serve", f"{host}{path} -> {target}"))
    for port, conf in sorted((data.get("TCP") or {}).items()):
        if isinstance(conf, dict) and conf.get("TCPForward"):
            rows.append(("ok", "tailscale serve", f"tcp {port} -> {conf['TCPForward']}"))
    return rows or [("off", "tailscale serve", "no entries")]


def _autostart_row() -> tuple[str, str, str]:
    on = unit_path().is_file()
    return ("ok" if on else "off", "autostart", "on" if on else "off (fleet autostart on)")


def status_rows() -> list[tuple[str, str, str]]:
    herdr_up = herdr_running()
    rows = [("ok" if herdr_up else "fail", "herdr", "running" if herdr_up else "not running (fleet start)")]
    if dispatcher_running():
        pid = dispatcher_pid()
        rows.append(("ok", "autopilot", "dispatch --watch running" + (f" (pid {pid})" if pid else "")))
    else:
        rows.append(("fail", "autopilot", "dispatch --watch not running (fleet start)"))
    rows.append(_service_row("kaneo", kaneo.load_config))
    rows.append(_service_row("ntfy", notify.load_config, "/v1/health"))
    rows += _tailscale_rows()
    rows.append(_autostart_row())
    return rows


def cmd_status() -> int:
    rows = status_rows()
    for state, what, detail in rows:
        print(f"  {state.upper():<4}  {what:<16} {detail}")
    return 1 if any(r[0] == "fail" and r[1] in ("herdr", "autopilot") for r in rows) else 0


# --- logs --------------------------------------------------------------------

def _tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read()
    except OSError:
        return []
    return data.decode(errors="replace").splitlines()[-n:]


def cmd_logs(n: int) -> int:
    home = tk.fleet_home()
    names = ["dispatch.log"] + sorted(p.name for p in home.glob("*.log") if p.name != "dispatch.log")
    shown = False
    for name in names:
        lines = _tail(home / name, n)
        if lines:
            shown = True
            print(f"==> {home / name} <==")
            print("\n".join(lines))
            print()
    if not shown:
        print(f"no logs yet in {home}")
    return 0


# --- autostart and launcher --------------------------------------------------

def unit_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config) / "systemd" / "user" / UNIT_NAME


def _unit_quote(word: str) -> str:
    if any(c in word for c in "\n\r"):
        raise ServiceError("a path with a line break cannot go in a service file")
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_text() -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for name in ("FLEET_HOME", "FLEET_HERDR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    exe = [sys.executable, str(ROOT / "bin" / "fleet")]
    lines = ["[Unit]", "Description=Start the agent fleet (herdr and the dispatcher)",
             "After=network-online.target", "", "[Service]", "Type=oneshot", "RemainAfterExit=yes"]
    lines += [f"Environment={_unit_quote(f'{k}={v}')}" for k, v in env.items()]
    lines += ["ExecStart=" + " ".join(_unit_quote(w) for w in exe + ["start"]),
              "ExecStop=" + " ".join(_unit_quote(w) for w in exe + ["stop"]),
              "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _systemctl(*args: str) -> None:
    proc = _run(_command("FLEET_SYSTEMCTL", "systemctl") + ["--user", *args])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise ServiceError(f"systemctl --user {' '.join(args)} failed" + (f": {detail[0][:200]}" if detail else ""))


def _linger_enabled(user: str) -> bool:
    try:
        proc = _run(_command("FLEET_LOGINCTL", "loginctl") + ["show-user", user, "--property=Linger", "--value"])
    except ServiceError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "yes"


def cmd_autostart(state: str) -> int:
    user = getpass.getuser()
    path = unit_path()
    if state == "on":
        _write(path, unit_text())
        _systemctl("daemon-reload")
        _systemctl("enable", UNIT_NAME)
        print(f"autostart on: {path} enabled")
        if _linger_enabled(user):
            print("linger is already on, so the fleet starts at boot without a login")
        else:
            print("To start at boot without a login, run this once (it needs sudo, so I do not run it):")
            print(f"  sudo loginctl enable-linger {user}")
        return 0
    if path.is_file():
        _systemctl("disable", UNIT_NAME)
        path.unlink()
        _systemctl("daemon-reload")
        print(f"autostart off: {path} removed")
    else:
        print("autostart was already off")
    if _linger_enabled(user):
        print("linger is still on for your user; to turn it off too (only if nothing else needs it):")
        print(f"  sudo loginctl disable-linger {user}")
    return 0


def launcher_paths() -> list[Path]:
    return [Path.home() / ".local" / "share" / "applications" / LAUNCHER_NAME,
            Path.home() / "Desktop" / LAUNCHER_NAME]


def _desktop_arg(word: str) -> str:
    if any(c in word for c in '"`$\\%\n\r'):
        raise ServiceError(f"{word!r} has a character a desktop launcher cannot hold safely")
    return f'"{word}"'


def launcher_text() -> str:
    exec_line = " ".join(_desktop_arg(w) for w in [sys.executable, str(ROOT / "bin" / "fleet"), "start", "--pause"])
    return ("[Desktop Entry]\nType=Application\nName=Fleet\nComment=Start herdr and the dispatcher\n"
            f"Exec={exec_line}\nTerminal=true\nCategories=Development;\n")


def cmd_launcher(state: str) -> int:
    paths = launcher_paths()
    if state == "on":
        text = launcher_text()
        for p in paths:
            _write(p, text, 0o755)
            print(f"launcher installed: {p}")
        return 0
    removed = [p for p in paths if p.is_file()]
    for p in removed:
        p.unlink()
        print(f"launcher removed: {p}")
    if not removed:
        print("no launcher was installed")
    return 0
