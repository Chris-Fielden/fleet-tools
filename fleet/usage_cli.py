"""The `fleet` command: usage, doctor, and running the fleet (start, stop, restart,
status, logs, autostart, launcher; see service.py)."""

from __future__ import annotations

import argparse
import json
import sys

from . import doctor
from . import launch as lc
from . import service as sv
from . import settings as st
from . import ticket as tk
from . import usage as us


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"fleet: {message}", file=sys.stderr)
        raise SystemExit(2)


def main(argv=None) -> int:
    p = _Parser(prog="fleet")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    u = sub.add_parser("usage", help="each AI tool's usage against its limits (local, read-only)")
    u.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("doctor", help="check the settings, the tools on PATH and the tracker (read-only)")
    s = sub.add_parser("start", help="start herdr (headless), the workspaces and dispatch --watch; safe to repeat")
    s.add_argument("--pause", action="store_true", help="wait for Enter at the end (for the desktop launcher)")
    sub.add_parser("stop", help="stop dispatch --watch; agents are left running")
    sub.add_parser("restart", help="stop, then start")
    sub.add_parser("status", help="herdr, the dispatcher, Kaneo, ntfy and tailscale serve (no secrets)")
    lg = sub.add_parser("logs", help="tail the dispatcher log and the other fleet logs")
    lg.add_argument("-n", type=int, default=30, metavar="LINES", help="lines per log (default 30)")
    a = sub.add_parser("autostart", help="start the fleet at boot (a systemd --user service); default off")
    a.add_argument("state", choices=("on", "off"))
    la = sub.add_parser("launcher", help="install or remove the desktop launcher that runs fleet start")
    la.add_argument("state", choices=("on", "off"))
    args = p.parse_args(argv)

    if args.cmd == "doctor":
        return doctor.main()
    if args.cmd in ("start", "stop", "restart", "status", "logs", "autostart", "launcher"):
        return _service(args)

    results = us.collect()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(us.line(r))
    return 0


def _service(args) -> int:
    try:
        if args.cmd in ("start", "stop", "restart", "autostart"):
            sv._refuse_agent()
        if args.cmd == "start":
            return sv.cmd_start(args.pause)
        if args.cmd == "stop":
            return sv.cmd_stop()
        if args.cmd == "restart":
            sv.cmd_stop()
            return sv.cmd_start()
        if args.cmd == "status":
            return sv.cmd_status()
        if args.cmd == "logs":
            return sv.cmd_logs(max(args.n, 1))
        if args.cmd == "autostart":
            return sv.cmd_autostart(args.state)
        return sv.cmd_launcher(args.state)
    except (sv.ServiceError, lc.LaunchError, st.SettingsError, tk.TicketError, OSError) as exc:
        print(f"fleet: {exc}", file=sys.stderr)
        return 1
