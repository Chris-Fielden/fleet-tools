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
  failed and an on-call fix ticket is filed (launched by the dispatch cycle in
  its own slot); see _fail_ticket. A mid-loop FAIL verdict never pings.
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
from . import ticket as tk

BLOCKED_AFTER = 20 * 60   # seconds an agent waits for input before the ticket fails
STALL_AFTER = 45 * 60     # seconds with no commit, note or log output before the ticket fails
STALL_STATES = ("idle", "done", "blocked", "waiting")   # herdr states in which a stall counts; never "working"
STOPPED_AFTER = 5 * 60    # seconds an agent sits idle on a failed tip before the ticket fails
UNRECORDED_GRACE = 2 * 60  # seconds review.sh gets to record a review it just saved
LOG_TAIL = 256 * 1024
MUST_FIX_PART = 500       # characters of a review's MUST FIX section per part of the on-call body
MUST_FIX_PARTS = 6        # parts kept per review; the context before the spec is clipped to leave the spec whole
# Dispatcher log kinds that show a ticket's work moving. (RECONCILE_SKIP and
# DECISION lines repeat every cycle and say nothing about the agent.)
ACTIVITY_LOG_KINDS = ("LAUNCHED", "RELAUNCHED", "REVIEW_READY", "REVIEW_BACK")
FAILURE_EVENTS = ("oncall", "needs_human")
LAUNCH_FAILED_NOTE = "Launch attempt failed:"   # written by launch.launch_oncall
LAUNCH_ATTEMPTS = 3       # failed launches of one on-call ticket before it is given up
FAILURE_KEYS_KEPT = 500   # sent-keys of closed tickets' failure pings; the oldest go first
LEFTOVERS_PREFIX = "SHOULD FIX leftovers from"  # saved by `ticket review-result`, not a summary


def state_path() -> Path:
    return tk.fleet_home() / "review-watch.json"


def _load_state(warn) -> dict:
    state = {"sent": [], "blocked_since": {}, "stopped_since": {}, "seen": {}, "pending": [], "fresh": {},
             "dialog_pinged": {}}
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
                         fresh={k: v for k, v in (data["fresh"] if isinstance(data.get("fresh"), dict) else {}).items()
                                if isinstance(k, str) and isinstance(v, str)},
                         pending=[p for p in data.get("pending", []) if isinstance(p, dict)
                                  and isinstance(p.get("id"), str) and p.get("event") in FAILURE_EVENTS
                                  and isinstance(p.get("detail"), (str, type(None)))])
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


def _queue_ping(ticket_id: str, event: str, state: dict, warn, detail: str | None = None) -> None:
    """Queue the one ping of a failure (event oncall or needs_human), once per ticket
    and event, and save it to disk at once: it is queued BEFORE the ticket is closed,
    so an interruption cannot lose it. `_retry_pending` sends it, and keeps trying
    every cycle (even though the closed ticket is no longer visited) until it
    goes through."""
    if f"{ticket_id}:{event}" in state["sent"] or any(
            p["id"] == ticket_id and p["event"] == event for p in state["pending"]):
        return
    state["pending"].append({"id": ticket_id, "event": event, "detail": detail})
    try:
        _save_state(state)
    except OSError:
        state["pending"].pop()
        raise   # nothing is closed until the ping is safely on disk


def _retry_pending(state: dict, warn) -> None:
    keep = []
    for p in state["pending"]:
        try:
            ticket = tk.load_ticket(p["id"])
        except (tk.TicketError, tk.TicketInvalid):
            continue   # gone: nothing to tell anyone about
        if notify.notify_quietly(ticket, p["event"], warn, p.get("detail")):
            state["sent"].append(f"{p['id']}:{p['event']}")
        else:
            keep.append(p)
    state["pending"] = keep


def _unrecorded_pings(t: dict, repo: Path, tip: str, state: dict, now: datetime, emit, warn) -> None:
    """A review that was saved for this tip but never reached the ticket."""
    tid, v = t["id"], t["verdict"]
    wt = repo / ".worktrees" / t["branch"].removeprefix("task/")
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
        detail = f"{info['reviewer']} review of {tip[:7]} ({info['verdict'] or 'no verdict'}) is not on the ticket"
        if _ping(t, "review_unrecorded", key, state, warn, detail):
            emit(f"{tid}  {detail}")
            dp.log("ALERT", f"{tid} review_unrecorded {tip[:8]} {kind}")


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

def fail_rounds(t: dict) -> int:
    """Different commits that got a recorded FAIL."""
    v = t["verdict"]
    commits = set(t.get("fail_commits", []))
    if v is not None and v["result"] == "FAIL":
        commits.add(v["commit"])
    return len(commits)


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
    dp.log("AGENT_DIALOG", f"{tid} tab={tab} {label}: {flat}")
    notify.notify_quietly(t, "dialog", detail="\x1f".join((label, tab, flat)))


def _failure_reason(t: dict, repo: Path, tip: str, agent: dict | None, state: dict,
                    now: datetime, fail_rounds_threshold: int) -> str | None:
    """Why this claimed ticket has truly failed, or None. Keeps the clocks for
    the stopped and blocked spells in `state`."""
    tid, v = t["id"], t["verdict"]
    failed_now = v is not None and v["result"] == "FAIL"
    agent_state = agent.get("agent_status") if agent else None
    reason = None
    # 1. Going in circles.
    rounds = fail_rounds(t)
    if failed_now and rounds >= fail_rounds_threshold:
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


