"""What `dispatch --watch` does about claimed tickets each cycle.

* A claimed build or fix ticket becomes READY, and moves to status `review`
  (Kaneo: In Review), when all hold: its recorded verdict is a PASS that meets
  the merge gate for the branch's exact tip, a SUMMARY note was recorded after
  that review, and its herdr agent is idle or done. One "ready to ship" ping.
  If that stops being true (the tip moved, the agent is busy again) it goes
  back to claimed. The ping waits for the prep pass that covers the ticket (prep.holds_ping):
  a pass's one "N ready to ship, list ready" ping speaks for its tickets; a ticket no pass
  will touch is pinged at once.
* A claimed ticket TRULY FAILS when its agent sat idle on a failed commit for
  5 minutes, it has `fail_rounds_threshold` (default 8) FAIL verdicts on different commits, its agent was blocked
  for 20 minutes, or it had no activity for 45 minutes while its agent was idle or waiting. It is then closed as
  failed and an on-call fix ticket is filed (except an agent idle with no commit since the claim:
  nudged once after GIVE_UP_AFTER, failed GIVE_UP_AFTER later with a needs_human ping, no on-call) (launched by the dispatch cycle in
  its own slot); see _fail_ticket. A mid-loop FAIL verdict never pings.
* A commit landing after a PASS (the tip is past the commit the PASS is on, and its agent has
  stopped) clears the ready state and re-runs check.sh and the reviews on that exact tip in the
  background; see "a commit after the PASS" below. It ends in the usual ready ping, the usual
  FAIL handling, or one "needs a human" ping: never a ticket left claimed with no action.
* The only ping here besides "ready to ship" is a review that ran but could
  not be recorded; failures ping once, low ("on-call is on it" or "needs a
  human").

A true failure is made durable first: before anything else is done, a recovery
record (`$FLEET_HOME/pending-recovery/<ID>.json`: ticket id, failed commit, reason,
reviewer, rounds) is written atomically. Every cycle processes the records
idempotently: close the ticket if it is not closed, file the on-call ticket if none
exists for that parent, add the notes, queue the ping. The record is deleted only
when all of that is done, and a ticket with a record is never skipped. A step that
fails just leaves the record for the next cycle.

Every state is decided from the stored ticket (verdict, notes), the git branch,
the review files in the task folder, herdr and the dispatcher log. The agent's
own words are never parsed: only that a note exists, and when it was saved.
Nothing is pinged twice for the same commit (or the same stall or block): what
was sent is remembered in $FLEET_HOME/review-watch.json. Plain Python.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import dialog
from . import dispatch as dp
from . import fresh
from . import governor
from . import kaneo
from . import launch as lc
from . import notify
from . import prep
from . import risk as rk
from . import supervisor_run as sr
from . import ticket as tk

BLOCKED_AFTER = 20 * 60   # seconds an agent waits for input before the ticket fails
STALL_AFTER = 45 * 60     # seconds with no commit, note or log output before the ticket fails
STALL_STATES = ("idle", "done", "blocked", "waiting")   # herdr states in which a stall counts; never "working"
GIVE_UP_AFTER = 5 * 60    # seconds an agent sits idle with no commit (and again after one nudge) before the ticket fails
NO_COMMIT_REASON = "agent stopped without a commit"
NUDGE_TEXT = ("You stopped without making a commit. Commit your work now, or write a ticket note "
              "(ticket note <ID> TEXT) explaining why you stopped. Never ask questions in chat: "
              "write them as a ticket note.")
STOPPED_AFTER = 5 * 60   # seconds an agent sits idle on a failed tip before the ticket fails
UNRECORDED_GRACE = 2 * 60  # seconds review.sh gets to record a review it just saved
LOG_TAIL = 256 * 1024
BACKSTOP_ROUNDS = 15      # review rounds (commits with a recorded review) after which a still-FAILing ticket is closed
NEEDS_HUMAN_RE = re.compile(r"\bneeds?\s+(?:a\s+)?human\b|\bneeds?\s+the\s+real\s+(?:ticket\s+)?store\b", re.I)
BACKSTOP_REASON = "review-round backstop"   # starts the reason of a backstop failure
ANALYSIS_MAX = 600        # characters of the supervisor's latest note kept in the backstop's close note
MUST_FIX_PART = 500       # characters of a review's MUST FIX section per part of the on-call body
MUST_FIX_PARTS = 6        # parts kept per review; the context before the spec is clipped to leave the spec whole
# Dispatcher log kinds that show a ticket's work moving. (RECONCILE_SKIP and
# DECISION lines repeat every cycle and say nothing about the agent.)
ACTIVITY_LOG_KINDS = ("LAUNCHED", "RELAUNCHED", "REVIEW_READY", "REVIEW_BACK")
FAILURE_EVENTS = ("oncall", "needs_human", "leftovers")   # pings kept until sent, once per ticket
LAUNCH_FAILED_NOTE = "Launch attempt failed:"   # written by launch.launch_oncall
LAUNCH_ATTEMPTS = 3       # failed launches of one on-call ticket before it is given up
FAILURE_KEYS_KEPT = 500   # sent-keys of closed tickets' failure pings; the oldest go first
READY_REMINDER_AFTER = 6 * 3600   # seconds a ticket sits ready (In Review) before its one reminder ping
LEFTOVERS_PREFIX = "SHOULD FIX leftovers from"  # saved by `ticket review-result`, not a summary


def state_path() -> Path:
    return tk.fleet_home() / "review-watch.json"


def _load_state(warn) -> dict:
    state = {"sent": [], "blocked_since": {}, "stopped_since": {}, "seen": {}, "pending": [], "fresh": {},
             "dialog_pinged": {}, "ready_since": {}, "unrecorded_seen": {}, "nudged": {}}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("sent"), list)
                and isinstance(data.get("blocked_since"), dict) and isinstance(data.get("seen"), dict)
                and isinstance(data.get("stopped_since", {}), dict)
                and isinstance(data.get("pending", []), list)):
            state.update(sent=[k for k in data["sent"] if isinstance(k, str)],
                         blocked_since=data["blocked_since"], seen=data["seen"],
                         stopped_since=data.get("stopped_since", {}),
                         dialog_pinged={k: v for k, v in (data["dialog_pinged"]
                                                          if isinstance(data.get("dialog_pinged"), dict) else {}).items()
                                        if isinstance(k, str) and isinstance(v, str)},
                         ready_since={k: v for k, v in (data["ready_since"]
                                                        if isinstance(data.get("ready_since"), dict) else {}).items()
                                      if isinstance(k, str) and isinstance(v, str)},
                         nudged={k: v for k, v in (data["nudged"] if isinstance(data.get("nudged"), dict)
                                                   else {}).items() if isinstance(k, str) and isinstance(v, str)},
                         unrecorded_seen={k: v for k, v in (data["unrecorded_seen"] if isinstance(
                             data.get("unrecorded_seen"), dict) else {}).items()
                                          if isinstance(k, str) and type(v) is int and v > 0},
                         fresh={k: v for k, v in (data["fresh"] if isinstance(data.get("fresh"), dict) else {}).items()
                                if isinstance(k, str) and isinstance(v, str)},
                         pending=[p for p in data.get("pending", []) if isinstance(p, dict)
                                  and isinstance(p.get("id"), str) and p.get("event") in FAILURE_EVENTS
                                  and isinstance(p.get("detail"), (str, type(None)))
                                  and isinstance(p.get("commit"), (str, type(None)))])
        else:
            warn(f"{state_path()} has an unexpected shape; starting with an empty one")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        warn(f"cannot read {state_path()} ({exc}); starting with an empty one")
    return state


def _save_state(state: dict) -> None:
    tk._write_atomic(state_path(), state)


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# --- facts about one ticket --------------------------------------------------

def _tip(repo: Path, branch: str) -> str | None:
    out = dp._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def has_summary_after_review(t: dict) -> bool:
    """A note by the ticket's agent saved at or after the verdict was recorded.
    Only its existence and time count, never what it says."""
    v = t["verdict"]
    if v is None:
        return False
    # The prep pass (the dispatcher, so a human) re-checks a branch it changed and says so
    # in a note; that stands in for the agent's summary of the new tip.
    return any(n["at"] >= v["at"] and not n["text"].startswith(LEFTOVERS_PREFIX)
               and (n["by"] == t.get("assignee") or (n["by"] == "human" and n["text"].startswith(prep.PASS_NOTE_PREFIX)))
               for n in t.get("notes", []))


def passed_for_tip(t: dict, tip: str) -> bool:
    """A recorded PASS that satisfies the merge gate's review rules for this exact tip."""
    v = t["verdict"]
    return (v is not None and v["result"] == "PASS" and v["commit"] == tip
            and kaneo.ready_to_ship(t))


