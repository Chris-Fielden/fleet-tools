"""The supervisor, step C: run it from the watch cycle without ever blocking the cycle.

After a ticket's latest commit got a FAIL, the watch asks the supervisor (supervisor.py) to
classify the loop. The model call can take minutes, so it runs on ONE background worker
thread; the cycle only starts it and, a cycle later, collects the answer.

Threat model. The model reads text written by agents (notes, review items, commit subjects,
test names) and runs as the human's user. Its answer is parsed into a closed set
(PROGRESS, LOOP, SQUIRRELS) and can change exactly two things: one note on the ticket, and
whether a PROGRESS commit counts toward `fail_rounds_threshold`; a LOOP also starts the
restart described under step D below, and SQUIRRELS the mark described under step F. Its text is never run or parsed into a command or path.
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
ticket never gets two agents.

Step F, the SQUIRRELS action. A SQUIRRELS verdict marks the ticket "ready with leftovers" on
its exact commit and files ONE leftovers ticket (proposed, so a human approves it) that holds
the full Codex review text. Threat model: the verdict comes from a model reading agent-written
text, so the mark is only a label on one ticket at one commit, kept in supervisor.json where
agents cannot write; the model's word is never enough. The dispatcher first checks, from the
stored ticket, git and the saved review files, that the tip has a Codex FAIL and a Claude PASS,
that the Codex review text can be read, and that the change's risk (stored level, every ticket
on the branch, and the diff) is not high: a high-risk change is refused, and so is one whose
risk cannot be worked out. The mark never ships, merges, closes, edits another ticket or
lowers a risk level; shipping with leftovers is step G and stays human-only. The mark is for
one commit: when the branch tip moves it no longer stands. A marked tip is not failed by the
backstop, the circles rule or the stopped-after-FAIL rule, but BLOCKED and STALL still apply.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch as dp
from . import fresh
from . import kaneo
from . import launch as lc
from . import review_history as rh
from . import risk as rk
from . import supervisor as sv
from . import ticket as tk

ATTEMPTS = 3          # model calls tried for one commit before it is left unclassified
SETTLE = 2 * 60       # seconds after a FAIL before asking, if only one reviewer has reported
KEPT = 50             # classified commits kept per ticket (as tk.FAIL_COMMITS_MAX)
RESTART_ATTEMPTS = 3  # cycles a LOOP restart is tried before it is given up
DESIGN_MAX = 3000     # characters of the model's DESIGN CHANGE that go into the new session
PENDING_MAX = 20      # pending restarts kept
LEFTOVERS_PARTS = 8   # notes of at most NOTE_MAX characters that carry the part of a review the body cannot hold
BACKSTOP_WAIT = 10 * 60   # seconds the 15-round backstop waits for the supervisor to judge a FAIL commit


def state_path() -> Path:
    return tk.fleet_home() / "supervisor.json"


def load_state(warn=lambda m: None) -> dict:
    """{classified: {tid: {commit: CLASS}}, attempts: {tid: {commit: n}}, pending: {tid: step},
    leftovers: {tid: mark}}; bad entries are dropped. A pending step is {commit, design, reason,
    tries}; a leftovers mark is {commit, reason, ticket, at}."""
    state = {"classified": {}, "attempts": {}, "pending": {}, "leftovers": {}}
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
    raw = data.get("leftovers")
    for tid, m in (raw.items() if isinstance(raw, dict) else []):
        mark = _valid_mark(tid, m)
        if mark is None:
            warn(f"{state_path()}: dropped a malformed leftovers mark for {str(tid)[:20]!r}")
        else:
            state["leftovers"][tid] = mark
    return state


def _valid_mark(tid, m) -> dict | None:
    """A leftovers mark read from disk, or None. The mark lets a human merge a failed review
    (step G), so a damaged or edited entry must never be read as one."""
    if not (isinstance(tid, str) and tk.ID_RE.fullmatch(tid) and isinstance(m, dict)):
        return None
    commit, reason, other, at = m.get("commit"), m.get("reason"), m.get("ticket"), m.get("at")
    if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
            and isinstance(reason, str) and 1 <= len(reason) <= sv.REASON_MAX
            and (other is None or (isinstance(other, str) and tk.ID_RE.fullmatch(other)))
            and isinstance(at, str) and len(at) <= 40):
        return None
    return {"commit": commit, "reason": reason, "ticket": other, "at": at}


def leftovers_for(state: dict, tid: str, commit: str) -> dict | None:
    """The ticket's leftovers mark, if it is for exactly this commit."""
    m = state.get("leftovers", {}).get(tid)
    return m if isinstance(m, dict) and m.get("commit") == commit else None


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
    state["leftovers"] = {k: v for k, v in state.get("leftovers", {}).items() if k in live}
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
    # Cursor reviews only a tip that already passed (BD-136), so a FAIL never waits for it.
    if all(k in v.get("reviews", {}) for k in rk.ALWAYS_REVIEW):
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


