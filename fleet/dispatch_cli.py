"""The `dispatch` command: --dry-run, --reconcile, --once and --watch."""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import dispatch as dp
from . import kaneo
from . import launch as lc
from . import notify
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


def _cycle(settings: dict, emit, warn, reserve=None, offer=None, retry_tabs=False) -> int:
    """The steps of one --once run; returns the exit code. `reserve()` is called
    before each launch and says whether it may go ahead (False: the daily limit
    is reached; it may raise OSError, which refuses the launch). `offer(ticket)`
    says whether to relaunch a claimed ticket whose agent is gone. `retry_tabs`
    (--watch) retries closing the tabs of tickets done on an earlier cycle."""
    code = 0
    _reconcile(settings, emit, warn)
    if retry_tabs:
        done = {t["id"] for t in tk.list_tickets() if t["status"] == "done"}
        kept = [i for i in lc.kept_tab_ids() if i in done]
        for ticket_id, reason in lc.retry_kept_tabs(kept, settings, warn):
            emit(f"{ticket_id}  tab closed: {reason}")
    for t in lc.finish_investigations(tk.list_tickets(), warn):
        emit(f"{t['id']}  investigation {t['status']}")
        kaneo.mirror_quietly(t, warn)
        notify.notify_quietly(t, "failed" if t["status"] == "failed" else None, warn)
    for t in lc.lost_agents(tk.list_tickets(), warn):
        emit(f"{t['id']}  claimed, but its agent is not running in herdr (the tab died?); "
             f"task folder {t['repo']}/.worktrees/{t['branch'].removeprefix('task/')}")
        dp.log("AGENT_LOST", f"{t['id']} {t['assignee']} not listed in herdr")
        if offer is not None and offer(t):
            relaunch_code, message = lc.relaunch(t, settings, warn)
            code = code or relaunch_code
            emit(message)
    tickets = tk.list_tickets()
    # Investigations are read-only and don't compete with build work: one of
    # each kind may start per run.
    started = False
    investigations = dp.decide_investigations(tickets, settings)
    for d in investigations:
        if d.eligible:
            if reserve is not None and not reserve():
                emit(f"{d.ticket['id']}  not launched: daily launch limit reached")
                break
            research_code, message = lc.launch_research(d, settings, warn)
            code = code or research_code
            emit(message)
            started = True
            break
    decisions = dp.decide(tickets, settings)
    for d in decisions:
        if d.eligible:
            if reserve is not None and not reserve():
                emit(f"{d.ticket['id']}  not launched: daily launch limit reached")
                return code
            build_code, message = lc.launch(d, settings, warn)
            emit(message)
            return code or build_code
    if started:
        return code
    if not decisions and not investigations:
        emit("nothing to launch: no open tickets")
        dp.log("DECISION", "nothing to launch: no open tickets")
    for d in decisions + investigations:
        emit(f"nothing launched; {d.line()}")
        dp.log("DECISION", f"{d.ticket['id']} skip: {d.skip}")
    return code


def _cmd_once(settings: dict) -> int:
    def offer(t: dict) -> bool:
        return _ask_yes_no(f"{t['id']}: start a new {t['assignee']} agent in the same task folder?")
    return _cycle(settings, print, _warn, offer=offer)


def _cmd_watch(settings: dict, clock: dp.Clock | None = None) -> int:
    """Run the --once steps every watch_interval_seconds until Ctrl+C, printing
    a line only when it was not printed by the cycle before. At most
    max_launches_per_day launches are attempted per UTC day; after that it only
    reconciles."""
    clock = clock or dp.Clock()
    interval, cap = settings["watch_interval_seconds"], settings["max_launches_per_day"]
    print(f"watching every {interval}s, at most {cap} launches a day (Ctrl+C to stop)", flush=True)
    lc._safe_log("WATCH_START", f"every {interval}s, max {cap} launches a day")
    previous: set[str] = set()
    announced = None

    def today() -> str:
        return clock.now().strftime("%Y-%m-%d")

    def reserve() -> bool:
        """Count a launch before it starts, so an interruption or a later error
        can never leave it uncounted. The day is read now, not when the cycle
        began: a cycle can cross midnight. A count that cannot be read or saved
        raises, which refuses the launch."""
        day = today()
        if dp.launches_today(day) >= cap:
            return False
        dp.add_launches(day)
        return True

    try:
        while True:
            lines: list[str] = []
            try:
                _cycle(settings, lines.append, lambda m: lines.append(f"warning: {m}"), reserve=reserve,
                       retry_tabs=True)
            except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
                lines.append(f"error: {exc}")
            for line in lines:
                if line not in previous:
                    print(line, flush=True)
            previous = set(lines)
            day = today()
            try:
                reached = dp.launches_today(day) >= cap
            except OSError:
                reached = False   # the error shows whenever a launch is refused for it
            if reached and announced != day:
                announced = day
                message = (f"daily launch limit reached ({cap} on {day}); still reconciling, "
                           "launching nothing until tomorrow (UTC)")
                print(message, flush=True)
                lc._safe_log("LAUNCH_CAP", message)
            clock.sleep(interval)
    except KeyboardInterrupt:
        print("\nwatch stopped", flush=True)
        lc._safe_log("WATCH_STOP", "interrupted")
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
