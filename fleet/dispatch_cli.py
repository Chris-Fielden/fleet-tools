"""The `dispatch` command: --dry-run, --reconcile, --once and --watch."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import dispatch as dp
from . import dupes
from . import governor
from . import startup_block
from . import kaneo
from . import launch as lc
from . import notify
from . import prep
from . import push_notes
from . import review_watch
from . import supervisor_run
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
    for d in (dp.decide(tickets, settings) + dp.decide_investigations(tickets, settings)
              + dp.decide_oncall(tickets, settings, running_agents(tickets, _warn))):
        print(d.line())
        if d.skip:
            d.log_skip()
        else:
            dp.log("DECISION", d.line())
    return 0


def _pull_board_priorities(emit, warn) -> None:
    """A human dragged a card into (or out of) Urgent on the Kaneo board: make the
    ticket urgent (or normal) and mirror it. Kaneo trouble is only a warning."""
    try:
        moves = list(kaneo.pull_priorities(tk.list_tickets()))
    except kaneo.NotConfigured:
        return
    except (kaneo.KaneoError, tk.TicketError, tk.TicketInvalid, OSError) as exc:
        warn(f"board priorities not read: {exc}")
        return
    for ticket_id, priority in moves:
        try:
            t = tk.set_priority(ticket_id, priority)
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{ticket_id} priority not set from the board: {exc}")
            continue
        emit(f"{ticket_id}  priority {priority} (moved on the board)")
        dp.log("PRIORITY", f"{ticket_id} {priority} from the board")
        kaneo.mirror_quietly(t, warn)


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


def running_agents(tickets: list[dict], warn) -> frozenset | None:
    """The lowercase ids of herdr agents that have not stopped, for the on-call slot;
    None if herdr cannot be asked (the slot is then busy: fail closed). Herdr is
    only asked when an on-call ticket is waiting."""
    if not any(t.get("oncall") and t["status"] == "open" for t in tickets):
        return frozenset()
    try:
        return frozenset(a.get("name") for a in lc.list_agents() if a.get("agent_status") not in ("idle", "done"))
    except lc.LaunchError as exc:
        warn(f"on-call launch deferred: cannot ask herdr which agents are running ({exc})")
        return None


def _prep_quietly(step, warn) -> None:
    """One step of the prep pass; a problem with it must not stop the cycle's launches."""
    try:
        step()
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired, lc.LaunchError) as exc:
        warn(f"prep pass: {exc}")