def moved_past_pass(t: dict, tip: str) -> bool:
    """The recorded verdict is a PASS on a commit the branch is no longer at (a commit landed
    after it, or the branch was rewritten): that verdict says nothing about this tip."""
    v = t.get("verdict")
    return v is not None and v["result"] == "PASS" and v["commit"] != tip


def vouched_by_rereview(t: dict) -> bool:
    """The dispatcher's own re-review note stands after the recorded verdict: the tip was checked
    and passed by it, so no agent has to be around to say so."""
    v = t["verdict"]
    return v is not None and any(
        n["by"] == "human" and n["at"] >= v["at"] and n["text"].startswith(prep.PASS_NOTE_PREFIX)
        and REREVIEW_NOTE in n["text"] for n in t.get("notes", []))


def _last_activity(t: dict, repo: Path, tip: str, agent: dict | None, state: dict,
                   now: datetime) -> datetime:
    """The latest sign of life: ticket change (claim, note, verdict), commit,
    dispatcher log line for it, or a change in what herdr reports for the agent."""
    times = [_parse(t["updated_at"])]
    times += [_parse(n["at"]) for n in t.get("notes", [])]
    out = dp._git(repo, "log", "-1", "--format=%ct", tip)
    if out.returncode == 0 and out.stdout.strip().isdigit():
        times.append(datetime.fromtimestamp(int(out.stdout.strip()), timezone.utc))
    pattern = re.compile(rf"\b{re.escape(t['id'])}\b")
    try:
        with open(dp.log_path(), "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - LOG_TAIL))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[1] in ACTIVITY_LOG_KINDS and pattern.search(parts[2]):
            times.append(_parse(parts[0]))
    if agent is not None:
        sig = [agent.get(k) for k in ("revision", "state_change_seq", "completion_seq", "agent_status")]
        seen = state["seen"].get(t["id"])
        if not isinstance(seen, dict) or seen.get("sig") != sig:
            seen = {"sig": sig, "at": _stamp(now)}
            state["seen"][t["id"]] = seen
        times.append(_parse(seen["at"]))
    return max(x for x in times if x is not None)


# --- pings -------------------------------------------------------------------

def _ping(t: dict, event: str, key: str, state: dict, warn, detail: str | None = None) -> bool:
    """Send once per key. A send that failed is tried again on the next cycle."""
    if key in state["sent"]:
        return False
    if notify.notify_quietly(t, event, warn, detail):
        state["sent"].append(key)
        return True
    return False


def _ready_ping(t: dict, tip: str, settings: dict, now: datetime, state: dict, warn) -> None:
    """The "ready to ship" ping, once the prep pass that will cover this ticket is out of the way:
    while an agent holds the ticket, a pass is due or running, or the pass already covered it
    (its one "N ready to ship, list ready" ping speaks for it), nothing is sent here. Asked every
    cycle, so a held ping goes out as soon as no pass will touch the ticket."""
    try:
        if prep.holds_ping(t, tk.list_tickets(), settings, now):
            return
    except OSError as exc:
        warn(f"prep state unreadable ({exc}); the ready ping of {t['id']} waits")
        return
    _ping(t, "ship", f"{t['id']}:ship:{tip}", state, warn)  # a send that failed is retried


def _ready_reminders(tickets: list[dict], state: dict, now: datetime, warn) -> None:
    """One low "still waiting for you" ping for a ticket that has sat ready (In Review) for
    READY_REMINDER_AFTER after its "ready to ship" ping went out (the clock starts when this
    watcher first sees that ping sent; a ticket held for or covered by a prep pass has no such
    ping and gets no reminder). A ticket that leaves the ready state is forgotten, so a return
    starts again (and may ping again)."""
    ready = {t["id"]: t for t in tickets if t["status"] == "review"
             and any(k.startswith(f"{t['id']}:ship:") for k in state["sent"])}
    for tid in [i for i in state["ready_since"] if i not in ready]:
        del state["ready_since"][tid]
        state["sent"] = [k for k in state["sent"] if k != f"{tid}:ready_reminder"]
    for tid, t in ready.items():
        since = _parse(state["ready_since"].setdefault(tid, _stamp(now)))
        if since is not None and (now - since).total_seconds() >= READY_REMINDER_AFTER:
            _ping(t, "ready_reminder", f"{tid}:ready_reminder", state, warn, "ready and not shipped yet")


def _queue_ping(ticket_id: str, event: str, state: dict, warn, detail: str | None = None,
                commit: str | None = None) -> None:
    """Queue the one ping of a failure (event oncall or needs_human, or leftovers: the ticket
    was marked ready with leftovers at `commit`), once per ticket
    and event, and save it to disk at once: it is queued BEFORE the ticket is closed,
    so an interruption cannot lose it. `_retry_pending` sends it, and keeps trying
    every cycle (even though the closed ticket is no longer visited) until it
    goes through."""
    if f"{ticket_id}:{event}" in state["sent"] or any(
            p["id"] == ticket_id and p["event"] == event for p in state["pending"]):
        return
    state["pending"].append({"id": ticket_id, "event": event, "detail": detail, "commit": commit})
    try:
        _save_state(state)
    except OSError:
        state["pending"].pop()
        raise   # nothing is closed until the ping is safely on disk


def _leftovers_stands(ticket: dict, commit: str | None, settings: dict | None, warn) -> bool | None:
    """Is a queued "ready with leftovers" ping still true? It is only while the ticket is still
    claimed and its branch tip is the marked commit. None: cannot be checked here (no settings)."""
    if settings is None:
        return None
    repo = settings["projects_dir"] / ticket["repo"]
    tip = _tip(repo, ticket["branch"]) if repo.is_dir() else None
    return (commit is not None and ticket["status"] in ("claimed", "review") and tip == commit
            and sr.leftovers_for(sr.load_state(warn), ticket["id"], commit) is not None)


def _retry_pending(state: dict, warn, settings: dict | None = None) -> None:
    keep = []
    for p in state["pending"]:
        try:
            ticket = tk.load_ticket(p["id"])
        except (tk.TicketError, tk.TicketInvalid):
            continue   # gone: nothing to tell anyone about
        if p["event"] == "leftovers":
            standing = _leftovers_stands(ticket, p.get("commit"), settings, warn)
            if standing is None:
                keep.append(p)
                continue
            if not standing:   # the branch tip moved: a ping about the old commit would be false
                dp.log("LEFTOVERS_PING_DROPPED", f"{p['id']} the branch moved on; ping dropped")
                continue
        if notify.notify_quietly(ticket, p["event"], warn, p.get("detail")):
            state["sent"].append(f"{p['id']}:{p['event']}")
        else:
            keep.append(p)
    state["pending"] = keep


def _unrecorded_pings(t: dict, repo: Path, tip: str, state: dict, now: datetime, emit, warn,
                      cycles: int = 3) -> None:
    """A review that was saved for this tip but never reached the ticket. The ping goes out only
    when the same commit and kind has been seen unrecorded on `cycles` cycles (the first sight
    counts as one and is logged as ALERT), so a record that is merely late never pings. The count
    is kept in state, so a restart keeps it, and dropped as soon as the review is recorded.
    It counts sightings, not strictly consecutive cycles: a cycle that exits early before
    reaching here neither advances nor resets it."""
    tid, v = t["id"], t["verdict"]
    wt = repo / ".worktrees" / t["branch"].removeprefix("task/")
    seen, unrecorded = state["unrecorded_seen"], set()
    for kind, info in sorted(rk.latest_reviews(wt / ".reviews", tip).items()):
        recorded = (v is not None and v["commit"] == tip and kind in v["reviews"]
                    and v["reviews"][kind]["result"] == info["verdict"])
        try:
            saved_at = Path(info["file"]).stat().st_mtime
        except OSError:
            continue
        key = f"{tid}:review_unrecorded:{tip}:{kind}"
        if recorded or now.timestamp() - saved_at < UNRECORDED_GRACE or key in state["sent"]:
            continue
        unrecorded.add(key)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 1:
            dp.log("ALERT", f"{tid} review_unrecorded {tip[:8]} {kind} first seen; "
                            f"ping after {cycles} cycles")
        if seen[key] < cycles:
            continue
        detail = f"{info['reviewer']} review of {tip[:7]} ({info['verdict'] or 'no verdict'}) is not on the ticket"
        if _ping(t, "review_unrecorded", key, state, warn, detail):
            emit(f"{tid}  {detail}")
            dp.log("ALERT", f"{tid} review_unrecorded {tip[:8]} {kind} ping sent")
    prefix = f"{tid}:review_unrecorded:"
    for key in [k for k in seen if k.startswith(prefix) and k not in unrecorded]:
        del seen[key]   # recorded, sent or the tip moved: a later one starts from zero


# --- true failure ----------------------------------------------------------------
# A FAIL verdict is a normal mid-loop result and never pings. A claimed ticket
# has TRULY failed only when (decided from the ticket, git and herdr, never from
# what an agent says):
#   * its agent stopped (idle or done) with the branch tip still the commit
#     that got a FAIL, for STOPPED_AFTER seconds in a row; or
#   * it has `fail_rounds_threshold` or more FAIL verdicts on different commits, the latest
#     verdict still being a FAIL (going in circles); or
#   * its agent has waited for input for BLOCKED_AFTER seconds (permission
#     prompts are never answered); or
#   * nothing has happened for STALL_AFTER seconds while its agent is idle or
#     waiting (never while herdr shows it working).
# Then the ticket is closed as failed and an on-call agent is filed (see _fail_ticket).

def fail_rounds(t: dict, ignored: frozenset = frozenset()) -> int:
    """Different commits that got a recorded FAIL, except `ignored` ones (the supervisor
    classified those rounds PROGRESS)."""
    v = t["verdict"]
    commits = set(t.get("fail_commits", []))
    if v is not None and v["result"] == "FAIL" and rk.majority_failed(v["reviews"]):
        commits.add(v["commit"])
    return len(commits - ignored)


def latest_analysis(t: dict) -> str:
    """The supervisor's latest note on the ticket (its text only, clipped), for the backstop's
    close note. Any author can write a note that looks like one, so this is quoted as text and
    never acted on."""
    for n in reversed(t.get("notes", [])):
        if n["text"].startswith("SUPERVISOR "):
            return _clip(" ".join(n["text"].split()), ANALYSIS_MAX)
    return "no supervisor analysis was recorded"


def _failed_reviewers(t: dict) -> str:
    v = t["verdict"]
    if v is None or v["result"] != "FAIL":
        return "none"
    return ", ".join(sorted(r["reviewer"] for r in v["reviews"].values() if r["result"] == "FAIL"))


def _end_spells(ticket_id: str, state: dict) -> None:
    """The ticket is ready (its agent has stopped for good): no blocked or
    stopped-on-a-failure spell is running any more."""
    state["blocked_since"].pop(ticket_id, None)
    state["stopped_since"].pop(ticket_id, None)
    state["dialog_pinged"].pop(ticket_id, None)
    state["nudged"].pop(ticket_id, None)


def _agent_last_note(t: dict) -> str:
    """The newest note not written by the human or the supervisor, clipped; "" if none. Untrusted text."""
    for n in reversed(t.get("notes", [])):
        if n.get("by") != "human" and not n["text"].startswith("SUPERVISOR "):
            return _clip(" ".join(n["text"].split()), ANALYSIS_MAX)
    return ""


def _no_commit_check(t: dict, tip: str, agent: dict | None, idle: float, state: dict,
                     now: datetime) -> str | None:
    """An agent that ended its turn with no commit since the claim (the branch tip is still the
    base commit). Idle for GIVE_UP_AFTER: it is nudged once (commit, or write a note saying why).
    Still idle with no commit GIVE_UP_AFTER after the nudge: the ticket fails. Never while herdr
    shows working, blocked or waiting."""
    tid = t["id"]
    base = t.get("base_commit")
    if not base or tip != base:
        state["nudged"].pop(tid, None)
        return None
    if agent is None or agent.get("agent_status") not in ("idle", "done") or idle < GIVE_UP_AFTER:
        return None
    nudged = _parse(state["nudged"].get(tid))
    if nudged is None:
        try:
            lc.herdr("agent", "prompt", agent.get("name") or tid.lower(), NUDGE_TEXT)
        except lc.LaunchError as exc:
            dp.log("NUDGE_FAILED", f"{tid} {_clip(str(exc), 200)}")
            return None   # not recorded: tried again next cycle
        state["nudged"][tid] = _stamp(now)
        dp.log("NUDGED", f"{tid} idle with no commit for {int(idle // 60)} min")
        return None
    if (now - nudged).total_seconds() >= GIVE_UP_AFTER:
        return NO_COMMIT_REASON
    return None


def _dialog_ping(t: dict, agent: dict, state: dict, now: datetime) -> None:
    """The first time a ticket's agent is seen blocked: log AGENT_DIALOG once with the
    tail of its pane and send one low ping, so a person can answer before the
    BLOCKED_AFTER rule fails the ticket. Read-only: nothing is ever sent to the pane.
    Pane text is untrusted data (see dialog.py); only the fixed label drives anything."""
    tid = t["id"]
    if tid in state["dialog_pinged"]:
        return
    state["dialog_pinged"][tid] = _stamp(now)   # first, so an ntfy outage does not retry every cycle
    label, tail = dialog.inspect(agent.get("pane_id"))
    tab = dialog.sanitize(agent["tab_id"])[:40] if isinstance(agent.get("tab_id"), str) else ""
    tab = tab or "?"
    flat = dialog.one_line(tail)
    if flat and tk.check_secrets("agent dialog", flat):   # fails closed: keep only the ids and the label
        flat = ""
    dp.log("AGENT_DIALOG", f"{tid} tab={tab} {label}: {flat}")
    notify.notify_quietly(t, "dialog", detail="\x1f".join((label, tab, flat)))


def _failure_reason(t: dict, repo: Path, tip: str, agent: dict | None, state: dict,
                    now: datetime, fail_rounds_threshold: int,
                    progress: frozenset = frozenset(), leftovers: bool = False,
                    waiting: bool = False) -> str | None:
    """Why this claimed ticket has truly failed, or None. Keeps the clocks for
    the stopped and blocked spells in `state`. `leftovers`: the supervisor marked this exact tip
    ready with leftovers, so the rules about a failed tip (backstop, rounds, stopped after a
    FAIL) leave it alone; BLOCKED and STALL still apply. `waiting`: the supervisor has not
    judged the failed commit yet, so the backstop holds off a little (see sr.BACKSTOP_WAIT)."""
    tid, v = t["id"], t["verdict"]
    failed_now = v is not None and v["result"] == "FAIL"
    agent_state = agent.get("agent_status") if agent else None
    reason = None
    # 0. The backstop: PROGRESS rounds do not count below and a LOOP restarts the session, so
    # this is what ends a ticket that would otherwise review for ever, whatever the supervisor
    # said. SQUIRRELS is judged first (EM-8 step F): a ticket it marked leaves the loop, and the
    # backstop waits for the supervisor's answer on the failed commit (at most sr.BACKSTOP_WAIT).
    total = tk.review_rounds(t)
    failed_now = failed_now and not leftovers
    if failed_now and total >= BACKSTOP_ROUNDS and not waiting:
        reason = f"{BACKSTOP_REASON}: {total} review rounds without a pass (limit {BACKSTOP_ROUNDS})"
    # 1. Going in circles.
    rounds = fail_rounds(t, progress)
    if reason is None and failed_now and rounds >= fail_rounds_threshold:
        reason = f"going in circles: {rounds} FAIL verdicts on different commits"
    # 2. The agent stopped without fixing the failed commit.
    if failed_now and v["commit"] == tip and agent_state in ("idle", "done"):
        seen = state["stopped_since"].get(tid)
        if not (isinstance(seen, dict) and seen.get("tip") == tip and _parse(seen.get("at")) is not None):
            seen = {"tip": tip, "at": _stamp(now)}
            state["stopped_since"][tid] = seen
        waited = (now - _parse(seen["at"])).total_seconds()
        if reason is None and waited >= STOPPED_AFTER:
            reason = (f"the agent stopped {int(waited // 60)} min after a FAIL and the branch tip "
                      "is still the failed commit")
    else:
        state["stopped_since"].pop(tid, None)
    # 3. An agent waiting for a person.
    if agent_state == "blocked":
        _dialog_ping(t, agent, state, now)
        since = state["blocked_since"].setdefault(tid, _stamp(now))
        waited = (now - (_parse(since) or now)).total_seconds()
        if reason is None and waited >= BLOCKED_AFTER:
            reason = f"the agent was blocked waiting for input for {int(waited // 60)} min"
    else:
        state["blocked_since"].pop(tid, None)
        state["dialog_pinged"].pop(tid, None)   # the blocked episode is over
    # 4. Nothing has happened for a long time. Only while herdr shows the agent idle,
    # done or waiting: a working agent can be silent for hours. The clock restarts when
    # the agent's status changes, so it counts from when the agent stopped working.
    last = _last_activity(t, repo, tip, agent, state, now)
    idle = (now - last).total_seconds()
    if reason is None:
        reason = _no_commit_check(t, tip, agent, idle, state, now)
    if reason is None and idle >= STALL_AFTER and agent_state in STALL_STATES:
        reason = f"stalled: no commit, note, log output or agent change for {int(idle // 60)} min"
    return reason


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _split(text: str, limit: int) -> list[str]:
    """Cut text into pieces of at most `limit` characters, at line ends where it can
    (and in the middle of a line only when one line is longer than `limit`)."""
    pieces, cur = [], ""
    for line in text.strip().splitlines():
        while len(line) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            pieces.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        pieces.append(cur)
    return pieces


def _log_tail(tid: str, lines: int = 12) -> str:
    pattern = re.compile(rf"\b{re.escape(tid)}\b")
    try:
        with open(dp.log_path(), "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - LOG_TAIL))
            found = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    return "\n".join([line for line in found if pattern.search(line)][-lines:])


def _oncall_body(t: dict, detail: str, tip: str, repo: Path) -> str:
    """What the on-call agent needs, in the ticket body: the failed ticket's WHOLE
    spec (agents can read only their own ticket), the latest reviews' MUST FIX
    sections, why it failed and the log tail. The failed ticket's notes are copied
    as notes on the on-call ticket (_copy_notes)."""
    v = t["verdict"]
    reviewed = v["commit"] if v is not None and v["result"] == "FAIL" else tip
    parts = [f"On-call fix for {t['id']} ({t['type']}): {_clip(t['title'], 100)}",
             f"WHY IT FAILED: {detail}",
             f"FAILED BRANCH: {t['branch']} at {tip}. Your new branch starts from that commit. "
             "The failed agent's herdr tab may still be open: do not rely on that branch staying put.",
             "The failed ticket's notes are in this ticket's notes."]
    wt = repo / ".worktrees" / t["branch"].removeprefix("task/")
    reviews = wt / ".reviews"
    # The failed agent can write in its folder: never follow a link (to the folder
    # or to a file) out of it into the on-call ticket, which may be mirrored.
    safe = not wt.is_symlink() and not reviews.is_symlink() and reviews.resolve().parent == wt.resolve()
    for kind, info in sorted(rk.latest_reviews(reviews, reviewed).items() if safe else []):
        try:
            if Path(info["file"]).is_symlink():
                continue
            must = rk.review_items(Path(info["file"]).read_text(encoding="utf-8"), "MUST FIX")
        except (OSError, UnicodeDecodeError):
            continue
        if must:
            every = _split(must, MUST_FIX_PART)
            chunks = every[:MUST_FIX_PARTS]
            for i, chunk in enumerate(chunks, 1):
                part = f" (part {i}/{len(every)})" if len(every) > 1 else ""
                parts.append(f"MUST FIX from the latest {info['reviewer']} review ({reviewed[:7]}){part}:\n{chunk}")
            if len(every) > len(chunks):
                parts.append(f"MUST FIX from the {info['reviewer']} review: parts {len(chunks) + 1} to {len(every)} "
                             f"are left out here; the whole review is in {info['file']}")
    tail = _log_tail(t["id"])
    if tail:
        parts.append("DISPATCHER LOG TAIL:\n" + _clip(tail, 600))
    spec = f"FAILED TICKET SPEC (whole):\n{t['body']}"
    # The spec comes last and whole; if the context before it is too long, that is what is clipped.
    head = _clip("\n\n".join(parts), max(0, tk.ONCALL_BODY_MAX - len(spec) - 2))
    return _clip(f"{head}\n\n{spec}", tk.ONCALL_BODY_MAX)


def _squash(text: str) -> str:
    """Whitespace normalised (runs of spaces and tabs to one space, trailing space and
    blank-line runs gone), so a stored note and the piece it was cut from compare equal."""
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.strip().splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def _copy_notes(oc_id: str, t: dict, warn) -> None:
    """Copy all of the failed ticket's notes onto the on-call ticket, in order, cut
    into pieces that fit a note. The text is copied as it is; whitespace is
    normalised only to tell whether a piece is already there. Pieces already there
    are skipped, so a retry after an interruption finishes the job. The store holds NOTES_LIMIT notes a ticket;
    room is kept for the dispatcher's own, and anything over says so."""
    room = tk.NOTES_LIMIT - 6
    pieces = []
    for n in t.get("notes", []):
        text = f"Note on {t['id']} by {n['by']} at {n['at']}:\n{n['text']}"
        pieces += [text[i:i + tk.NOTE_MAX] for i in range(0, len(text), tk.NOTE_MAX)]
    have = {_squash(n["text"]) for n in tk.load_ticket(oc_id).get("notes", [])}
    for piece in pieces[:room]:
        if _squash(piece) not in have:
            _note(oc_id, piece, warn)
    over = f"{len(pieces) - room} more pieces of {t['id']}'s notes are not copied: a ticket holds {tk.NOTES_LIMIT} notes."
    if len(pieces) > room and _squash(over) not in have:
        _note(oc_id, over, warn)


def _note(ticket_id: str, text: str, warn) -> None:
    """Add a note; a note the store refuses for good (a secret scan hit) is only a warning."""
    try:
        tk.add_note(ticket_id, _clip(text, tk.NOTE_MAX))
    except (tk.TicketInvalid, tk.TicketError) as exc:
        # Refused (a secret scan hit, or the ticket is at its notes limit): skip the
        # note so recovery still queues its ping and completes.
        warn(f"{ticket_id}: could not save a note ({exc})")


def _note_once(ticket_id: str, marker: str, text: str, warn) -> None:
    """Add the note unless one starting with `marker` is already there (a retry)."""
    if not any(n["text"].startswith(marker) for n in tk.load_ticket(ticket_id).get("notes", [])):
        _note(ticket_id, text, warn)


def needs_human_first(t: dict) -> bool:
    """True when an on-call agent cannot help: the newest note an agent wrote (not the human,
    which is also the fleet's own author, and not the supervisor) says it needs a human or the
    real store. Untrusted text decides only a safe thing here: a person looks instead of an agent."""
    for n in reversed(t.get("notes", [])):
        if n.get("by") == "human" or n["text"].startswith("SUPERVISOR"):
            continue
        return bool(NEEDS_HUMAN_RE.search(n["text"]))
    return False


def _file_oncall(t: dict, detail: str, tip: str, repo: Path, warn) -> dict | None:
    """File the on-call fix ticket for a failed ticket, carrying its whole spec and
    notes (an agent can read only its own ticket). None if it cannot be made for
    good (the secret scan refused the copied text): a human must look. An OSError
    is not caught: the recovery record stays and the next cycle tries again."""
    fields = {"type": "fix", "repo": t["repo"], "branch": f"task/oc-{t['id'].lower()}",
              "title": _clip(f"On-call: fix {t['id']}: {t['title']}", 100), "parent": t["id"],
              "body": _oncall_body(t, detail, tip, repo)}
    try:
        oc = tk.create_ticket(fields, oncall=True, oncall_base=tip)
    except (tk.TicketError, tk.TicketInvalid) as exc:
        warn(f"{t['id']}: could not file the on-call ticket: {exc}")
        return None
    return oc


# --- the recovery record ---------------------------------------------------------

def recovery_dir() -> Path:
    return tk.fleet_home() / "pending-recovery"


def _record_path(ticket_id: str) -> Path:
    return recovery_dir() / f"{ticket_id}.json"


def write_recovery(t: dict, reason: str, commit: str, final: str | None = None) -> None:
    """Record, atomically, that this ticket has truly failed. This is the FIRST thing
    done; everything else (close, on-call ticket, notes, ping) is driven from the
    record by `process_recoveries`. One record per ticket; raises OSError. `final` (the review-rounds backstop: the
    supervisor's latest analysis) means no on-call ticket: a human takes it from here."""
    path = _record_path(t["id"])
    tk.refuse_real_store_in_tests(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"id": t["id"], "commit": commit, "reason": reason, "reviewer": _failed_reviewers(t),
           "rounds": fail_rounds(t), "at": _stamp(datetime.now(timezone.utc))}
    if final is not None:
        rec["final"] = final
    tk._write_atomic(path, rec)


def pending_recoveries(warn) -> list[dict]:
    d = recovery_dir()
    found = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        if path.name.startswith("."):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            ok = (isinstance(rec, dict) and isinstance(rec.get("id"), str) and path.stem == rec["id"]
                  and tk.ID_RE.fullmatch(rec["id"]) and all(isinstance(rec.get(k), str)
                                                          for k in ("commit", "reason", "reviewer", "at"))
                  and isinstance(rec.get("rounds"), int)
                  and (rec.get("final") is None or isinstance(rec["final"], str)))
        except (OSError, ValueError):
            ok = False
        if ok:
            found.append(rec)
        else:
            warn(f"{path} is not a usable recovery record; fix or delete it")
    return sorted(found, key=lambda r: (r["at"], r["id"]))


def _oncall_for(t: dict, rec: dict, detail: str, repo: Path, settings: dict, now: datetime,
                warn) -> tuple[dict | None, str | None]:
    """The on-call ticket for a failed ticket, or why there is none. One already
    on the expected branch is this recovery's own, filed before an interruption."""
    tickets = tk.list_tickets()
    mine = [x for x in tickets if x.get("oncall") and x["parent"] == t["id"]]
    for x in mine:
        if x["branch"] == f"task/oc-{t['id'].lower()}":
            _copy_notes(x["id"], t, warn)   # finish an interrupted copy
            return x, None
    if mine:
        return None, "an on-call attempt was already made for it"
    cap = settings["max_oncall_per_day"]
    if dp.oncall_today(tickets, now, settings["timezone"]) >= cap:
        return None, f"the daily on-call limit ({cap}) is reached"
    oc = _file_oncall(t, detail, rec["commit"], repo, warn)
    if oc is None:
        return None, "the on-call ticket could not be filed"
    _copy_notes(oc["id"], t, warn)
    return oc, None


def _recover(rec: dict, settings: dict, state: dict, now: datetime, emit, warn) -> None:
    """Carry one recovery record as far as it will go; it is deleted only when every
    step is done. Every step is safe to repeat."""
    tid = rec["id"]
    path = _record_path(tid)
    if not tk._ticket_path(tid).exists():
        path.unlink(missing_ok=True)
        return
    t = tk.load_ticket(tid)
    detail = f"{rec['reason']}; failed by: {rec['reviewer']}; FAIL rounds: {rec['rounds']}"
    if rec.get("final") is not None:
        label = "agent's last note" if rec["reason"] == NO_COMMIT_REASON else "supervisor"
        detail += f"; {label}: {rec['final']}"
    if tk.check_secrets("close note", detail):   # the reviewer name and analysis are free text: scan like any note
        detail = f"{rec['reason']}; FAIL rounds: {rec['rounds']} (reviewer name and analysis withheld)"
    # 1. close it as failed (its card moves to Failed)
    if t["status"] in ("claimed", "open"):
        t = tk.set_status(tid, "failed", allowed_from=("claimed", "open"),
                          close_note=_clip(f"Failed: {detail}", tk.CLOSE_NOTE_MAX))
        kaneo.mirror_quietly(t, warn)
        emit(f"{tid}  failed: {detail}")
        lc._safe_log("FAILED", f"{tid} {_clip(detail, 200)}")
    elif t["status"] != "failed":
        path.unlink(missing_ok=True)   # it went another way (done, cancelled, in review): nothing failed
        return
    repo = settings["projects_dir"] / t["repo"]
    # 2. the on-call ticket, or the reason there is none, as notes
    if t.get("oncall"):
        original = t["parent"]
        diagnosis = (f"Diagnosis: on-call {tid} could not fix {original}. {detail}. Its branch "
                     f"{t['branch']} ends at {rec['commit']}. Nothing more will be launched for it.")
        _note_once(tid, "Diagnosis:", diagnosis, warn)
        _note_once(original, "Diagnosis:", diagnosis, warn)
        ping = (original, "needs_human", None)
    elif rec.get("final") is not None:   # the review-rounds backstop: no on-call attempt
        _note_once(tid, "Needs a human:", f"Needs a human: {detail}.", warn)
        lc._safe_log("NEEDS_HUMAN", tid)
        ping = (tid, "needs_human", None)
    elif needs_human_first(t):   # no on-call: it cannot do what the ticket needs
        _note_once(tid, "Needs a human:", f"Needs a human: {detail}. No on-call agent can do this work.", warn)
        lc._safe_log("NEEDS_HUMAN", tid)
        ping = (tid, "needs_human", None)
    else:
        oc, why = _oncall_for(t, rec, detail, repo, settings, now, warn)
        if oc is not None:
            kaneo.mirror_quietly(oc, warn)
            _note_once(tid, f"On-call ticket {oc['id']} was filed",
                       f"On-call ticket {oc['id']} was filed to fix this. {detail}.", warn)
            lc._safe_log("ONCALL_FILED", f"{oc['id']} for {tid}")
            ping = (tid, "oncall", oc["id"])
        else:
            _note_once(tid, "Needs a human:", f"Needs a human: {why}. {detail}.", warn)
            lc._safe_log("NEEDS_HUMAN", tid)
            ping = (tid, "needs_human", None)
    # 2b. its herdr tab, now the on-call ticket (or the reason there is none) is on record.
    # Never while its agent still runs: the sweep of finished tabs retries later.
    closed, why_tab = lc.close_finished_tab(tid, settings)
    emit(f"{tid}  tab {'closed' if closed else 'kept'}: {why_tab}")
    # 3. queue the one ping (saved to disk; a send that fails is retried every cycle)
    _queue_ping(ping[0], ping[1], state, warn, ping[2])
    path.unlink(missing_ok=True)
    _retry_pending(state, warn, settings)


def process_recoveries(settings: dict, state: dict, now: datetime, emit, warn) -> None:
    for rec in pending_recoveries(warn):
        try:
            _recover(rec, settings, state, now, emit, warn)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{rec['id']}: recovery is not finished and will be retried next cycle ({exc})")


# --- a commit after the PASS -----------------------------------------------------------
# Threat model: a moved branch must never ship on an old verdict, and a re-review always runs on
# the exact new tip. The gate and ready_to_ship already refuse a verdict of another commit; this
# is what makes the ticket move on instead of sitting claimed with nobody looking at it. Once the
# tip is past its PASS and its agent has stopped (idle, done, or gone), the ready state is
# cleared (back to claimed) and the dispatcher runs prep.validate on THAT commit in the
# background: check.sh, then the reviews its risk level needs, with the task folder and the branch
# both required to be at it before and after. Meanwhile the ticket is reserved like a ship, so
# `ship` and the prep pass keep off it. A PASS then goes through the normal ready flow (one
# "ready to ship" ping); a FAIL is the normal mid-loop FAIL (the true-failure rules ping), a
# ticket with no agent gets a new one; anything else (no verdict recorded: check.sh failed,
# review.sh crashed, the branch moved again) is a note and one needs-human ping per tip.

_REREVIEWS: dict[tuple[str, str], dict] = {}   # in memory: a restarted dispatcher re-runs it (pings are deduped)
_REREVIEWS_LOCK = threading.Lock()
REREVIEW_NOTE = ("the branch moved past its PASS; scripts/check.sh and the required reviews of "
                 "this exact commit passed.")


def _rereview_thread(t: dict, tip: str, settings: dict, entry: dict) -> None:
    try:
        try:
            problem = prep.validate(t, settings, expect=tip, note=REREVIEW_NOTE)
        except Exception as exc:   # a thread must always leave a result behind
            problem = f"the re-review could not finish ({_clip(str(exc), 150)})"
        entry["problem"] = problem
        entry["done"] = True
    finally:
        prep.release_shipping(t["id"])   # last: the ticket is judged again only after the result is stored


def wait_rereviews() -> None:
    """Block until the background re-reviews are done (a one-shot run, and tests)."""
    with _REREVIEWS_LOCK:
        threads = [e["thread"] for e in _REREVIEWS.values()]
    for th in threads:
        th.join()


def abandon_rereviews(wait: float = 5.0) -> None:
    """Before the dispatcher restarts (exec keeps the PID and drops the threads) or stops: give the
    re-reviews a moment, then release the shipping reservation of every one still running, so no
    ticket stays reserved under a PID that will never finish it. A review script left running only
    records a verdict of the exact commit it reviewed; the restarted watcher re-runs the re-review."""
    with _REREVIEWS_LOCK:
        running = [(k[0], e["thread"]) for k, e in _REREVIEWS.items()]
    for _, th in running:
        th.join(wait)
    for tid, th in running:
        if th.is_alive():
            prep.release_shipping(tid)
    with _REREVIEWS_LOCK:
        _REREVIEWS.clear()


def _rereview(t: dict, tip: str, agent: dict | None, settings: dict, state: dict, emit, warn) -> tuple[str, dict]:
    """Drive the re-review of a ticket whose tip is past its PASS. Returns ("wait", t) when
    nothing more is to be decided this cycle, ("passed", t) when the new tip was reviewed and
    passed (t reloaded), ("failed", t) when it did not (t reloaded; judged by the normal rules)."""
    tid, old = t["id"], t["verdict"]["commit"]
    key = (tid, tip)
    with _REREVIEWS_LOCK:
        entry = _REREVIEWS.get(key)
    if entry is None:
        if t["status"] == "review":   # the ready state goes first, whatever happens next
            new = _move(t, "claimed", ("review",), warn)
            if not new:
                return "wait", t
            emit(f"{tid}  back to In Progress (a commit landed after the PASS)")
            dp.log("REVIEW_BACK", f"{tid} back to claimed: tip {tip[:8]} is past the PASS on {old[:8]}")
            kaneo.mirror_quietly(new, warn)
            t = new
        problem = prep.reserve_shipping(tid)   # no ship or prep pass touches it meanwhile
        if problem:
            warn(f"{tid}: re-review waits: {problem}")
            return "wait", t
        entry = {"done": False, "problem": None}
        entry["thread"] = threading.Thread(target=_rereview_thread, args=(t, tip, settings, entry),
                                           name=f"rereview-{tid}", daemon=True)
        with _REREVIEWS_LOCK:
            for k in [k for k in _REREVIEWS if k[0] == tid]:   # an older tip's result is of no use now
                del _REREVIEWS[k]
            _REREVIEWS[key] = entry
        entry["thread"].start()
        emit(f"{tid}  {tip[:8]} landed after the PASS on {old[:8]}: re-checking and re-reviewing it")
        dp.log("REREVIEW_STARTED", f"{tid} {tip[:8]} is past the PASS on {old[:8]}")
        return "wait", t
    if not entry["done"]:
        return "wait", t
    fresh = tk.load_ticket(tid)
    if entry["problem"] is None or passed_for_tip(fresh, tip):   # or the agent got its own PASS on this tip
        return "passed", fresh
    v = fresh.get("verdict")
    if v is not None and v["result"] == "FAIL" and v["commit"] == tip:
        if agent is None and not entry.get("relaunched"):   # nobody is on it: the normal FAIL loop needs an agent
            entry["relaunched"] = True
            prep.relaunch_sent_back(fresh, settings, emit, warn)
            return "wait", fresh
        return "failed", fresh   # a FAIL is the usual mid-loop FAIL: the true-failure rules take it from here
    _rereview_problem(fresh, tip, entry["problem"], state, warn)
    return "failed", fresh


def _rereview_problem(t: dict, checked: str, problem: str, state: dict, warn) -> None:
    """A re-review of `checked` ended without a verdict: a note and one needs-human ping per tip."""
    tid = t["id"]
    detail = _clip(f"the re-review of {checked[:8]} did not complete: {problem}", 250)
    _note_once(tid, f"Needs a human: the re-review of {checked[:8]}", f"Needs a human: {detail}.", warn)
    if _ping(t, "needs_human", f"{tid}:rereview:{checked}", state, warn, detail):
        dp.log("NEEDS_HUMAN", f"{tid} {detail}")


def _settle_stale_rereviews(t: dict, tip: str, state: dict, warn) -> None:
    """A finished re-review of a tip the branch is no longer at (it moved while review.sh ran, or
    after): drop it, and say if it ended without a verdict, so that is never silent. A newer
    commit past the PASS then starts its own re-review as usual."""
    with _REREVIEWS_LOCK:
        old = [(k, e) for k, e in _REREVIEWS.items() if k[0] == t["id"] and k[1] != tip and e["done"]]
        for k, _ in old:
            del _REREVIEWS[k]
    for (_, checked), entry in old:
        if entry["problem"] is not None:
            _rereview_problem(t, checked, entry["problem"], state, warn)


# --- the cycle -----------------------------------------------------------------

def _move(t: dict, status: str, allowed_from: tuple, warn):
    try:
        return tk.set_status(t["id"], status, allowed_from=allowed_from)
    except tk.TicketError as exc:
        dp.log("REVIEW_SKIP", f"{t['id']} {exc}")
        warn(f"{t['id']}: could not move to {status}: {exc}")
        return None


def _check_one(t: dict, agent: dict | None, agents_known: bool, settings: dict,
               now: datetime, state: dict, emit, warn, moved: list | None = None,
               progress: dict | None = None, marks: dict | None = None,
               waiting: frozenset = frozenset()) -> None:
    repo = settings["projects_dir"] / t["repo"]
    if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
        return
    tip = _tip(repo, t["branch"])
    if tip is None:
        return
    agent_state = agent.get("agent_status") if agent else None
    stopped = agent_state in ("idle", "done")
    earned = passed_for_tip(t, tip) and has_summary_after_review(t)

    _settle_stale_rereviews(t, tip, state, warn)
    with _REREVIEWS_LOCK:
        rereviewing = (t["id"], tip) in _REREVIEWS   # its result is taken up here once it is in
    if agents_known and (agent is None or stopped) and (rereviewing or moved_past_pass(t, tip)):
        outcome, t = _rereview(t, tip, agent, settings, state, emit, warn)
        if outcome == "wait":
            return
        if outcome == "passed":   # no agent left to wait for: the new PASS is judged like any ready ticket
            stopped = True
            earned = passed_for_tip(t, tip) and has_summary_after_review(t)
        else:
            earned = False
    elif agents_known and agent is None and earned and vouched_by_rereview(t):
        stopped = True   # re-reviewed and passed, then the dispatcher restarted before the ticket moved on

    if t["status"] == "review":
        # Leave it alone if herdr cannot be asked; a vanished agent is not busy.
        busy = agents_known and agent is not None and not stopped
        if earned and not busy:
            if moved is not None:
                moved.append((t, tip))   # like a ticket that just moved: judged after every move of the cycle
            else:
                _ready_ping(t, tip, settings, now, state, warn)
            _end_spells(t["id"], state)
            _unrecorded_pings(t, repo, tip, state, now, emit, warn, settings["unrecorded_ping_cycles"])
            return
        new = _move(t, "claimed", ("review",), warn)
        if new:
            why = "agent is busy again" if earned else "its reviewed commit or summary no longer matches"
            emit(f"{t['id']}  back to In Progress ({why})")
            dp.log("REVIEW_BACK", f"{t['id']} back to claimed: {why}")
            kaneo.mirror_quietly(new, warn)
            t = new   # claimed again: the alert checks below apply in full
            if agents_known and agent is None:   # its tab was closed in review: nobody is on it
                prep.relaunch_sent_back(t, settings, emit, warn)
                return   # the new agent starts fresh: judge it from the next cycle
        else:
            return
    elif earned and stopped:
        new = _move(t, "review", ("claimed",), warn)
        if new:
            emit(f"{t['id']}  ready to ship: moved to In Review")
            dp.log("REVIEW_READY", f"{t['id']} {tip[:8]} ready to ship")
            kaneo.mirror_quietly(new, warn)
            # Its agent has stopped and the ticket no longer fills a build slot: close the
            # tab (never a focused one; a kept tab is closed when the ticket is done).
            closed, reason = lc.close_finished_tab(t["id"], settings)
            emit(f"{t['id']}  tab {'closed' if closed else 'kept'}: {reason}")
            if moved is not None:
                moved.append((new, tip))   # pinged by check() once every ticket of this cycle has moved
            else:
                _ready_ping(new, tip, settings, now, state, warn)
            _end_spells(t["id"], state)
            _unrecorded_pings(new, repo, tip, state, now, emit, warn, settings["unrecorded_ping_cycles"])
        return
    if fresh.refresh_if_large(t, agent, tip, settings, state, emit, warn):
        return   # a new session starts from here: judge it from the next cycle
    _unrecorded_pings(t, repo, tip, state, now, emit, warn, settings["unrecorded_ping_cycles"])
    reason = _failure_reason(t, repo, tip, agent, state, now, settings["fail_rounds_threshold"],
                            (progress or {}).get(t["id"], frozenset()),
                            leftovers=(marks or {}).get(t["id"]) == tip, waiting=t["id"] in waiting)
    if reason:
        final = latest_analysis(t) if reason.startswith(BACKSTOP_REASON) else None
        if reason == NO_COMMIT_REASON:   # no on-call attempt: a person reads the agent's last note
            final = _agent_last_note(t) or "no note was written"
        write_recovery(t, reason, tip, final)   # first: everything else is driven from the record


REVIVE_NOTE = ("revived: the failed branch's tip passed the vote ({vote}) at the review level it needs, "
               "and it is not in main.")


def _revivable(t: dict, repo: Path) -> str | None:
    """The tip of a ticket the dispatcher failed that can be revived, else None. All must hold:
    it failed through the dispatcher (its close note starts "Failed: "; a human's close is final),
    its branch tip carries an exact-commit verdict that passes the vote (two distinct companies,
    no FAIL) and the gate of its real risk level, and the tip is not already delivered to main
    (by its own merge or through another ticket's)."""
    if t["status"] != "failed" or t["type"] not in dp.LAUNCH_TYPES or not t.get("branch") \
            or not (t.get("close_note") or "").startswith("Failed: "):
        return None
    tip = _tip(repo, t["branch"])
    v = t.get("verdict")
    if tip is None or v is None or v["commit"] != tip or v["result"] != "PASS":
        return None
    if not rk.vote_passes(v["reviews"]) or not kaneo.ready_to_ship(t):
        return None
    main = dp.main_branch(repo)
    if main is None or dp._git(repo, "merge-base", "--is-ancestor", tip, main).returncode == 0:
        return None   # already in main
    if dp.is_merged(repo, t["branch"], t.get("base_commit")) or dp.delivered_by_partner(repo, t, tk.list_tickets()):
        return None
    return tip


def revive_failed(settings: dict, emit, warn) -> None:
    """Failed -> claimed (dispatcher only) for a failed ticket whose branch tip now has a new
    exact-commit verdict that meets the gate and is not delivered elsewhere. Its superseded
    on-call ticket is cancelled when it has not started; one already running keeps the parent
    failed (two agents on one branch tip is the worse outcome). The normal review flow then
    moves the revived ticket on to In Review."""
    tickets = tk.list_tickets()
    for t in sorted((t for t in tickets if t["status"] == "failed"), key=tk.oldest_first_key):
        repo = settings["projects_dir"] / t["repo"]
        if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
            continue
        try:
            tip = _revivable(t, repo)
            if tip is None:
                continue
            ocs = [o for o in tickets if o.get("oncall") and o.get("parent") == t["id"]]
            if any(o["status"] in ("claimed", "review", "done") for o in ocs):
                continue   # its on-call agent is on it or has delivered: not ours to take back
            for o in ocs:
                if o["status"] == "open":
                    cancelled = tk.set_status(o["id"], "cancelled", allowed_from=("open",),
                                              close_note=f"superseded: {t['id']} was revived at {tip[:8]}")
                    kaneo.mirror_quietly(cancelled, warn)
            new = tk.revive_ticket(t["id"])
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            dp.log("REVIVE_SKIP", f"{t['id']} {exc}")
            warn(f"{t['id']}: could not revive: {exc}")
            continue
        vote = ", ".join(f"{r['reviewer']}: {r['result']}" for r in new["verdict"]["reviews"].values())
        tk.add_note(t["id"], f"{prep.PASS_NOTE_PREFIX} {tip[:8]}: " + REVIVE_NOTE.format(vote=vote)
                    + " " + REREVIEW_NOTE)
        emit(f"{t['id']}  revived: its tip {tip[:8]} passed the vote")
        dp.log("REVIVED", f"{t['id']} {tip[:8]} back to claimed")
        kaneo.mirror_quietly(new, warn)


def _flag_stuck_oncall(settings: dict, warn) -> None:
    """An open on-call ticket that can never be launched (its repo or start commit is
    gone, its branch or task folder already exists, or its launch failed
    LAUNCH_ATTEMPTS times) would sit in the queue for ever: give it a recovery
    record, which closes it as failed and sends one "needs a human" ping."""
    tickets = tk.list_tickets()
    blocked = {d.ticket["id"]: d.skip for d in dp.decide_oncall(tickets, settings, frozenset())
               if d.skip and not d.skip.startswith(dp.QUEUE_SKIPS)}
    for oc in tickets:
        if not oc.get("oncall") or oc["status"] != "open":
            continue
        reason = blocked.get(oc["id"])
        failures = sum(1 for n in oc.get("notes", []) if n["by"] == "human" and n["text"].startswith(LAUNCH_FAILED_NOTE))
        repo = settings["projects_dir"] / oc["repo"]
        folder = repo / ".worktrees" / oc["branch"].removeprefix("task/")
        if reason is None and failures >= LAUNCH_ATTEMPTS:
            reason = f"its launch failed {failures} times"
        elif reason is None and repo.is_dir() and (folder.exists() or folder.is_symlink()):
            reason = f"its task folder {folder.name} already exists"
        if reason is not None:
            write_recovery(oc, f"the on-call ticket cannot be launched: {reason}", oc.get("oncall_base") or "0" * 40)


def _queue_leftovers_pings(marks: dict, sup: dict, state: dict, settings: dict, warn) -> None:
    """The one "ready with leftovers" ping of each marked ticket whose mark still stands (the
    branch tip is the marked commit). Worked out from the saved marks every cycle, so a crash
    between marking and queueing loses nothing; `_queue_ping` sends it once per ticket, not once
    per commit, and `_retry_pending` drops it if the tip moves before it goes out."""
    for tid, commit in sorted(marks.items()):
        try:
            t = tk.load_ticket(tid)
            if t["status"] not in ("claimed", "review") or not _leftovers_stands(t, commit, settings, warn):
                continue
            _queue_ping(tid, "leftovers", state, warn, sup["leftovers"][tid]["reason"], commit)
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{tid}: could not queue the leftovers ping: {exc}")


def _save_supervisor(sup: dict, warn) -> None:
    try:
        sr.save_state(sup)
    except OSError as exc:
        warn(f"cannot save {sr.state_path()} ({exc}); a classification may be lost")


def _run_supervisor(sup: dict, settings: dict, now: datetime, busy: set, warn) -> None:
    """After the recovery records are written: let the supervisor look at ONE ticket whose
    latest commit failed review, in the background. A ticket failing this cycle (it has a
    record), or held or locked, is skipped. Nothing here waits for the model."""
    try:
        candidates = []
        for t in tk.list_tickets():
            v = t.get("verdict")
            if t["status"] != "claimed" or v is None or v["result"] != "FAIL" or t["id"] in busy:
                continue
            repo = settings["projects_dir"] / t["repo"]
            if repo.is_dir() and _tip(repo, t["branch"]) == v["commit"]:
                candidates.append(t)
        sr.start_next(candidates, sup, settings, now, busy, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"supervisor: {exc}")
    live = {t["id"] for t in tk.list_tickets() if t["status"] in ("claimed", "review")}
    before = json.dumps(sup, sort_keys=True)
    sr.prune(sup, live)
    if json.dumps(sup, sort_keys=True) != before:
        _save_supervisor(sup, warn)


def check(settings: dict, now: datetime, emit, warn, skip=frozenset()) -> None:
    """One watch cycle's pass over every claimed (or in-review) build or fix ticket.
    Tickets in `skip` (their agent was just sent a note) are not judged this cycle."""
    try:
        locked = prep.locked_ids()   # a prep agent is working on these branches: not ours to judge now
    except OSError as exc:   # before anything is sent, so a cycle that stops here leaves no half-saved state
        warn(f"prep state unreadable ({exc}); not judging any ticket this cycle")
        return
    state = _load_state(warn)
    sup = sr.load_state(warn)
    if sr.collect(sup, warn, settings):   # answers that came in since the last cycle count from this one
        _save_supervisor(sup, warn)
    progress = sr.progress_commits(sup)
    marks = {tid: m["commit"] for tid, m in sup["leftovers"].items()}
    _queue_leftovers_pings(marks, sup, state, settings, warn)
    _retry_pending(state, warn, settings)
    process_recoveries(settings, state, now, emit, warn)
    recovering = {r["id"] for r in pending_recoveries(warn)}   # a ticket with a record is never checked again
    recovering |= locked | set(skip) | governor.held_ids()   # stopped by the usage governor: not failed
    tickets = sorted((t for t in tk.list_tickets()
                      if t["status"] in ("claimed", "review") and t["type"] in dp.LAUNCH_TYPES
                      and t.get("assignee") in tk.AGENTS and t["id"] not in recovering), key=tk.oldest_first_key)
    try:
        agents = {a.get("name"): a for a in lc.list_agents() if a.get("name")} if tickets else {}
        known = True
    except lc.LaunchError as exc:
        warn(f"cannot check agents for review status or alerts: {exc}")
        agents, known = {}, False
    waiting = sr.awaiting(tickets, sup, now)   # the backstop waits for the supervisor on these
    moved: list[tuple[dict, str]] = []
    for t in tickets:
        if not known and t["status"] == "claimed":
            continue  # nothing here can be decided without herdr
        try:
            _check_one(t, agents.get(t["id"].lower()), known, settings, now, state, emit, warn, moved, progress, marks, waiting)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{t['id']}: {exc}")
    for new, tip in moved:   # judged against the final set of waiting tickets, not one at a time
        try:
            _ready_ping(new, tip, settings, now, state, warn)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{new['id']}: {exc}")
    try:   # the statuses _check_one just settled, not the snapshot taken before it
        current = {t["id"]: t for t in tk.list_tickets()}
        _ready_reminders([current[t["id"]] for t in tickets if t["id"] in current], state, now, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
        warn(f"ready reminders: {exc}")
    try:
        revive_failed(settings, emit, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"revive check: {exc}")
    try:
        _flag_stuck_oncall(settings, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"on-call check: {exc}")
    process_recoveries(settings, state, now, emit, warn)
    failing = {r["id"] for r in pending_recoveries(warn)}
    busy = recovering | failing
    try:
        if sr.continue_loops(sup, settings, failing, busy, emit, warn):
            _save_supervisor(sup, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"supervisor restart: {exc}")
    _run_supervisor(sup, settings, now, busy | set(sup["pending"]), warn)
    live = {t["id"] for t in tickets} | locked
    for name in ("blocked_since", "stopped_since", "seen", "fresh", "dialog_pinged", "ready_since"):
        state[name] = {k: v for k, v in state[name].items() if k in live}
    state["unrecorded_seen"] = {k: n for k, n in state["unrecorded_seen"].items()
                                if k.split(":", 1)[0] in live}
    # Keys of the failure pings stay: their ticket is closed, and the key is the record that it was sent.
    kept = [k for k in state["sent"] if k.split(":", 1)[0] in live]
    kept_failures = [k for k in state["sent"]
                     if k.split(":", 1)[0] not in live and k.rsplit(":", 1)[-1] in FAILURE_EVENTS]
    state["sent"] = kept + kept_failures[-FAILURE_KEYS_KEPT:]
    try:
        _save_state(state)
    except OSError as exc:
        warn(f"cannot save {state_path()} ({exc}); alerts may repeat")
