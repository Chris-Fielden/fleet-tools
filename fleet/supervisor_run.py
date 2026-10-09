"""The supervisor, step C: run it from the watch cycle without ever blocking the cycle.

After a ticket's latest commit got a FAIL, the watch asks the supervisor (supervisor.py) to
classify the loop. The model call can take minutes, so it runs on ONE background worker
thread; the cycle only starts it and, a cycle later, collects the answer.

Threat model. The model reads text written by agents (notes, review items, commit subjects,
test names) and runs as the human's user. Its answer is parsed into a closed set
(PROGRESS, LOOP, SQUIRRELS) and can change exactly two things: one note on the ticket, and
whether a PROGRESS commit counts toward `fail_rounds_threshold`; a LOOP also starts the
restart described under step D below. Its text is never run or parsed into a command or path.
The classification is kept in $FLEET_HOME/supervisor.json, which agents cannot write; it is
never read back from note text, so a forged "SUPERVISOR PROGRESS" note changes nothing.
Only the watch thread writes that file; the worker only returns a result through a queue.
Unclassified rounds (pending, failed, timed out, model down) count as before, so the old
threshold still protects when the supervisor is broken.

Step D, the LOOP action. A LOOP verdict writes the DESIGN CHANGE note and then, on the same
branch, replaces that one ticket's agent with a fresh session told to make the change first.
The steps are kept in the "pending" part of supervisor.json until they are done, so a crash
or a busy tab cannot lose them. Threat model: the verdict comes from a model reading
agent-written text, so a LOOP may only stop and restart THAT ticket's own agent (launch.
stop_agent: its own tab, never one that holds another agent) on THAT ticket's own branch.
It never ships, merges, edits another ticket, stops another agent, or sends text to a new
session that has not been scanned for secrets. A pending step is dropped if the ticket or
its branch moved on since it was recorded, and while one is pending the lost-agent relaunch
leaves the ticket alone (and launch.relaunch refuses a ticket whose agent is listed), so one
ticket never gets two agents. SQUIRRELS is only recorded for now (its action is a later step).
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime
from pathlib import Path

from . import dispatch as dp
from . import fresh
from . import launch as lc
from . import supervisor as sv
from . import ticket as tk

ATTEMPTS = 3          # model calls tried for one commit before it is left unclassified
SETTLE = 2 * 60       # seconds after a FAIL before asking, if only one reviewer has reported
KEPT = 50             # classified commits kept per ticket (as tk.FAIL_COMMITS_MAX)
RESTART_ATTEMPTS = 3  # cycles a LOOP restart is tried before it is given up
DESIGN_MAX = 3000     # characters of the model's DESIGN CHANGE that go into the new session
PENDING_MAX = 20      # pending restarts kept


def state_path() -> Path:
    return tk.fleet_home() / "supervisor.json"


def load_state(warn=lambda m: None) -> dict:
    """{classified: {tid: {commit: CLASS}}, attempts: {tid: {commit: n}}, pending: {tid: step}};
    bad entries are dropped. A pending step is {commit, design, reason, tries}."""
    state = {"classified": {}, "attempts": {}, "pending": {}}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, ValueError) as exc:
        warn(f"cannot read {state_path()} ({exc}); starting with an empty one")
        return state
    if not isinstance(data, dict):
        warn(f"{state_path()} has an unexpected shape; starting with an empty one")
        return state
    for key, ok in (("classified", lambda v: v in sv.CLASSES),
                    ("attempts", lambda v: isinstance(v, int) and not isinstance(v, bool))):
        raw = data.get(key)
        for tid, per in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(tid, str) and isinstance(per, dict):
                state[key][tid] = {c: v for c, v in per.items() if isinstance(c, str) and ok(v)}
    raw = data.get("pending")
    for tid, p in (raw.items() if isinstance(raw, dict) else []):
        step = _valid_step(tid, p)
        if step is None:
            warn(f"{state_path()}: dropped a malformed pending restart for {str(tid)[:20]!r}")
        elif len(state["pending"]) < PENDING_MAX:
            state["pending"][tid] = step
    return state


def _valid_step(tid, p) -> dict | None:
    """A pending restart read from disk, or None. Nothing in it is trusted: a damaged or
    edited file must not start anything odd."""
    if not (isinstance(tid, str) and tk.ID_RE.fullmatch(tid) and isinstance(p, dict)):
        return None
    commit, design, reason, tries = p.get("commit"), p.get("design"), p.get("reason"), p.get("tries")
    if not (isinstance(commit, str) and tk.COMMIT_RE.fullmatch(commit)
            and isinstance(design, str) and 20 <= len(design) <= DESIGN_MAX
            and isinstance(reason, str) and 1 <= len(reason) <= sv.REASON_MAX
            and isinstance(tries, int) and not isinstance(tries, bool) and 0 <= tries <= RESTART_ATTEMPTS):
        return None
    return {"commit": commit, "design": design, "reason": reason, "tries": tries}


def save_state(state: dict) -> None:
    tk._write_atomic(state_path(), state)


def progress_commits(state: dict) -> dict[str, frozenset]:
    """Per ticket, the commits the supervisor classified PROGRESS: they do not count as FAIL rounds."""
    return {tid: frozenset(c for c, cls in per.items() if cls == "PROGRESS")
            for tid, per in state["classified"].items()}


def pending_ids() -> frozenset:
    """Tickets with a LOOP restart under way. The lost-agent relaunch skips them: it would
    start a second agent next to the one this restart is about to start."""
    try:
        return frozenset(load_state()["pending"])
    except Exception as exc:   # the lost-agent check must not die on this; relaunch itself refuses a listed agent
        dp.log("SUPERVISOR_STATE", f"pending restarts could not be read ({type(exc).__name__})")
        return frozenset()


def prune(state: dict, live: set) -> None:
    state["pending"] = {k: v for k, v in state["pending"].items() if k in live}
    for key in ("classified", "attempts"):
        state[key] = {k: v for k, v in state[key].items() if k in live}


def _remember(state: dict, tid: str, key: str, commit: str, value) -> None:
    per = state[key].setdefault(tid, {})
    per.pop(commit, None)
    per[commit] = value
    while len(per) > KEPT:
        per.pop(next(iter(per)))


def due(t: dict, state: dict, now: datetime, busy_ids) -> bool:
    """Should the supervisor look at this ticket now? Claimed, latest verdict a FAIL whose
    commit is not yet classified, attempts left, not failing or held this cycle, and both
    reviewers in (or SETTLE seconds passed)."""
    v = t.get("verdict")
    if t["status"] != "claimed" or t["id"] in busy_ids or v is None or v["result"] != "FAIL":
        return False
    commit, tid = v["commit"], t["id"]
    if commit in state["classified"].get(tid, {}) or state["attempts"].get(tid, {}).get(commit, 0) >= ATTEMPTS:
        return False
    if len(v.get("reviews", {})) >= len(tk.REVIEW_KINDS):
        return True
    try:
        at = datetime.fromisoformat(v["at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError, KeyError):
        return False
    return (now - at).total_seconds() >= SETTLE


class Runner:
    """At most one model call in flight, on a daemon thread. The thread reads and returns;
    only the caller's thread writes state and notes."""

    def __init__(self):
        self.results: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, t: dict, repo: Path, base: str, tip: str, wt: Path) -> None:
        def work():
            try:
                reply, usage = sv.ask(t, repo, base, tip, wt, cwd=repo, stop=self.stop_flag)
                self.results.put((t["id"], tip, reply, usage, None))
            except Exception as exc:   # nothing may escape the thread; the cause is logged by the collector
                self.results.put((t["id"], tip, None, {}, f"{type(exc).__name__}: {exc}"))
        self.thread = threading.Thread(target=work, name="supervisor", daemon=True)
        self.thread.start()

    def drain(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out

    def stop(self, wait: float = 5) -> None:
        """Kill a running model call (the watch is shutting down) and wait for the thread."""
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(wait)


_RUNNER: Runner | None = None


def runner() -> Runner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = Runner()
    return _RUNNER


def shutdown() -> None:
    if _RUNNER is not None:
        _RUNNER.stop()


def reset() -> None:
    """Stop and forget the runner (tests)."""
    global _RUNNER
    shutdown()
    _RUNNER = None


def collect(state: dict, warn) -> bool:
    """Apply finished model calls. Returns True if `state` changed. A result is dropped as
    stale unless the ticket is still claimed with a FAIL on that same commit."""
    changed = False
    for tid, commit, reply, _usage, error in runner().drain():
        try:
            t = tk.load_ticket(tid)
        except (tk.TicketError, tk.TicketInvalid, OSError):
            continue
        v = t.get("verdict")
        if t["status"] != "claimed" or v is None or v["result"] != "FAIL" or v["commit"] != commit:
            dp.log("SUPERVISOR_STALE", f"{tid} {commit[:8]} dropped")
            continue
        if error is not None or reply is None:
            dp.log("SUPERVISOR_FAILED", f"{tid} {commit[:8]} {error}")
            warn(f"{tid}: supervisor could not classify {commit[:7]}: {error}")
            continue
        _remember(state, tid, "classified", commit, reply["classification"])
        changed = True
        dp.log("SUPERVISOR", f"{tid} {commit[:8]} {reply['classification']} tokens={sv.tokens(_usage)}")
        try:
            tk.add_note(tid, sv.note_text(reply, commit))
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{tid}: could not save the supervisor's note: {exc}")
            continue   # no note, no restart: the DESIGN CHANGE note is the record of why
        if reply["classification"] == "LOOP" and tid not in state["pending"]:
            state["pending"][tid] = {"commit": commit, "design": _clip_design(reply["design"]),
                                     "reason": reply["reason"], "tries": 0}
    return changed


def start_next(tickets: list[dict], state: dict, settings: dict, now: datetime, busy_ids, warn) -> bool:
    """Start the model on at most one due ticket (oldest first), if no call is running.
    The attempt is counted before it starts, so a crash cannot make it loop. Returns True
    if `state` changed."""
    run = runner()
    if run.busy():
        return False
    for t in sorted(tickets, key=tk.oldest_first_key):
        if not due(t, state, now, busy_ids):
            continue
        repo = settings["projects_dir"] / t["repo"]
        base = t.get("base_commit")
        if (not base or repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir()):
            continue
        wt = repo / ".worktrees" / t["branch"].removeprefix("task/")
        commit = t["verdict"]["commit"]
        n = state["attempts"].get(t["id"], {}).get(commit, 0) + 1
        _remember(state, t["id"], "attempts", commit, n)
        try:
            save_state(state)
        except OSError as exc:
            warn(f"cannot save {state_path()} ({exc}); not running the supervisor")
            return False
        run.start(t, repo, base, commit, wt)
        return True
    return False


# --- the LOOP action --------------------------------------------------------------

def _clip_design(text: str) -> str:
    return text if len(text) <= DESIGN_MAX else text[:DESIGN_MAX - 1] + "…"


def _tip(repo: Path, branch: str) -> str | None:
    out = dp._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _still_valid(t: dict, p: dict, repo: Path) -> bool:
    """The step was recorded for a ticket claimed with a FAIL on `commit`, the branch tip."""
    v = t.get("verdict")
    return (t["status"] == "claimed" and v is not None and v["result"] == "FAIL"
            and v["commit"] == p["commit"] and _tip(repo, t["branch"]) == p["commit"])


def restart_prompt(t: dict, p: dict, repo: Path, folder: Path) -> str | None:
    """The new session's whole starting text, scanned for secrets, or None when it cannot
    be made safe. The DESIGN CHANGE is model output built from agent-written text: if it
    trips the scan it is replaced by a pointer to the ticket and the whole text is scanned
    again; the text is never sent unscanned."""
    left_out = "(left out: it looked like it held a secret; read the ticket notes and .reviews/)"
    for design in (p["design"], left_out):
        prompt = fresh.build_prompt(t, repo, folder, design=design)
        if not tk.check_secrets("restart prompt", prompt):
            return prompt
    return None


def _give_up(tid: str, p: dict, state: dict, why: str, emit) -> None:
    state["pending"].pop(tid, None)
    dp.log("SUPERVISOR_GAVE_UP", f"{tid} {p['commit'][:8]} no fresh session: {why[:150]}")
    emit(f"{tid}  review loop: DESIGN CHANGE noted, but no fresh session could be started ({why}); "
         "the lost-agent check takes over")
    try:
        tk.add_note(tid, f"SUPERVISOR: the fresh session for the DESIGN CHANGE ({p['commit'][:7]}) could not "
                         f"be started: {why[:300]}"[:tk.NOTE_MAX])
    except (tk.TicketError, tk.TicketInvalid, OSError):
        pass


def continue_loops(state: dict, settings: dict, failing_ids, waiting_ids, emit, warn) -> bool:
    """Carry each pending LOOP restart as far as it goes this cycle. Returns True if `state`
    changed. Steps: check the ticket and branch are as recorded (else drop the step and stop
    nothing), build and scan the restart text, stop that ticket's own agent, start a fresh
    session on the same branch. A failure before the stop leaves the agent running; a failed
    start is retried next cycle, RESTART_ATTEMPTS times in all. A ticket in `failing_ids`
    (it has a failure record) loses its step; one in `waiting_ids` (just sent a note, held by
    the governor, being prepped) only waits: those are passing states, not reasons to give up."""
    changed = False
    for tid, p in sorted(state["pending"].items()):
        try:
            t = tk.load_ticket(tid)
        except (tk.TicketError, tk.TicketInvalid, OSError):
            state["pending"].pop(tid)
            changed = True
            continue
        repo = settings["projects_dir"] / t["repo"]
        folder = repo / ".worktrees" / t["branch"].removeprefix("task/")
        if (repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir()
                or not _still_valid(t, p, repo)):
            state["pending"].pop(tid)
            changed = True
            dp.log("SUPERVISOR_CANCELLED", f"{tid} {p['commit'][:8]} the ticket or branch moved on; restart dropped")
            continue
        if tid in failing_ids:
            state["pending"].pop(tid)   # it is being closed as failed: nothing to restart
            changed = True
            dp.log("SUPERVISOR_CANCELLED", f"{tid} {p['commit'][:8]} the ticket is failing; restart dropped")
            continue
        if tid in waiting_ids:
            continue
        if lc.governor.refusal(settings):
            continue   # a new session could not start: keep the old one running
        why = "the restart text did not pass the secret scan"
        try:
            prompt = restart_prompt(t, p, repo, folder)
        except Exception as exc:   # nothing is stopped yet
            prompt, why = None, f"the restart text could not be built ({type(exc).__name__})"
        if prompt is None:
            _give_up(tid, p, state, why, emit)
            changed = True
            continue
        stopped, why = lc.stop_agent(tid, settings)
        if stopped:
            code, message = lc.relaunch(t, settings, warn, prompt=prompt, why=" (LOOP: DESIGN CHANGE)")
            if code == 0 and "deferred" not in message:
                state["pending"].pop(tid)
                changed = True
                dp.log("SUPERVISOR_RESTART", f"{tid} {p['commit'][:8]} DESIGN CHANGE; fresh session started")
                emit(f"{tid}  review loop: DESIGN CHANGE noted, fresh session started on {t['branch']}")
                continue
            why = message
        p["tries"] += 1
        changed = True
        if p["tries"] >= RESTART_ATTEMPTS:
            _give_up(tid, p, state, why, emit)
        else:
            warn(f"{tid}: the DESIGN CHANGE restart is not finished ({why}); will retry next cycle")
    return changed