def _cycle(settings: dict, emit, warn, reserve=None, offer=None, retry_tabs=None, checks=None, clock=None) -> int:
    """The steps of one --once run; returns the exit code. `reserve()` is called
    before each launch and says whether it may go ahead (False: the daily limit
    is reached; it may raise OSError, which refuses the launch). `offer(ticket)`
    says whether to relaunch a claimed ticket whose agent is gone. `retry_tabs`
    (--watch; a launch.KeptTabs) retries closing the tabs of tickets done on an
    earlier cycle. `checks(emit, warn)` (--watch) moves finished tickets to review and sends
    the alert pings. `clock` is the time source for the prep pass (default: real time)."""
    code = 0
    clock = clock or dp.Clock()
    # Every cycle, whether or not anything is waiting: pause at the limit, stop at the hard limit,
    # lift after the reset.
    try:
        governor.evaluate(settings, clock.now(), emit, warn)
    except Exception as exc:   # noqa: BLE001 - the governor must never stop the rest of the cycle
        warn(f"usage governor: {exc}")
    if checks is not None:   # --watch only; before anything mirrors, or a mirror would undo the drag
        _pull_board_priorities(emit, warn)
    _reconcile(settings, emit, warn)
    if retry_tabs is not None:
        done = {t["id"] for t in tk.list_tickets() if t["status"] == "done"}
        kept = [i for i in retry_tabs.poll() if i in done]
        for ticket_id, reason in lc.retry_kept_tabs(kept, settings, warn):
            emit(f"{ticket_id}  tab closed: {reason}")
    prompted: set[str] = set()
    if checks is not None:   # --watch only
        # Notes go first, before anything below decides that an idle agent has finished.
        # (none while the usage governor has stopped the fleet: a note would wake an agent above the limit)
        prompted = push_notes.push([] if governor.status()["state"] == "stopped" else tk.list_tickets(),
                                   emit, warn, settings)
    for ticket_id, reason in lc.sweep_tabs(settings, warn):
        emit(f"{ticket_id}  stale tab swept: {reason}")
    for t in lc.finish_investigations([t for t in tk.list_tickets()
                                   if t["id"] not in prompted and t["id"] not in governor.held_ids()], warn):
        emit(f"{t['id']}  investigation {t['status']}")
        kaneo.mirror_quietly(t, warn)
        notify.notify_quietly(t, "failed" if t["status"] == "failed" else "investigated", warn)
    # held: stopped on purpose, the governor restarts them after the reset; looping: the supervisor's
    # LOOP restart is about to start their fresh session (a second agent would be a duplicate)
    stopped = governor.held_ids() | supervisor_run.pending_ids() | startup_block.blocked_ids()
    for t in lc.lost_agents([t for t in tk.list_tickets() if t["id"] not in stopped], warn):
        emit(f"{t['id']}  claimed, but its agent is not running in herdr (the tab died?); "
             f"task folder {t['repo']}/.worktrees/{t['branch'].removeprefix('task/')}")
        dp.log("AGENT_LOST", f"{t['id']} {t['assignee']} not listed in herdr")
        if offer is not None and offer(t):
            relaunch_code, message = lc.relaunch(t, settings, warn)
            code = code or relaunch_code
            emit(message)
    # Overnight queue: finish prep agents that stopped before the review pass looks at their
    # tickets, and start the next prep pass after it has moved new tickets into review.
    _prep_quietly(lambda: prep.finish(settings, clock.now(), emit, warn), warn)
    if checks is not None:
        checks(emit, warn, prompted)
        kaneo.refresh_usage(tk.list_tickets(), warn)   # --watch: each run's tokens reach the card
    _prep_quietly(lambda: prep.maybe_start(settings, clock.now(), emit, warn, reserve), warn)
    _prep_quietly(lambda: prep.flush_pings(settings, clock.now(), warn), warn)
    lines, held = dp.ensure_investigations(tk.list_tickets(), warn)
    for line in lines:
        emit(line)
    tickets = tk.list_tickets()
    paused = governor.refusal(settings, clock.now())
    if paused:   # before any reservation, so a refused launch is never counted against the day
        emit(f"nothing launched: {paused}")   # LAUNCH_PAUSED was logged when the pause began
        return code
    # The on-call agent has its own slot: it starts at once, whatever the normal
    # queue is doing, and never counts against the daily launch limit.
    running = running_agents(tickets, warn)
    for d in dp.decide_oncall(tickets, settings, running):
        if d.skip and not d.skip.startswith(dp.QUEUE_SKIPS):
            emit(f"{d.ticket['id']}  on-call not launched: {d.skip}")   # a blocker, not just a queue
            d.log_skip()
        if d.eligible:
            try:
                oncall_code, message = lc.launch_oncall(d, settings, warn)
            except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
                oncall_code, message = 1, f"{d.ticket['id']}  on-call launch failed: {exc}"
            code = code or oncall_code
            emit(message)
            if oncall_code == 0:
                push_notes.baseline(d.ticket)
            tickets = tk.list_tickets()   # the on-call ticket is claimed now: it holds its branch
            break
    # Investigations are read-only and don't compete with build work: one of
    # each kind may start per run.
    started = False
    investigations = dp.decide_investigations(tickets, settings)
    for d in investigations:
        if d.eligible:
            if reserve is not None and not reserve():
                d.skip = LIMIT_REACHED
                emit(f"{d.ticket['id']}  not launched: {d.skip}")
                break
            research_code, message = lc.launch_research(d, settings, warn)
            code = code or research_code
            emit(message)
            if research_code == 0:
                push_notes.baseline(d.ticket)
            started = True
            break
    decisions = dp.decide([t for t in tickets if t["id"] not in held], settings)
    _ping_unmet(decisions, emit, warn)
    for d in decisions:
        if d.eligible:
            if reserve is not None and not reserve():
                d.skip = LIMIT_REACHED
                emit(f"{d.ticket['id']}  not launched: {d.skip}")
                d.log_skip()
                for inv in investigations:   # this return skips the loop below
                    if inv.skip == LIMIT_REACHED:
                        inv.log_skip()
                return code
            build_code, message = lc.launch(d, settings, warn)
            emit(message)
            if build_code == 0:
                push_notes.baseline(d.ticket)
            return code or build_code
    if started:
        return code
    if not decisions and not investigations:
        emit("nothing to launch: no open tickets")
        dp.log("DECISION", "nothing to launch: no open tickets")
    for d in decisions + investigations:
        d.skip = d.skip or "eligible but not launched this run"
        emit(f"nothing launched; {d.line()}")
        d.log_skip()
    return code