def collect(state: dict, warn, settings: dict | None = None) -> bool:
    """Apply finished model calls. Returns True if `state` changed. A result is dropped as
    stale unless the ticket is still claimed with a FAIL on that same commit. With `settings`
    a SQUIRRELS answer is acted on (see squirrels); without it, it is only recorded."""
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
        elif reply["classification"] == "SQUIRRELS" and settings is not None:
            try:
                squirrels(t, commit, reply["reason"], state, settings, warn)
            except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
                dp.log("SUPERVISOR_REJECTED", f"{tid} {commit[:8]} SQUIRRELS not acted on: {type(exc).__name__}")
                warn(f"{tid}: SQUIRRELS was not acted on: {exc}")
    return changed


def awaiting(tickets: list[dict], state: dict, now: datetime) -> frozenset:
    """Tickets whose latest FAIL commit the supervisor has not judged yet, for at most
    BACKSTOP_WAIT seconds after the verdict: the 15-round backstop waits for these, so SQUIRRELS
    is judged before it (EM-8 step E's documented order). Without a time limit a supervisor
    that cannot run would hold a ticket open for ever."""
    out = set()
    for t in tickets:
        v = t.get("verdict")
        if t["status"] != "claimed" or v is None or v["result"] != "FAIL":
            continue
        tid, commit = t["id"], v["commit"]
        if commit in state["classified"].get(tid, {}):
            continue
        if state["attempts"].get(tid, {}).get(commit, 0) >= ATTEMPTS and not runner().busy():
            continue
        try:
            at = datetime.fromisoformat(v["at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            continue
        if 0 <= (now - at).total_seconds() < BACKSTOP_WAIT:
            out.add(tid)
    return frozenset(out)


# --- the SQUIRRELS action ---------------------------------------------------------

def _branch_level(t: dict, repo: Path) -> str | None:
    """The change's risk level as the merge gate sees it (stored level, every ticket on the
    branch, the diff against the base branch), or None when it cannot be worked out."""
    try:
        base = "main"
        try:
            rk._git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main")
        except rk.RiskError:
            base = "master"
        rev_range = f"{base}...{t['branch']}"
        rules = rk.rules_for_range(repo, rev_range)
        _, diff_level = rk.classify_all(rk.changed_files(repo, rev_range), rules)
        stored, _ = rk.level_for_branch(tk.list_tickets(), t["branch"], rules, t["repo"])
        return rk.higher(rk.higher(stored, rk.stored_level(t, rules)), diff_level)
    except (rk.RiskError, tk.TicketError, tk.TicketInvalid, OSError):
        return None


def _codex_review(wt: Path, tip: str) -> tuple[Path, str] | None:
    """(file, full text) of the newest saved Codex review of exactly `tip`, or None. The task
    folder is agent-writable: links are never followed out of it."""
    found = None
    for path in sorted(rh._review_files(wt), key=lambda p: p.stat().st_mtime):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        info = rk.parse_review(text)
        if info["commit"] == tip and info["kind"] == "codex":
            found = (path, text, info["verdict"])
    if found is None or found[2] != "FAIL" or not found[1].strip():
        return None
    return found[0], found[1]


def squirrels_problem(t: dict, repo: Path, wt: Path, tip: str) -> tuple[str | None, Path | None, str]:
    """(why SQUIRRELS cannot be acted on or None, Codex review file, its text). Facts from
    the stored ticket, git and the saved reviews: the model's word counts for nothing here."""
    v = t.get("verdict")
    if t["status"] != "claimed" or v is None or v["commit"] != tip or _tip(repo, t["branch"]) != tip:
        return "the ticket is not claimed with its latest verdict on the branch tip", None, ""
    codex, claude = v["reviews"].get("codex"), v["reviews"].get("claude")
    if codex is None or codex["result"] != "FAIL":
        return "there is no Codex FAIL on the tip", None, ""
    if claude is None or claude["result"] != "PASS":
        return "the Claude review did not pass the tip", None, ""
    level = _branch_level(t, repo)
    if level is None:
        return "the risk level of the change could not be worked out", None, ""
    if level == "high":
        return "the change is high risk: a failed review is not marked ready with leftovers", None, ""
    review = _codex_review(wt, tip)
    if review is None:
        return "the saved Codex review of the tip could not be read", None, ""
    return None, review[0], review[1]


def _chunks(text: str) -> list[str]:
    return [text[i:i + tk.NOTE_MAX] for i in range(0, len(text), tk.NOTE_MAX)][:LEFTOVERS_PARTS]


def file_leftovers(t: dict, tip: str, review_file: Path, review_text: str, warn) -> dict:
    """The ONE leftovers ticket of `t`, holding the full Codex review text of `tip` (what the
    body cannot hold goes into notes on it). An active one filed earlier, by this action or by
    `review.sh --propose-leftovers`, is reused: the text goes into its notes, once per commit.
    Proposed, not open: it must start after the merge. Repeating it files nothing new."""
    heading = f"Full Codex review of commit {tip[:7]} (FAIL):"
    if tk.check_secrets("review", review_text):   # never copy what looks like a secret into a ticket
        review_text = ("(not copied: the review text looked like it held a secret; read "
                       f"{review_file.name} in the task folder of {t['id']})")
    existing = tk.leftovers_ticket(t)
    if existing is not None:
        if not any(n["text"].startswith(heading) for n in existing.get("notes", [])):
            for part in _chunks(f"{heading}\n{review_text}"):
                tk.add_note(existing["id"], part)
        return tk.load_ticket(existing["id"])
    head = (f"Leftovers from {t['id']} ({sv._clip(t['title'], 100)}). The supervisor judged the Codex "
            f"review of commit {tip[:7]} below the MUST FIX bar and the Claude review passed that commit. "
            f"{heading}\n")
    room = tk.BODY_MAX - len(head)
    new = tk.create_leftovers_ticket(t, f"Leftovers from {t['id']}", head + review_text[:room], priority="normal")
    for part in _chunks(review_text[room:]):
        try:
            tk.add_note(new["id"], part)
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{new['id']}: could not save part of the review text: {exc}")
            break
    return new


def squirrels(t: dict, tip: str, reason: str, state: dict, settings: dict, warn) -> bool:
    """Mark ticket `t` ready with leftovers at `tip` and file the leftovers ticket. Returns True
    when it was done; a refusal (and why) is logged and noted on the ticket. Safe to repeat."""
    tid = t["id"]
    repo = settings["projects_dir"] / t["repo"]
    wt = repo / ".worktrees" / t["branch"].removeprefix("task/")
    if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
        problem, review_file, text = "the repo is not under the projects folder", None, ""
    else:
        problem, review_file, text = squirrels_problem(t, repo, wt, tip)
    if problem is not None:
        dp.log("SUPERVISOR_REJECTED", f"{tid} {tip[:8]} SQUIRRELS not acted on: {problem}")
        try:
            tk.add_note(tid, f"SUPERVISOR: SQUIRRELS was not acted on for {tip[:7]}: {problem}.")
        except (tk.TicketError, tk.TicketInvalid, OSError):
            pass
        return False
    new = file_leftovers(t, tip, review_file, text, warn)
    kaneo.mirror_quietly(new, warn)
    state.setdefault("leftovers", {})[tid] = {"commit": tip, "reason": reason, "ticket": new["id"],
                                              "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    save_state(state)   # the mark is on disk before anything is said about it
    dp.log("LEFTOVERS", f"{tid} {tip[:8]} marked ready with leftovers; review text in {new['id']}")
    try:
        tk.add_note(tid, f"SUPERVISOR: ready with leftovers ({tip[:7]}): {reason}. The full Codex review is "
                         f"in {new['id']}. The mark covers this commit only; it does not ship or merge anything."[:tk.NOTE_MAX])
    except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
        warn(f"{tid}: could not save the leftovers note: {exc}")
    return True


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
                or t.get("assignee") not in tk.AGENTS or not _still_valid(t, p, repo)):
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
