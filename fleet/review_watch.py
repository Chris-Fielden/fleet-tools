"""What `dispatch --watch` does about claimed tickets each cycle.

* A claimed build or fix ticket becomes READY, and moves to status `review`
  (Kaneo: In Review), when all hold: its recorded verdict is a PASS that meets
  the merge gate for the branch's exact tip, a SUMMARY note was recorded after
  that review, and its herdr agent is idle or done. One "ready to ship" ping.
  If that stops being true (the tip moved, the agent is busy again) it goes
  back to claimed.
* A claimed ticket TRULY FAILS when its agent sat idle on a failed commit for
  5 minutes, it has 3 FAIL verdicts on different commits, its agent was blocked
  for 20 minutes, or it had no activity for 45 minutes. It is then closed as
  failed and an on-call fix ticket is filed (launched by the dispatch cycle in
  its own slot); see _fail_ticket. A mid-loop FAIL verdict never pings.
* The only ping here besides "ready to ship" is a review that ran but could
  not be recorded; failures ping once, low ("on-call is on it" or "needs a
  human").

Every state is decided from the stored ticket (verdict, notes), the git branch,
the review files in the task folder, herdr and the dispatcher log. The agent's
own words are never parsed: only that a note exists, and when it was saved.
Nothing is pinged twice for the same commit (or the same stall or block): what
was sent is remembered in $FLEET_HOME/review-watch.json. Plain Python.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch as dp
from . import kaneo
from . import launch as lc
from . import notify
from . import risk as rk
from . import ticket as tk

BLOCKED_AFTER = 20 * 60   # seconds an agent waits for input before the ticket fails
STALL_AFTER = 45 * 60     # seconds with no commit, note or log output before the ticket fails
STOPPED_AFTER = 5 * 60    # seconds an agent sits idle on a failed tip before the ticket fails
FAIL_ROUNDS = 3           # FAIL verdicts on different commits before the ticket fails
UNRECORDED_GRACE = 2 * 60  # seconds review.sh gets to record a review it just saved
LOG_TAIL = 256 * 1024
# Dispatcher log kinds that show a ticket's work moving. (RECONCILE_SKIP and
# DECISION lines repeat every cycle and say nothing about the agent.)
ACTIVITY_LOG_KINDS = ("LAUNCHED", "RELAUNCHED", "REVIEW_READY", "REVIEW_BACK")
FAILURE_EVENTS = ("oncall", "needs_human")
FAILURE_KEYS_KEPT = 500   # sent-keys of closed tickets' failure pings; the oldest go first
LEFTOVERS_PREFIX = "SHOULD FIX leftovers from"  # saved by `ticket review-result`, not a summary


def state_path() -> Path:
    return tk.fleet_home() / "review-watch.json"


def _load_state(warn) -> dict:
    state = {"sent": [], "blocked_since": {}, "stopped_since": {}, "seen": {}, "pending": []}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("sent"), list)
                and isinstance(data.get("blocked_since"), dict) and isinstance(data.get("seen"), dict)
                and isinstance(data.get("stopped_since", {}), dict)
                and isinstance(data.get("pending", []), list)):
            state.update(sent=[k for k in data["sent"] if isinstance(k, str)],
                         blocked_since=data["blocked_since"], seen=data["seen"],
                         stopped_since=data.get("stopped_since", {}),
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
    return any(n["by"] == t.get("assignee") and n["at"] >= v["at"]
               and not n["text"].startswith(LEFTOVERS_PREFIX) for n in t.get("notes", []))


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


def _notify_failure(ticket: dict, event: str, state: dict, warn, detail: str | None = None) -> None:
    """The one ping of a failure (event oncall or needs_human), once per ticket and
    event. The ticket is closed by now, so the watch cycle no longer visits it: a
    send that failed is kept in state["pending"] and retried every cycle until it
    goes through."""
    key = f"{ticket['id']}:{event}"
    if key in state["sent"] or any(p["id"] == ticket["id"] and p["event"] == event for p in state["pending"]):
        return
    if notify.notify_quietly(ticket, event, warn, detail):
        state["sent"].append(key)
    else:
        state["pending"].append({"id": ticket["id"], "event": event, "detail": detail})


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
#   * it has FAIL_ROUNDS or more FAIL verdicts on different commits, the latest
#     verdict still being a FAIL (going in circles); or
#   * its agent has waited for input for BLOCKED_AFTER seconds (permission
#     prompts are never answered); or
#   * nothing has happened for STALL_AFTER seconds.
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


def _failure_reason(t: dict, repo: Path, tip: str, agent: dict | None, state: dict,
                    now: datetime) -> str | None:
    """Why this claimed ticket has truly failed, or None. Keeps the clocks for
    the stopped and blocked spells in `state`."""
    tid, v = t["id"], t["verdict"]
    failed_now = v is not None and v["result"] == "FAIL"
    agent_state = agent.get("agent_status") if agent else None
    reason = None
    # 1. Going in circles.
    rounds = fail_rounds(t)
    if failed_now and rounds >= FAIL_ROUNDS:
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
        since = state["blocked_since"].setdefault(tid, _stamp(now))
        waited = (now - (_parse(since) or now)).total_seconds()
        if reason is None and waited >= BLOCKED_AFTER:
            reason = f"the agent was blocked waiting for input for {int(waited // 60)} min"
    else:
        state["blocked_since"].pop(tid, None)
    # 4. Nothing has happened for a long time.
    last = _last_activity(t, repo, tip, agent, state, now)
    idle = (now - last).total_seconds()
    if reason is None and idle >= STALL_AFTER:
        reason = f"stalled: no commit, note, log output or agent change for {int(idle // 60)} min"
    return reason


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


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
    """What the on-call agent needs, in one ticket body (4000 characters at most):
    the failed ticket's spec and notes, the latest reviews' MUST FIX sections,
    why it failed and the log tail."""
    v = t["verdict"]
    reviewed = v["commit"] if v is not None and v["result"] == "FAIL" else tip
    parts = [f"On-call fix for {t['id']} ({t['type']}): {_clip(t['title'], 100)}",
             f"WHY IT FAILED: {detail}",
             f"FAILED BRANCH: {t['branch']} at {tip}. Your new branch starts from that commit. "
             "The failed agent's herdr tab may still be open: do not rely on that branch staying put.",
             "FAILED TICKET SPEC:\n" + _clip(t["body"], 1200)]
    notes = [f"- {n['by']} {n['at']}: {_clip(n['text'], 250)}" for n in t.get("notes", [])[-4:]]
    if notes:
        parts.append("FAILED TICKET NOTES (latest):\n" + "\n".join(notes))
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
            parts.append(f"MUST FIX from the latest {info['reviewer']} review ({reviewed[:7]}):\n"
                         + _clip(must, 500))
    tail = _log_tail(t["id"])
    if tail:
        parts.append("DISPATCHER LOG TAIL:\n" + _clip(tail, 600))
    return _clip("\n\n".join(parts), tk.BODY_MAX)


def _note(ticket_id: str, text: str, warn) -> None:
    try:
        tk.add_note(ticket_id, _clip(text, tk.NOTE_MAX))
    except (tk.TicketError, tk.TicketInvalid) as exc:
        warn(f"{ticket_id}: could not save a note ({exc})")


def _file_oncall(t: dict, detail: str, tip: str, repo: Path, warn) -> dict | None:
    """File the on-call fix ticket for a failed ticket; None if it cannot be made."""
    fields = {"type": "fix", "repo": t["repo"], "branch": f"task/oc-{t['id'].lower()}",
              "title": _clip(f"On-call: fix {t['id']}: {t['title']}", 100), "parent": t["id"],
              "body": _oncall_body(t, detail, tip, repo)}
    try:
        try:
            return tk.create_ticket(fields, oncall=True, oncall_base=tip)
        except tk.TicketInvalid:
            # The copied text tripped the secret scan or a limit: point at the ticket instead.
            fields["body"] = (f"On-call fix for {t['id']}. Why it failed: {_clip(detail, 300)}. "
                              f"Failed branch {t['branch']} at {tip}. Run `ticket show {t['id']}` "
                              "for its spec and notes.")
            return tk.create_ticket(fields, oncall=True, oncall_base=tip)
    except (tk.TicketError, tk.TicketInvalid) as exc:
        warn(f"{t['id']}: could not file the on-call ticket: {exc}")
        return None


def _fail_ticket(t: dict, reason: str, repo: Path, tip: str, settings: dict, state: dict,
                 emit, warn) -> None:
    """A ticket has truly failed: close it as failed (its card moves to Failed),
    then file ONE on-call ticket, which the dispatch cycle launches at once in
    the on-call slot. If it is the on-call ticket that failed, or an on-call
    attempt was already made, or the daily on-call limit is reached, nothing
    more is launched: the ticket gets a diagnosis note and one "needs a human"
    ping. Never pings for a mid-loop FAIL."""
    tid = t["id"]
    detail = f"{reason}; failed by: {_failed_reviewers(t)}; FAIL rounds: {fail_rounds(t)}"
    if tk.check_secrets("close note", detail):   # the reviewer name is free text: scan it like any note
        detail = f"{reason}; FAIL rounds: {fail_rounds(t)} (reviewer name withheld)"
    try:
        failed = tk.set_status(tid, "failed", allowed_from=("claimed",),
                               close_note=_clip(f"Failed: {detail}", tk.CLOSE_NOTE_MAX))
    except tk.TicketError as exc:
        lc._safe_log("FAIL_SKIP", f"{tid} {exc}")
        warn(f"{tid}: could not close as failed: {exc}")
        return
    kaneo.mirror_quietly(failed, warn)
    emit(f"{tid}  failed: {detail}; its agent's herdr tab is left open (close it by hand if it is still there)")
    lc._safe_log("FAILED", f"{tid} {_clip(detail, 200)}")
    tickets = tk.list_tickets()
    if t.get("oncall"):
        original = t["parent"]
        diagnosis = (f"Diagnosis: on-call {tid} could not fix {original}. {detail}. Its branch "
                     f"{t['branch']} ends at {tip}. Nothing more will be launched for it.")
        _note(tid, diagnosis, warn)
        _human(original, diagnosis, state, warn)
        return
    cap = settings["max_oncall_per_day"]
    if any(x.get("oncall") and x["parent"] == tid for x in tickets):
        why = "an on-call attempt was already made for it"
    elif dp.oncall_today(tickets) >= cap:
        why = f"the daily on-call limit ({cap}) is reached"
    else:
        oc = _file_oncall(failed, detail, tip, repo, warn)
        if oc is not None:
            kaneo.mirror_quietly(oc, warn)
            _note(tid, f"On-call ticket {oc['id']} was filed to fix this. {detail}.", warn)
            lc._safe_log("ONCALL_FILED", f"{oc['id']} for {tid}")
            _notify_failure(failed, "oncall", state, warn, oc["id"])
            return
        why = "the on-call ticket could not be filed"
    _human(tid, f"Needs a human: {why}. {detail}.", state, warn)


def _human(ticket_id: str, note: str, state: dict, warn) -> None:
    """Note why a human must look at a failed ticket and send the one low ping."""
    _note(ticket_id, note, warn)
    try:
        ticket = tk.load_ticket(ticket_id)
    except (tk.TicketError, tk.TicketInvalid) as exc:
        warn(f"{ticket_id}: {exc}")
        return
    lc._safe_log("NEEDS_HUMAN", f"{ticket_id}")
    _notify_failure(ticket, "needs_human", state, warn)


# --- the cycle -----------------------------------------------------------------

def _move(t: dict, status: str, allowed_from: tuple, warn):
    try:
        return tk.set_status(t["id"], status, allowed_from=allowed_from)
    except tk.TicketError as exc:
        dp.log("REVIEW_SKIP", f"{t['id']} {exc}")
        warn(f"{t['id']}: could not move to {status}: {exc}")
        return None


def _check_one(t: dict, agent: dict | None, agents_known: bool, settings: dict,
               now: datetime, state: dict, emit, warn) -> None:
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
            _ping(t, "ship", f"{t['id']}:ship:{tip}", state, warn)  # a send that failed is retried
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
        else:
            return
    elif earned and stopped:
        new = _move(t, "review", ("claimed",), warn)
        if new:
            emit(f"{t['id']}  ready to ship: moved to In Review")
            dp.log("REVIEW_READY", f"{t['id']} {tip[:8]} ready to ship")
            kaneo.mirror_quietly(new, warn)
            _ping(new, "ship", f"{t['id']}:ship:{tip}", state, warn)
            _end_spells(t["id"], state)
            _unrecorded_pings(new, repo, tip, state, now, emit, warn)
        return
    _unrecorded_pings(t, repo, tip, state, now, emit, warn)
    reason = _failure_reason(t, repo, tip, agent, state, now)
    if reason:
        _fail_ticket(t, reason, repo, tip, settings, state, emit, warn)


def _escalate_stuck_oncall(settings: dict, state: dict, emit, warn) -> None:
    """An on-call ticket that is open although its branch already exists is one
    whose launch failed and left its folder or branch behind (they may hold
    work), so it can never be launched. Close it as failed and tell a human."""
    for oc in tk.list_tickets():
        if not oc.get("oncall") or oc["status"] != "open":
            continue
        repo = settings["projects_dir"] / oc["repo"]
        if repo.resolve().parent != settings["projects_dir"].resolve() or not repo.is_dir():
            continue
        folder = repo / ".worktrees" / oc["branch"].removeprefix("task/")
        if not dp.branch_exists(repo, oc["branch"]) and not (folder.exists() or folder.is_symlink()):
            continue
        diagnosis = (f"Diagnosis: the on-call ticket for {oc['parent']} cannot be launched: {oc['branch']} "
                     "or its task folder already exists (a failed launch left it, or someone made it). Nothing more will be launched for it.")
        try:
            closed = tk.set_status(oc["id"], "failed", allowed_from=("open",),
                                   close_note=_clip(f"Failed: {diagnosis}", tk.CLOSE_NOTE_MAX))
        except tk.TicketError:
            continue
        kaneo.mirror_quietly(closed, warn)
        emit(f"{oc['id']}  failed: on-call launch left {oc['branch']} behind")
        lc._safe_log("FAILED", f"{oc['id']} on-call launch left {oc['branch']} behind")
        _note(oc["id"], diagnosis, warn)
        _human(oc["parent"], diagnosis, state, warn)


def check(settings: dict, now: datetime, emit, warn) -> None:
    """One watch cycle's pass over every claimed (or in-review) build or fix ticket."""
    state = _load_state(warn)
    _retry_pending(state, warn)
    tickets = sorted((t for t in tk.list_tickets()
                      if t["status"] in ("claimed", "review") and t["type"] in dp.LAUNCH_TYPES
                      and t.get("assignee") in tk.AGENTS), key=tk.oldest_first_key)
    try:
        agents = {a.get("name"): a for a in lc.list_agents() if a.get("name")} if tickets else {}
        known = True
    except lc.LaunchError as exc:
        warn(f"cannot check agents for review status or alerts: {exc}")
        agents, known = {}, False
    for t in tickets:
        if not known and t["status"] == "claimed":
            continue  # nothing here can be decided without herdr
        try:
            _check_one(t, agents.get(t["id"].lower()), known, settings, now, state, emit, warn)
        except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
            warn(f"{t['id']}: {exc}")
    try:
        _escalate_stuck_oncall(settings, state, emit, warn)
    except (tk.TicketError, tk.TicketInvalid, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"on-call check: {exc}")
    live = {t["id"] for t in tickets}
    for name in ("blocked_since", "stopped_since", "seen"):
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