def write_recovery(t: dict, reason: str, commit: str) -> None:
    """Record, atomically, that this ticket has truly failed. This is the FIRST thing
    done; everything else (close, on-call ticket, notes, ping) is driven from the
    record by `process_recoveries`. One record per ticket; raises OSError."""
    path = _record_path(t["id"])
    tk.refuse_real_store_in_tests(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tk._write_atomic(path, {"id": t["id"], "commit": commit, "reason": reason, "reviewer": _failed_reviewers(t),
                            "rounds": fail_rounds(t), "at": _stamp(datetime.now(timezone.utc))})


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
                  and isinstance(rec.get("rounds"), int))
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
    if tk.check_secrets("close note", detail):   # the reviewer name is free text: scan it like any note
        detail = f"{rec['reason']}; FAIL rounds: {rec['rounds']} (reviewer name withheld)"
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
    _retry_pending(state, warn)


def process_recoveries(settings: dict, state: dict, now: datetime, emit, warn) -> None:
    for rec in pending_recoveries(warn):
        try:
            _recover(rec, settings, state, now, emit, warn)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{rec['id']}: recovery is not finished and will be retried next cycle ({exc})")


# --- the cycle -----------------------------------------------------------------

def _move(t: dict, status: str, allowed_from: tuple, warn):
    try:
        return tk.set_status(t["id"], status, allowed_from=allowed_from)
    except tk.TicketError as exc:
        dp.log("REVIEW_SKIP", f"{t['id']} {exc}")
        warn(f"{t['id']}: could not move to {status}: {exc}")
        return None


def _check_one(t: dict, agent: dict | None, agents_known: bool, settings: dict,
               now: datetime, state: dict, emit, warn, moved: list | None = None) -> None:
    repo = settings["projects_dir"] / t["repo"]
    if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
        return
    tip = _tip(repo, t["branch"])
    if tip is None:
        return
    agent_state = agent.get("agent_status") if agent else None
    stopped = agent_state in ("idle", "done")
    earned = passed_for_tip(t, tip) and has_summary_after_review(t)

    if t["status"] == "review":
        # Leave it alone if herdr cannot be asked; a vanished agent is not busy.
        busy = agents_known and agent is not None and not stopped
        if earned and not busy:
            if moved is not None:
                moved.append((t, tip))   # like a ticket that just moved: judged after every move of the cycle
            else:
                _ready_ping(t, tip, settings, now, state, warn)
            _end_spells(t["id"], state)
            _unrecorded_pings(t, repo, tip, state, now, emit, warn)
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
            _unrecorded_pings(new, repo, tip, state, now, emit, warn)
        return
    if fresh.refresh_if_large(t, agent, tip, settings, state, emit, warn):
        return   # a new session starts from here: judge it from the next cycle
    _unrecorded_pings(t, repo, tip, state, now, emit, warn)
    reason = _failure_reason(t, repo, tip, agent, state, now, settings["fail_rounds_threshold"])
    if reason:
        write_recovery(t, reason, tip)   # first: everything else is driven from the record


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


def check(settings: dict, now: datetime, emit, warn, skip=frozenset()) -> None:
    """One watch cycle's pass over every claimed (or in-review) build or fix ticket.
    Tickets in `skip` (their agent was just sent a note) are not judged this cycle."""
    try:
        locked = prep.locked_ids()   # a prep agent is working on these branches: not ours to judge now
    except OSError as exc:   # before anything is sent, so a cycle that stops here leaves no half-saved state
        warn(f"prep state unreadable ({exc}); not judging any ticket this cycle")
        return
    state = _load_state(warn)
    _retry_pending(state, warn)
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
    moved: list[tuple[dict, str]] = []
    for t in tickets:
        if not known and t["status"] == "claimed":
            continue  # nothing here can be decided without herdr
        try:
            _check_one(t, agents.get(t["id"].lower()), known, settings, now, state, emit, warn, moved)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{t['id']}: {exc}")
    for new, tip in moved:   # judged against the final set of waiting tickets, not one at a time
        try:
            _ready_ping(new, tip, settings, now, state, warn)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{new['id']}: {exc}")
    try:
        _flag_stuck_oncall(settings, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"on-call check: {exc}")
    process_recoveries(settings, state, now, emit, warn)
    live = {t["id"] for t in tickets} | locked
    for name in ("blocked_since", "stopped_since", "seen", "fresh", "dialog_pinged"):
        state[name] = {k: v for k, v in state[name].items() if k in live}
    # Keys of the failure pings stay: their ticket is closed, and the key is the record that it was sent.
    kept = [k for k in state["sent"] if k.split(":", 1)[0] in live]
    kept_failures = [k for k in state["sent"]
                     if k.split(":", 1)[0] not in live and k.rsplit(":", 1)[-1] in FAILURE_EVENTS]
    state["sent"] = kept + kept_failures[-FAILURE_KEYS_KEPT:]
    try:
        _save_state(state)
    except OSError as exc:
        warn(f"cannot save {state_path()} ({exc}); alerts may repeat")
