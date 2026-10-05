"""Send new human notes on a claimed ticket into its running agent's herdr tab.

An agent reads its ticket only at launch, so a note the human adds later never
reaches it. Each watch cycle looks at claimed tickets and, for a note by the
human that was not sent yet, prompts the agent once, when it is idle.

What was handled is kept in $FLEET_HOME/pushed-notes.json as {ticket id: number
of notes handled}. Notes are only ever appended, so a count is enough, and a
restart of the watcher cannot resend anything. A launch records the notes the
agent was started with (`baseline`). A claimed ticket the watcher never launched
(or launched before this feature) starts at its current note count the first time
it is seen.
"""

from __future__ import annotations

import json

from . import dispatch as dp
from . import launch as lc
from . import ticket as tk

PREFIX = "New note from the human on your ticket:"
FINISHED = ("done", "failed", "cancelled")


def state_path():
    return tk.fleet_home() / "pushed-notes.json"


def _load() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def _save(state: dict) -> None:
    path = state_path()
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def baseline(ticket: dict) -> None:
    """A ticket was just launched with the notes it holds now in front of the agent:
    count them as handled, so a note added after the launch (even before the next
    cycle) still goes out. A ticket that is already counted keeps its count."""
    try:
        state = _load()
        if ticket["id"] not in state:
            state[ticket["id"]] = len(ticket.get("notes") or [])
            _save(state)
    except (tk.TicketError, OSError):
        pass   # the first cycle that sees the ticket then starts it at its note count


def push(tickets: list[dict], emit, warn) -> set[str]:
    """One pass. Never raises for herdr or disk trouble: it warns and tries again
    next cycle. The count is saved right after a send, before anything else can
    fail; only a crash in between could resend that one note. Returns the ids of the
    tickets whose agent was just prompted: it is not finished, whatever herdr says."""
    prompted: set[str] = set()
    claimed = sorted((t for t in tickets if t["status"] == "claimed" and t.get("assignee") in tk.AGENTS),
                     key=tk.oldest_first_key)
    try:
        state = _load()
        # Counts are kept until the ticket is finished: it may leave the claimed
        # set for a while (released, then claimed again) and must not lose its count.
        finished = {t["id"] for t in tickets if t["status"] in FINISHED}
        new = {k: v for k, v in state.items() if k not in finished}
        for t in claimed:   # first sight: what is there now was in the ticket at launch
            new.setdefault(t["id"], len(t.get("notes") or []))
        if new != state:
            _save(new)
        agents = None
        for t in claimed:
            notes = t.get("notes") or []
            done = new[t["id"]]
            # Skip notes by anyone else, then see whether the next one is the human's.
            while done < len(notes) and notes[done].get("by") != "human":
                done += 1
            new[t["id"]] = done
            if done >= len(notes):
                continue
            if agents is None:
                try:
                    agents = {a.get("name"): a.get("agent_status") for a in lc.list_agents()}
                except lc.LaunchError as exc:
                    warn(f"cannot push notes: {exc}")
                    break
            name = t["id"].lower()
            if agents.get(name) != "idle":
                continue   # not running, or mid-turn / waiting on a prompt: try next cycle
            try:
                lc.herdr("agent", "prompt", name, f"{PREFIX}\n{notes[done]['text']}")
            except lc.LaunchError as exc:
                warn(f"could not send a note to {t['id']}: {exc}")
                continue
            new[t["id"]] = done + 1
            prompted.add(t["id"])
            agents[name] = "working"   # it has a prompt now; the next note waits
            _save(new)
            emit(f"{t['id']}  sent a new human note to its agent")
            try:
                dp.log("NOTE_PUSHED", f"{t['id']} note {done + 1} of {len(notes)} sent to {name}")
            except OSError:
                pass
        _save(new)
    except (tk.TicketError, OSError) as exc:
        warn(f"pushing notes: {exc}")
    return prompted
