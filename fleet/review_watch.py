"""What `dispatch --watch` does about claimed tickets each cycle.

* A claimed build or fix ticket becomes READY, and moves to status `review`
  (Kaneo: In Review), when all hold: its recorded verdict is a PASS that meets
  the merge gate for the branch's exact tip, a SUMMARY note was recorded after
  that review, and its herdr agent is idle or done. One "ready to ship" ping.
  If that stops being true (the tip moved, the agent is busy again) it goes
  back to claimed.
* Pings for: a recorded FAIL (names the reviewer), a review that ran but could
  not be recorded, an agent blocked for over 10 minutes, and a claimed ticket
  with no activity for 30 minutes.

Every state is decided from the stored ticket (verdict, notes), the git branch,
the review files in the task folder, herdr and the dispatcher log. The agent's
own words are never parsed: only that a note exists, and when it was saved.
Nothing is pinged twice for the same commit (or the same stall or block): what
was sent is remembered in $FLEET_HOME/review-watch.json. Plain Python.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch as dp
from . import kaneo
from . import launch as lc
from . import notify
from . import risk as rk
from . import ticket as tk

BLOCKED_AFTER = 10 * 60   # seconds an agent waits for input before a ping
STALL_AFTER = 30 * 60     # seconds with no commit, note or log output before a ping
UNRECORDED_GRACE = 2 * 60  # seconds review.sh gets to record a review it just saved
SENT_LIMIT = 1000
LOG_TAIL = 256 * 1024
# Dispatcher log kinds that show a ticket's work moving. (RECONCILE_SKIP and
# DECISION lines repeat every cycle and say nothing about the agent.)
ACTIVITY_LOG_KINDS = ("LAUNCHED", "RELAUNCHED", "REVIEW_READY", "REVIEW_BACK")
LEFTOVERS_PREFIX = "SHOULD FIX leftovers from"  # saved by `ticket review-result`, not a summary


def state_path() -> Path:
    return tk.fleet_home() / "review-watch.json"


def _load_state(warn) -> dict:
    state = {"sent": [], "blocked_since": {}, "seen": {}}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("sent"), list)
                and isinstance(data.get("blocked_since"), dict) and isinstance(data.get("seen"), dict)):
            state.update(sent=[k for k in data["sent"] if isinstance(k, str)],
                         blocked_since=data["blocked_since"], seen=data["seen"])
        else:
            warn(f"{state_path()} has an unexpected shape; starting with an empty one")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        warn(f"cannot read {state_path()} ({exc}); starting with an empty one")
    return state


def _save_state(state: dict) -> None:
    state["sent"] = state["sent"][-SENT_LIMIT:]
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
    return out.stdout.strip() or None if out.returncode == 0 else None


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


def _alert_pings(t: dict, repo: Path, tip: str, agent: dict | None, state: dict,
                 now: datetime, emit, warn) -> None:
    tid, v = t["id"], t["verdict"]
    # 1. A recorded FAIL of this exact tip.
    if v is not None and v["commit"] == tip and v["result"] == "FAIL":
        failed = sorted((k, r["reviewer"]) for k, r in v["reviews"].items() if r["result"] == "FAIL")
        fresh = [(k, who) for k, who in failed if f"{tid}:review_failed:{tip}:{k}" not in state["sent"]]
        if fresh:
            detail = "failed by " + ", ".join(who for _, who in fresh)
            if notify.notify_quietly(t, "review_failed", warn, detail):
                state["sent"] += [f"{tid}:review_failed:{tip}:{k}" for k, _ in fresh]
            emit(f"{tid}  review FAILED ({detail})")
            dp.log("ALERT", f"{tid} review_failed {tip[:8]} {detail}")
    # 2. A review that was saved for this tip but never reached the ticket.
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
    # 3. An agent waiting for a person.
    if agent is not None and agent.get("agent_status") == "blocked":
        since = state["blocked_since"].setdefault(tid, _stamp(now))
        waited = (now - (_parse(since) or now)).total_seconds()
        if waited >= BLOCKED_AFTER and _ping(
                t, "blocked", f"{tid}:blocked:{since}", state, warn,
                f"waiting for input for {int(waited // 60)} min"):
            emit(f"{tid}  agent blocked for over {BLOCKED_AFTER // 60} minutes")
            dp.log("ALERT", f"{tid} blocked since {since}")
    else:
        state["blocked_since"].pop(tid, None)
    # 4. Nothing has happened for a long time.
    last = _last_activity(t, repo, tip, agent, state, now)
    idle = (now - last).total_seconds()
    if idle >= STALL_AFTER and _ping(t, "stalled", f"{tid}:stalled:{_stamp(last)}", state, warn,
                                     f"no activity for {int(idle // 60)} min"):
        emit(f"{tid}  stalled: no commit, note or log output for {int(idle // 60)} minutes")
        dp.log("ALERT", f"{tid} stalled since {_stamp(last)}")


# --- the cycle -----------------------------------------------------------------

def _move(t: dict, status: str, allowed_from: tuple, warn):
    try:
        return tk.set_status(t["id"], status, allowed_from=allowed_from)
    except tk.TicketError as exc:
        dp.log("RECONCILE_SKIP", f"{t['id']} {exc}")
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
            return
        new = _move(t, "claimed", ("review",), warn)
        if new:
            why = "agent is busy again" if earned else "its reviewed commit or summary no longer matches"
            emit(f"{t['id']}  back to In Progress ({why})")
            dp.log("REVIEW_BACK", f"{t['id']} back to claimed: {why}")
            kaneo.mirror_quietly(new, warn)
        return

    if earned and stopped:
        new = _move(t, "review", ("claimed",), warn)
        if new:
            emit(f"{t['id']}  ready to ship: moved to In Review")
            dp.log("REVIEW_READY", f"{t['id']} {tip[:8]} ready to ship")
            kaneo.mirror_quietly(new, warn)
            _ping(new, "ship", f"{t['id']}:ship:{tip}", state, warn)
        return
    _alert_pings(t, repo, tip, agent, state, now, emit, warn)


def check(settings: dict, now: datetime, emit, warn) -> None:
    """One watch cycle's pass over every claimed (or in-review) build or fix ticket."""
    state = _load_state(warn)
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
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{t['id']}: {exc}")
    live = {t["id"] for t in tickets}
    for name in ("blocked_since", "seen"):
        state[name] = {k: v for k, v in state[name].items() if k in live}
    try:
        _save_state(state)
    except OSError as exc:
        warn(f"cannot save {state_path()} ({exc}); alerts may repeat")
