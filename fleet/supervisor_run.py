"""The supervisor, step C: run it from the watch cycle without ever blocking the cycle.

After a ticket's latest commit got a FAIL, the watch asks the supervisor (supervisor.py) to
classify the loop. The model call can take minutes, so it runs on ONE background worker
thread; the cycle only starts it and, a cycle later, collects the answer.

Threat model. The model reads text written by agents (notes, review items, commit subjects,
test names) and runs as the human's user. Its answer is parsed into a closed set
(PROGRESS, LOOP, SQUIRRELS) and can change exactly two things: one note on the ticket, and
whether a PROGRESS commit counts toward `fail_rounds_threshold`. It never launches, stops,
ships or edits anything, and its text is never run or parsed into a command or path.
The classification is kept in $FLEET_HOME/supervisor.json, which agents cannot write; it is
never read back from note text, so a forged "SUPERVISOR PROGRESS" note changes nothing.
Only the watch thread writes that file; the worker only returns a result through a queue.
Unclassified rounds (pending, failed, timed out, model down) count as before, so the old
threshold still protects when the supervisor is broken.
LOOP and SQUIRRELS are only recorded for now (their actions come in later steps).
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime
from pathlib import Path

from . import dispatch as dp
from . import supervisor as sv
from . import ticket as tk

ATTEMPTS = 3          # model calls tried for one commit before it is left unclassified
SETTLE = 2 * 60       # seconds after a FAIL before asking, if only one reviewer has reported
KEPT = 50             # classified commits kept per ticket (as tk.FAIL_COMMITS_MAX)


def state_path() -> Path:
    return tk.fleet_home() / "supervisor.json"


def load_state(warn=lambda m: None) -> dict:
    """{classified: {tid: {commit: CLASS}}, attempts: {tid: {commit: n}}}; bad entries are dropped."""
    state = {"classified": {}, "attempts": {}}
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
    return state


def save_state(state: dict) -> None:
    tk._write_atomic(state_path(), state)


def progress_commits(state: dict) -> dict[str, frozenset]:
    """Per ticket, the commits the supervisor classified PROGRESS: they do not count as FAIL rounds."""
    return {tid: frozenset(c for c, cls in per.items() if cls == "PROGRESS")
            for tid, per in state["classified"].items()}


def prune(state: dict, live: set) -> None:
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