def _ping_unmet(decisions, emit, warn) -> None:
    """One note and one needs-human ping per ticket held for an empty dependency branch.
    The note is the record: while it is on the ticket nothing more is sent."""
    for d in decisions:
        if not (d.skip and d.skip.startswith(dp.UNMET_SKIP)):
            continue
        t = d.ticket
        try:
            if any(n["text"].startswith(dp.HELD_NOTE) for n in tk.load_ticket(t["id"]).get("notes", [])):
                continue
            tk.add_note(t["id"], f"{dp.HELD_NOTE} {d.skip}. It launches by itself once the "
                                 "dependency has commits."[:tk.NOTE_MAX])
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{t['id']}: could not record the hold: {exc}")
            continue
        emit(f"{t['id']}  {d.skip}")
        notify.notify_quietly(t, "needs_human", warn, detail=d.skip[:300])


LIMIT_REACHED = "daily launch limit reached, max_launches_per_day is spent"


def dupes_path():
    return tk.fleet_home() / "dupes-checked.txt"


def check_dupes_daily(day: str, emit, warn) -> None:
    """Once per UTC day (kept in a file, so a restart does not repeat it): look for
    likely duplicate tickets and log DUPES_FOUND with the ids if there are any."""
    path = dupes_path()
    try:
        if path.read_text(encoding="utf-8").strip() == day:
            return
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        pass  # unreadable: check again and rewrite it
    try:
        found = dupes.pairs(tk.list_tickets())
        if found:
            ids = "; ".join(f"{p['a']}~{p['b']}" + (f" ({p['branch']})" if p["branch"] else "") for p in found)
            dp.log("DUPES_FOUND", f"{len(found)} likely duplicate pair(s): {ids}")
            emit(f"likely duplicate tickets: {ids} (run `ticket dupes`)")
        tk.refuse_real_store_in_tests(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(day + "\n", encoding="utf-8")
    except (tk.TicketError, OSError) as exc:
        warn(f"duplicate check: {exc}")


def _cmd_once(settings: dict) -> int:
    def offer(t: dict) -> bool:
        return _ask_yes_no(f"{t['id']}: start a new {t['assignee']} agent in the same task folder?")
    code = _cycle(settings, print, _warn, offer=offer)
    # A one-shot run has no next cycle: wait for the prep re-checks started above and finish the job.
    prep.wait_checks()
    _prep_quietly(lambda: prep.finish(settings, dp.Clock().now(), print, _warn), _warn)
    _prep_quietly(lambda: prep.flush_pings(settings, dp.Clock().now(), _warn), _warn)   # owed pings
    return code


def _reload_settings(settings: dict, reload, last_error: str | None) -> str | None:
    """Re-read the settings file at the start of a cycle. A good file replaces `settings`
    in place (SETTINGS_RELOADED lists the changed keys); a bad one keeps the last good
    values, and SETTINGS_INVALID is logged and pinged once per distinct problem.
    Returns the problem now being reported (None if the file is fine)."""
    try:
        fresh = reload()
    except dp.SettingsError as exc:
        problem = str(exc)
        if problem != last_error:
            lc._safe_log("SETTINGS_INVALID", f"keeping the last good settings: {problem}")
            try:
                notify.send_alert("settings_invalid", "Fleet settings invalid",
                                  f"dispatch kept the last good settings: {problem}")
            except notify.NotConfigured:
                pass
            except Exception as exc2:   # best effort: the log line is what counts
                lc._safe_log("SETTINGS_INVALID", f"could not send the ping ({type(exc2).__name__})")
        return problem
    changed = sorted(k for k in fresh if fresh[k] != settings.get(k))
    if changed:
        settings.clear()
        settings.update(fresh)
        lc._safe_log("SETTINGS_RELOADED", f"changed: {', '.join(changed)}")
    return None


def _write_heartbeat(beat: dict) -> None:
    """Write $FLEET_HOME/heartbeat.json atomically. Values are ids, counts,
    times and an exception class name only: never ticket text. A failed write
    is logged and never stops the watcher."""
    try:
        path = tk.fleet_home() / "heartbeat.json"
        tk.refuse_real_store_in_tests(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tk._write_atomic(path, beat)
    except (tk.TicketError, OSError) as exc:
        lc._safe_log("HEARTBEAT_FAILED", type(exc).__name__)


def _cmd_watch(settings: dict, clock: dp.Clock | None = None, head=None, reload=None) -> int:
    """Run the --once steps every watch_interval_seconds until Ctrl+C, printing
    a line only when it was not printed by the cycle before. At most
    max_launches_per_day launches are attempted per day (midnight in the `timezone`
    setting); after that it only reconciles. `reload()` (main passes
    dp.load_settings) is called at the start of every cycle to pick up changes to
    the settings file; `settings` is updated in place. If HEAD of the checkout this code runs from (`head()`) changes,
    it logs WATCH_RESTART and raises WatchRestart: main re-executes after
    releasing the lock, so the new code runs."""
    clock = clock or dp.Clock()
    interval, cap = settings["watch_interval_seconds"], settings["max_launches_per_day"]
    invalid = None
    print(f"watching every {interval}s, at most {cap} launches a day (Ctrl+C to stop)", flush=True)
    lc._safe_log("WATCH_START", f"every {interval}s, max {cap} launches a day")
    previous: set[str] = set()
    announced = None
    head = head or dp.code_head
    started_at = head()
    kept_tabs = lc.KeptTabs(clock.now)
    beat = {"pid": os.getpid(), "code_head": started_at, "started_at": tk._now(),
            "cycle_started": None, "cycle_finished": None, "cycles_ok": 0,
            "consecutive_errors": 0, "last_error": None, "progress_at": tk._now()}
    _write_heartbeat(beat)

    def today() -> str:
        return dp.local_day(clock.now(), settings["timezone"])

    def reserve() -> bool:
        """Count a launch before it starts, so an interruption or a later error
        can never leave it uncounted. The day is read now, not when the cycle
        began: a cycle can cross midnight. A count that cannot be read or saved
        raises, which refuses the launch."""
        day = today()
        if dp.launches_today(day) >= settings["max_launches_per_day"]:
            return False
        dp.add_launches(day)
        return True

    try:
        while True:
            lines: list[str] = []
            if reload is not None:
                invalid = _reload_settings(settings, reload, invalid)
            interval, cap = settings["watch_interval_seconds"], settings["max_launches_per_day"]
            beat["cycle_started"] = tk._now()
            failure = None
            try:
                def checks(emit, warn, skip=frozenset()):
                    review_watch.check(settings, clock.now(), emit, warn, skip)
                _cycle(settings, lines.append, lambda m: lines.append(f"warning: {m}"), reserve=reserve,
                       retry_tabs=kept_tabs, checks=checks, clock=clock)
            except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
                lines.append(f"error: {exc}")
                failure = exc
            except Exception as exc:   # one bug must be a counted error, not a dead watcher
                lines.append(f"error: cycle failed: {type(exc).__name__}")
                lc._safe_log("CYCLE_ERROR", type(exc).__name__)
                failure = exc
            if failure is None:
                beat["cycles_ok"] += 1
                beat["consecutive_errors"] = 0
            else:
                beat["consecutive_errors"] += 1
                beat["last_error"] = type(failure).__name__
            check_dupes_daily(today(), lines.append, lambda m: lines.append(f"warning: {m}"))
            for line in lines:
                if line not in previous:
                    print(line, flush=True)
            if set(lines) != previous:
                beat["progress_at"] = tk._now()
            previous = set(lines)
            beat["cycle_finished"] = tk._now()
            _write_heartbeat(beat)
            day = today()
            try:
                reached = dp.launches_today(day) >= cap
            except OSError:
                reached = False   # the error shows whenever a launch is refused for it
            if reached and announced != day:
                announced = day
                message = (f"daily launch limit reached ({cap} on {day}); still reconciling, "
                           f"launching nothing until midnight ({settings['timezone']})")
                print(message, flush=True)
                lc._safe_log("LAUNCH_CAP", message)
            clock.sleep(interval)
            now_head = head()
            if started_at and now_head and now_head != started_at:
                message = f"new code in the fleet-tools checkout ({started_at[:8]} -> {now_head[:8]}); restarting"
                print(message, flush=True)
                lc._safe_log("WATCH_RESTART", message)
                raise dp.WatchRestart()
    except KeyboardInterrupt:
        print("\nwatch stopped", flush=True)
        lc._safe_log("WATCH_STOP", "interrupted")
        return 0
    finally:
        supervisor_run.shutdown()   # no orphan model process, also before the restart's exec
        review_watch.abandon_rereviews()   # no ticket stays reserved under a PID exec keeps


def _exec_self(argv: list[str]) -> None:
    """Replace this process with a fresh run of the same command (tests swap
    this out). The lock is already released."""
    script = Path(__file__).resolve().parent.parent / "bin" / "dispatch"
    os.execv(sys.executable, [sys.executable, str(script), *argv])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
                return _cmd_watch(settings, reload=dp.load_settings)
            return _cmd_once(settings) if args.once else _cmd_reconcile(settings)
    except dp.WatchRestart:
        pass   # the lock is released by now: re-execute below
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
    try:
        _exec_self(argv)
    except OSError as exc:
        print(f"dispatch: could not restart: {exc}", file=sys.stderr)
        return 1
    return 0
