"""Send new human notes on a claimed ticket into its running agent's herdr tab.

An agent reads its ticket only at launch, so a note the human adds later never
reaches it. Each watch cycle looks at claimed tickets and, for a note by the
human that was not sent yet, prompts the agent once, when it is idle.

What was handled is kept in $FLEET_HOME/pushed-notes.json as {ticket id: number
of notes handled}. Notes are only ever appended, so a count is enough, and a
restart of the watcher cannot resend anything. A ticket seen for the first time
starts at its current note count: those notes were in the ticket the agent read
at launch (or predate this feature).
"""

from __future__ import annotations

import json

from . import dispatch as dp
from . import launch as lc
from . import ticket as tk

PREFIX = "New note from the human on your ticket:"


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


def push(tickets: list[dict], emit, warn) -> None:
    """One pass. Never raises for herdr or disk trouble: it warns and tries again
    next cycle."""
    claimed = [t for t in tickets if t["status"] == "claimed" and t.get("assignee") in tk.AGENTS]
    try:
        state = _load()
        new = {k: v for k, v in state.items() if k in {t["id"] for t in claimed}}
        agents = None
        for t in sorted(claimed, key=tk.oldest_first_key):
            notes = t.get("notes") or []
            done = new.get(t["id"])
            if done is None:
                new[t["id"]] = len(notes)
                continue
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
            agents[name] = "working"   # it has a prompt now; the next note waits
            dp.log("NOTE_PUSHED", f"{t['id']} note {done + 1} of {len(notes)} sent to {name}")
            emit(f"{t['id']}  sent a new human note to its agent")
            _save(new)   # save at once: a crash must not make the next cycle resend
            state = dict(new)
        if new != state:
            _save(new)
    except (tk.TicketError, OSError) as exc:
        warn(f"pushing notes: {exc}")
