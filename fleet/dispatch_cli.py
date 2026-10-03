"""The `dispatch` command: --dry-run, --reconcile, --once and --watch."""

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
    mode.add_argument("--watch", action="store_true",
                      help="repeat --once every watch_interval_seconds until Ctrl+C")
    return p


def _warn(message: str) -> None:
    print(f"dispatch: {message}", file=sys.stderr)


def _cmd_dry_run(settings: dict) -> int:
    tickets = tk.list_tickets()
    for d in dp.decide(tickets, settings) + dp.decide_investigations(tickets, settings):
        print(d.line())
        if d.skip:
            dp.log("DECISION", f"{d.ticket['id']} skip: {d.skip}")
        else:
            dp.log("DECISION", d.line())
    return 0


def _reconcile(settings: dict, emit, warn) -> None:
    """Mark merged tickets done, then close the tab of each if its agent has
    stopped."""
    for t in dp.reconcile(tk.list_tickets(), settings):
        emit(f"{t['id']}  done ({t['branch']} merged)")
        kaneo.mirror_quietly(t, warn)
        closed, reason = lc.close_finished_tab(t["id"], settings)
        emit(f"{t['id']}  tab {'closed' if closed else 'kept'}: {reason}")


def _cmd_reconcile(settings: dict) -> int:
    _reconcile(settings, print, _warn)
    return 0


def _ask_yes_no(question: str) -> bool:
    """Ask on the terminal; no terminal means no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _cycle(settings: dict, emit, warn, budget: int | None = None, offer=None) -> tuple[int, int]:
    """The steps of one --once run. `budget` is how many launches are still
    allowed (None: no limit; 0: reconcile only). `offer(ticket)` says whether
    to relaunch a claimed ticket whose agent is gone. Returns (exit code,
    launches attempted)."""
    _reconcile(settings, emit, warn)
    for t in lc.finish_investigations(tk.list_tickets(), warn):
        emit(f"{t['id']}  investigation {t['status']}")
        kaneo.mirror_quietly(t, warn)
    for t in lc.lost_agents(tk.list_tickets(), warn):
        emit(f"{t['id']}  claimed, but its agent is not running in herdr (the tab died?); "
             f"task folder {t['repo']}/.worktrees/{t['branch'].removeprefix('task/')}")
        dp.log("AGENT_LOST", f"{t['id']} {t['assignee']} not listed in herdr")
        if offer is not None and offer(t):
            code, message = lc.relaunch(t, settings, warn)
            emit(message)
    if budget == 0:
        return 0, 0
    tickets = tk.list_tickets()
    # Investigations are read-only and don't compete with build work: one of
    # each kind may start per run.
    code = 0
    attempts = 0
    started = False
    investigations = dp.decide_investigations(tickets, settings)
    for d in investigations:
        if d.eligible:
            code, message = lc.launch_research(d, settings, warn)
            emit(message)
            attempts += 1
            started = True
            break
    decisions = dp.decide(tickets, settings)
    for d in decisions:
        if d.eligible:
            if budget is not None and attempts >= budget:
                emit(f"{d.ticket['id']}  not launched: daily launch limit reached")
                return code, attempts
            build_code, message = lc.launch(d, settings, warn)
            emit(message)
            return code or build_code, attempts + 1
    if started:
        return code, attempts
    if not decisions and not investigations:
        emit("nothing to launch: no open tickets")
        dp.log("DECISION", "nothing to launch: no open tickets")
    for d in decisions + investigations:
        emit(f"nothing launched; {d.line()}")
        dp.log("DECISION", f"{d.ticket['id']} skip: {d.skip}")
    return 0, attempts


def _cmd_once(settings: dict) -> int:
    def offer(t: dict) -> bool:
        return _ask_yes_no(f"{t['id']}: start a new {t['assignee']} agent in the same task folder?")
    return _cycle(settings, print, _warn, offer=offer)[0]


def _cmd_watch(settings: dict, clock: dp.Clock | None = None) -> int:
    """Run the --once steps every watch_interval_seconds until Ctrl+C, printing
    a line only when it was not printed by the cycle before. At most
    max_launches_per_day launches are attempted per UTC day; after that it only
    reconciles."""
    clock = clock or dp.Clock()
    interval, cap = settings["watch_interval_seconds"], settings["max_launches_per_day"]
    print(f"watching every {interval}s, at most {cap} launches a day (Ctrl+C to stop)", flush=True)
    dp.log("WATCH_START", f"every {interval}s, max {cap} launches a day")
    previous: set[str] = set()
    announced = None
    try:
        while True:
            lines: list[str] = []
            day = clock.now().strftime("%Y-%m-%d")
            reached = False
            try:
                try:
                    used = dp.launches_today(day)
                except OSError as exc:
                    used = cap  # cannot tell how many launched: launch nothing
                    lines.append(f"error: {exc}; not launching")
                _, attempts = _cycle(settings, lines.append, lambda m: lines.append(f"warning: {m}"),
                                     budget=max(cap - used, 0))
                if attempts:
                    dp.add_launches(day, attempts)
                reached = used + attempts >= cap and not any(ln.startswith("error:") for ln in lines)
            except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
                lines.append(f"error: {exc}")
            for line in lines:
                if line not in previous:
                    print(line, flush=True)
            previous = set(lines)
            if reached and announced != day:
                announced = day
                message = (f"daily launch limit reached ({cap} on {day}); still reconciling, "
                           "launching nothing until tomorrow (UTC)")
                print(message, flush=True)
                dp.log("LAUNCH_CAP", message)
            clock.sleep(interval)
    except KeyboardInterrupt:
        print("\nwatch stopped", flush=True)
        dp.log("WATCH_STOP", "interrupted")
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
            if args.watch:
                return _cmd_watch(settings)
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
