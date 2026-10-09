"""The usage governor: keeps the fleet inside the weekly plan limit.

`evaluate()` runs at the start of every dispatch cycle (not only when something is
waiting to launch) and reads the weekly figure the status line saved
(planusage.py). Two limits, both settings:

  pause_weekly_pct      (default 90) no agent of any kind is launched: normal,
                        urgent, on-call, investigations, reconcile, prep,
                        relaunches and fresh sessions. LAUNCH_PAUSED is logged and
                        one low ping sent when the figure crosses it.
  hard_stop_weekly_pct  (default 95) running agents are stopped too: each is given a
                        short time to finish its current tool call, then interrupted;
                        its work stays where it is; its ticket is held, with a note,
                        until the reset. One high ping is sent.

After the weekly reset time both lift by themselves and the held tickets get a
new agent. With no fresh figure nothing is paused (the old file is not trusted
for a decision to spend or not), but a pause already in force stays until the
reset time or a fresh figure below the limit.

State lives in $FLEET_HOME/governor.json: the ticket ids and the flags that keep
each ping and each stop to once per crossing. Nothing secret is written.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone

from . import dispatch as dp
from . import notify
from . import planusage
from . import ticket as tk
from .usage import _iso, _ts

STOP_BUDGET_SECONDS = 60
SAFE_POINT_MS = 20000   # how long an agent may keep working before it is interrupted
IDLE_STATES = ("idle", "done")
NOTE_PREFIX = "Usage governor:"


def state_path():
    return tk.fleet_home() / "governor.json"


def _empty() -> dict:
    return {"paused": False, "stopped": False, "reset_at": None, "since": None,
            "pct": None, "held": [], "held_jobs": [], "stopped_agents": [], "pinged": []}


def load_state() -> dict:
    out = _empty()
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for key in ("paused", "stopped"):
            out[key] = data.get(key) is True
        for key in ("reset_at", "since"):
            out[key] = data[key] if isinstance(data.get(key), str) else None
        out["pct"] = data["pct"] if isinstance(data.get("pct"), (int, float)) else None
        for key in ("held", "held_jobs", "stopped_agents", "pinged"):
            if isinstance(data.get(key), list):
                out[key] = [x for x in data[key] if isinstance(x, str)]
    return out


def save_state(state: dict) -> None:
    target = state_path()
    tk.refuse_real_store_in_tests(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def held_ids() -> frozenset:
    """Tickets whose agent the governor stopped: the dispatcher must not judge them
    (they are not stalled or lost) until the governor resumes them."""
    return frozenset(load_state()["held"])


def held_jobs() -> frozenset:
    """Names of prep and reconcile agents the governor stopped: their jobs are not finished
    (and their tickets stay locked) until the governor resumes them."""
    return frozenset(load_state()["held_jobs"])


def _limits(settings: dict | None) -> tuple[int, int]:
    from .settings import DEFAULTS, SettingsError, load_settings
    if settings is None:   # the backstop in start_agent: use the settings in force, never just the defaults
        try:
            settings = load_settings()
        except SettingsError:
            settings = DEFAULTS
    s = settings
    return s.get("pause_weekly_pct", DEFAULTS["pause_weekly_pct"]), \
        s.get("hard_stop_weekly_pct", DEFAULTS["hard_stop_weekly_pct"])


def _weekly(now: datetime) -> dict | None:
    """The weekly window if the figure is fresh and its week is not over, else None."""
    data = planusage.load(now)
    w = data["weekly"]
    if w is None or w["stale"] or data["age_seconds"] is None or data["age_seconds"] > planusage.MAX_AGE_SECONDS:
        return None
    return w


def refusal(settings: dict | None = None, now: datetime | None = None) -> str | None:
    """Why no agent may be launched now, or None. Reads the live figure as well as the
    saved state, so a launch between two cycles is refused too."""
    now = now or datetime.now(timezone.utc)
    pause, hard = _limits(settings)
    state = load_state()
    if state["paused"] or state["stopped"]:
        until = f" until the weekly reset ({state['reset_at']})" if state["reset_at"] else ""
        return f"launches are paused: weekly usage is at its limit{until}"
    w = _weekly(now)
    if w is not None:
        used = w["used_percent"]
        if (pause and used >= pause) or (hard and used >= hard):
            return f"launches are paused: weekly usage is {used:g}% (limit {pause or hard}%)"
    return None


def defer(ticket_id: str) -> None:
    """A claimed ticket could not get its agent because of the pause: hold it so it is
    started after the reset and nothing treats it as lost."""
    state = load_state()
    if ticket_id not in state["held"]:
        state["held"].append(ticket_id)
        save_state(state)


# --- the cycle -----------------------------------------------------------------

def _ping(state: dict, event: str, title: str, text: str, warn) -> None:
    if event in state["pinged"]:
        return
    try:
        notify.send_alert(event, title, text)
    except notify.NotConfigured:
        pass
    except Exception as exc:   # noqa: BLE001 - a ping must never stop the cycle
        warn(f"usage governor: ping not sent ({type(exc).__name__}); will try once more next cycle")
        return
    state["pinged"].append(event)


def _stop_agents(now: datetime, state: dict, emit, warn) -> bool:
    """Interrupt every running agent at a safe point. False if herdr could not be asked
    (nothing is marked done: the next cycle tries again)."""
    from . import launch as lc
    try:
        agents = [a for a in lc.list_agents() if a.get("name") and a.get("agent_status") not in IDLE_STATES]
    except lc.LaunchError as exc:
        warn(f"usage governor: cannot ask herdr which agents are running ({exc}); will try again")
        return False
    all_tickets = {t["id"].lower(): t for t in tk.list_tickets()}
    ok = True
    deadline = time.monotonic() + STOP_BUDGET_SECONDS   # many agents must not hold up the cycle for minutes
    for a in agents:
        name = a["name"]
        if name in state["stopped_agents"] and not state["stopped"]:
            continue   # (once stopped, every agent still working is interrupted again, each cycle)
        try:
            # Let the current tool call finish: wait (bounded) for the agent to stop working
            # or to sit at a prompt, then interrupt whatever it is still doing.
            wait_ms = int(max(0.0, min(SAFE_POINT_MS / 1000, deadline - time.monotonic())) * 1000)
            if wait_ms:
                with contextlib.suppress(lc.LaunchError):
                    lc.herdr_run("agent", "wait", name, "--until", "idle", "--until", "blocked",
                                 "--until", "done", "--timeout", str(wait_ms), timeout=wait_ms / 1000 + 10)
            lc.herdr_run("agent", "send-keys", name, "esc")
        except lc.LaunchError as exc:
            warn(f"usage governor: could not interrupt {name} ({exc}); will try again")
            ok = False
            continue
        if name not in state["stopped_agents"]:
            state["stopped_agents"].append(name)
        t = all_tickets.get(name)
        if t is not None and t["status"] not in ("claimed", "proposed", "open"):
            continue   # a ticket on its way to review: interrupted, but nothing to resume
        dp.log("AGENT_STOPPED", f"{name} interrupted at weekly {state['pct']:g}%")
        emit(f"{name}  stopped: weekly usage {state['pct']:g}% (resumes after the reset)")
        if t is None:   # a prep or reconcile agent: its job is held, not finished
            if name not in state["held_jobs"]:
                state["held_jobs"].append(name)
        elif t["id"] not in state["held"]:
            state["held"].append(t["id"])
            with contextlib.suppress(tk.TicketError, tk.TicketInvalid):
                tk.add_note(t["id"], f"{NOTE_PREFIX} agent stopped at weekly usage {state['pct']:g}%. Work is "
                                     f"left in its task folder; a new agent starts after the reset "
                                     f"({state['reset_at'] or 'time unknown'}).")
    return ok


def _resume_held(settings: dict, state: dict, emit, warn) -> None:
    from . import launch as lc
    try:
        listed = {a.get("name") for a in lc.list_agents()}
    except lc.LaunchError as exc:
        warn(f"usage governor: cannot ask herdr which agents are running ({exc}); will resume next cycle")
        return
    jobs_kept = []
    for name in state["held_jobs"]:   # prep / reconcile: still in their tab, so just tell them to go on
        if name not in listed:
            continue
        try:
            lc.herdr("agent", "prompt", name, "Weekly usage has reset; carry on with your job.")
        except lc.LaunchError as exc:
            warn(f"usage governor: {name}: {exc}; will try again")
            jobs_kept.append(name)
            continue
        emit(f"{name}  resumed after the usage reset")
        dp.log("USAGE_RESUMED", f"{name} resumed after the weekly reset")
    state["held_jobs"] = jobs_kept
    keep = []
    for tid in state["held"]:
        try:
            t = tk.load_ticket(tid)
        except (tk.TicketError, tk.TicketInvalid):
            continue
        if t["status"] != "claimed":
            continue
        if tid.lower() in listed:   # its tab is still open: tell the agent to carry on
            try:
                lc.herdr("agent", "prompt", tid.lower(), "Weekly usage has reset; carry on with your ticket.")
            except lc.LaunchError as exc:
                warn(f"usage governor: {tid}: {exc}")
                keep.append(tid)
                continue
            emit(f"{tid}  resumed after the usage reset")
        else:
            code, message = lc.relaunch(t, settings, warn, why=" (usage reset)")
            emit(message)
            if code:
                keep.append(tid)
                continue
        dp.log("USAGE_RESUMED", f"{tid} resumed after the weekly reset")
    state["held"] = keep


def evaluate(settings: dict, now: datetime, emit, warn) -> dict:
    """One cycle's look at the weekly figure. Never raises for a usage problem."""
    pause, hard = _limits(settings)
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    w = _weekly(now)
    reset = _ts(state["reset_at"]) if state["reset_at"] else None

    # Lift: the week is over, or a fresh figure is back under both limits.
    if state["paused"] or state["stopped"]:
        over = reset is not None and now >= reset
        under = w is not None and not ((pause and w["used_percent"] >= pause) or (hard and w["used_percent"] >= hard))
        if over or under:
            dp.log("LAUNCH_RESUMED", f"weekly usage window reset or below the limit; launches allowed again "
                                     f"({'reset' if over else 'figure ' + format(w['used_percent'], 'g') + '%'})")
            emit("usage governor: launches allowed again")
            state.update(paused=False, stopped=False, pinged=[], since=None, stopped_agents=[])
    if not (state["paused"] or state["stopped"]) and (state["held"] or state["held_jobs"]):
        if json.dumps(state, sort_keys=True) != before:
            save_state(state)   # the relaunches below read the saved state: it must already say "lifted"
        _resume_held(settings, state, emit, warn)

    if w is not None:
        used = w["used_percent"]
        if (hard and used >= hard) or (pause and used >= pause):
            state["pct"] = used
            state["reset_at"] = w["resets_at"] or state["reset_at"]
        stopping = bool(hard) and used >= hard
        if not state["paused"] and ((pause and used >= pause) or stopping):
            state["paused"] = True
            state["since"] = _iso(now)
            dp.log("LAUNCH_PAUSED", f"weekly usage {used:g}% reached the limit of {pause if pause and used >= pause else hard}%; "
                                    f"no agent will be launched until the reset ({state['reset_at'] or 'time unknown'})")
            emit(f"usage governor: weekly usage {used:g}%, launches paused")
        if stopping:
            was_stopped = state["stopped"]
            if _stop_agents(now, state, emit, warn) and not was_stopped:
                state["stopped"] = True
                dp.log("USAGE_STOP", f"weekly usage {used:g}% reached {hard}%; running agents stopped, "
                                     f"{len(state['held'])} ticket(s) held until the reset")
    # Pings are sent from the saved state, so one that could not be delivered is tried again next cycle.
    when = state["reset_at"] or "time unknown"
    if state["stopped"]:
        _ping(state, "usage_stop", "Agents stopped",
              f"Weekly usage is {state['pct'] or 0:g}%. Running agents were stopped; "
              f"{len(state['held'])} ticket(s) resume after the reset ({when}).", warn)
    elif state["paused"]:
        _ping(state, "usage_pause", "Launches paused",
              f"Weekly usage is {state['pct'] or 0:g}%. No new agents until the reset ({when}).", warn)
    if json.dumps(state, sort_keys=True) != before:
        try:
            save_state(state)
        except OSError as exc:
            warn(f"usage governor: cannot save its state ({exc})")
    return state


def status(now: datetime | None = None) -> dict:
    """For fleet status: what the governor is doing."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    if state["stopped"]:
        mode = "stopped"
    elif state["paused"]:
        mode = "paused"
    else:
        mode = "ok"
    out = {"state": mode, "weekly_pct": state["pct"], "since": state["since"],
           "resumes_at": state["reset_at"] if mode != "ok" else None, "held": list(state["held"])}
    if mode != "ok":
        out["message"] = (("agents stopped and " if mode == "stopped" else "") + "launches paused at weekly "
                          f"{state['pct'] or 0:g}%; resumes automatically after the reset "
                          f"({state['reset_at'] or 'time unknown'})")
    return out
