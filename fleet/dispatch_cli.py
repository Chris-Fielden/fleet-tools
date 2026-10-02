"""The `dispatch` command: --dry-run, --reconcile and --once."""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import dispatch as dp
from . import kaneo
from . import launch as lc
from . import ticket as tk


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"dispatch: {message}", file=sys.stderr)
        raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="dispatch")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="show what would be launched; changes nothing")
    mode.add_argument("--reconcile", action="store_true",
                      help="mark claimed tickets whose branch was merged as done")
    mode.add_argument("--once", action="store_true",
                      help="reconcile, then launch the first eligible ticket")
    return p


def _warn(message: str) -> None:
    print(f"dispatch: {message}", file=sys.stderr)


def _cmd_dry_run(settings: dict) -> int:
    for d in dp.decide(tk.list_tickets(), settings):
        print(d.line())
        if d.skip:
            dp.log("DECISION", f"{d.ticket['id']} skip: {d.skip}")
        else:
            dp.log("DECISION", f"{d.ticket['id']} would launch {d.agent} "
                               f"({d.model or 'default'}) in {d.worktree}")
    return 0


def _cmd_reconcile(settings: dict) -> int:
    for t in dp.reconcile(tk.list_tickets(), settings):
        print(f"{t['id']}  done ({t['branch']} merged)")
        kaneo.mirror_quietly(t, _warn)
    return 0


def _cmd_once(settings: dict) -> int:
    _cmd_reconcile(settings)
    decisions = dp.decide(tk.list_tickets(), settings)
    for d in decisions:
        if d.eligible:
            code, message = lc.launch(d, settings, _warn)
            print(message)
            return code
    if not decisions:
        print("nothing to launch: no open tickets")
        dp.log("DECISION", "nothing to launch: no open tickets")
    for d in decisions:
        print(f"nothing launched; {d.line()}")
        dp.log("DECISION", f"{d.ticket['id']} skip: {d.skip}")
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    who = tk.current_creator()
    if who != "human":
        print(f"dispatch: refusing to run inside an agent (running as {who}); "
              "only a human runs the dispatcher", file=sys.stderr)
        return 1
    try:
        settings = dp.load_settings()
        with dp.single_instance():
            if args.dry_run:
                return _cmd_dry_run(settings)
            return _cmd_once(settings) if args.once else _cmd_reconcile(settings)
    except dp.AlreadyRunning:
        print("dispatch: another dispatch is already running; nothing to do.")
        return 0
    except dp.SettingsError as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("dispatch: a git command took too long; try again", file=sys.stderr)
        return 1
    except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        return 1
