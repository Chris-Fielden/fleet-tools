"""The `fleet` command: usage, doctor, and running the fleet (start, stop, restart,
status, logs, autostart, launcher; see service.py)."""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from . import doctor
from . import launch as lc
from . import ops
from . import service as sv
from . import settings as st
from . import ticket as tk
from . import ticketcost as tc
from . import usage as us
from .status import snapshot
from .status_server import DEFAULT_PORT, serve


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"fleet: {message}", file=sys.stderr)
        raise SystemExit(2)


# Verbs that hand the rest of the line to the command that already exists, so
# `fleet <verb> ARGS` is exactly `<verb> ARGS`. (module, function, help line)
FORWARDED = {
    "ship": ("ship_cli", "main", "merge a ticket in review: fleet ship ID (human only)"),
    "ticket": ("ticket_cli", "main", "tickets: new, list, show, note, approve, close ... (fleet ticket --help)"),
    "approve": ("ticket_cli", None, "approve a proposed ticket: fleet approve ID (human only)"),
    "alert": ("shortcuts_cli", "alert_main", "file an urgent ticket: fleet alert REPO TEXT (human only)"),
    "idea": ("shortcuts_cli", "idea_main", "file an idea for review: fleet idea TEXT (human only)"),
}


def _forward(argv: list[str]) -> int | None:
    """Run a forwarded verb; None if argv does not start with one."""
    if not argv or argv[0] not in FORWARDED:
        return None
    verb, rest = argv[0], argv[1:]
    module, func, _ = FORWARDED[verb]
    mod = importlib.import_module(f"fleet.{module}")
    if func is None:   # approve is `ticket approve`
        return mod.main(["approve", *rest])
    return getattr(mod, func)(rest)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    code = _forward(argv)
    if code is not None:
        return code
    if argv == ["help"]:
        argv = ["--help"]
    p = _Parser(prog="fleet", epilog="ship, ticket, alert and idea also still work as commands of their own.")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser, metavar="VERB")
    for verb, (_, _, text) in FORWARDED.items():
        sub.add_parser(verb, help=text, add_help=False)
    sub.add_parser("ops", help="open or reattach tmux session 'fleet': an ops shell and a read-only herdr view")
    sub.add_parser("help", help="this list")
    u = sub.add_parser("usage", help="each AI tool's usage against its limits (local, read-only)")
    u.add_argument("--by-ticket", action="store_true",
                   help="top tickets by tokens over the last 7 days (build vs review, Claude vs Codex)")
    u.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("doctor", help="check the settings, the tools on PATH and the tracker (read-only)")
    s = sub.add_parser("start", help="start herdr (headless), the workspaces and dispatch --watch; safe to repeat")
    s.add_argument("--pause", action="store_true", help="wait for Enter at the end (for the desktop launcher)")
    sub.add_parser("stop", help="stop dispatch --watch; agents are left running")
    sub.add_parser("restart", help="stop, then start")
    stat = sub.add_parser("status", help="herdr, the dispatcher, Kaneo, ntfy and tailscale serve (no secrets)")
    stat.add_argument("--json", action="store_true",
                      help="the widget snapshot instead: tokens, plan usage, agents, ready to ship, launches")
    ss = sub.add_parser("status-serve", help="serve that snapshot read-only for the Mac widgets (tailnet only)")
    ss.add_argument("--host", default="127.0.0.1", help="loopback or tailnet address (default 127.0.0.1)")
    ss.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    lg = sub.add_parser("logs", help="tail the dispatcher log and the other fleet logs")
    lg.add_argument("-n", type=int, default=30, metavar="LINES", help="lines per log (default 30)")
    a = sub.add_parser("autostart", help="start the fleet at boot (a systemd --user service); default off")
    a.add_argument("state", choices=("on", "off"))
    la = sub.add_parser("launcher", help="install or remove the desktop launcher that runs fleet start")
    la.add_argument("state", choices=("on", "off"))
    args = p.parse_args(argv)

    if args.cmd == "doctor":
        return doctor.main()
    if args.cmd == "status" and args.json:
        print(json.dumps(snapshot(), indent=2))
        return 0
    if args.cmd == "status-serve":
        try:
            return serve(args.host, args.port)
        except (ValueError, OSError) as exc:
            print(f"fleet: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "ops":
        try:
            return ops.cmd_ops()
        except (sv.ServiceError, OSError) as exc:
            print(f"fleet: {exc}", file=sys.stderr)
            return 1
    if args.cmd in ("start", "stop", "restart", "status", "logs", "autostart", "launcher"):
        return _service(args)

    if args.cmd == "usage" and args.by_ticket:
        rows = tc.by_ticket()
        print(json.dumps(rows, indent=2) if args.json else tc.table(rows))
        return 0
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
            return sv.cmd_stop() or sv.cmd_start()
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
