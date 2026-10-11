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

A note is held back, not dropped, while pushing it would be wrong (see `hold_reason`):
the ticket is not claimed any more, a merge run holds it, its branch is merged, or its
newest commit already has every PASS the merge gate needs ("settled": the agent is
done, and a note would only start another round that un-settles it). A held ordinary
note keeps its place and goes out if the ticket stops being held (a new commit).
SHOULD FIX leftovers that review.sh saved as notes are not held: they are filed in the
ticket's ONE leftovers ticket, the same one the supervisor and `review.sh
--propose-leftovers` use, and counted as handled.
"""

from __future__ import annotations

import json
import re

from . import dispatch as dp
from . import kaneo
from . import launch as lc
from . import prep
from . import risk as rk
from . import ticket as tk

PREFIX = "New note from the human on your ticket:"
FINISHED = ("done", "failed", "cancelled")
# the label written by ticket_cli._save_leftovers
LEFTOVERS_RE = re.compile(r"SHOULD FIX leftovers from the (\S+(?: \S+)*?) review of ([0-9a-f]{7,40}) \(PASS\):\n(.+)",
                          re.S)
_held_logged: set[tuple[str, int]] = set()   # (ticket, note number) already logged as held


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


def baseline(ticket: dict, overwrite: bool = False) -> None:
    """A ticket was just launched with the notes it holds now in front of the agent:
    count them as handled, so a note added after the launch (even before the next
    cycle) still goes out. A ticket that is already counted keeps its count, unless
    `overwrite` (a relaunch: the new agent read all the notes, whoever wrote them)."""
    try:
        state = _load()
        if overwrite or ticket["id"] not in state:
            state[ticket["id"]] = len(ticket.get("notes") or [])
            _save(state)
    except (tk.TicketError, OSError):
        pass   # the first cycle that sees the ticket then starts it at its note count


def _settled(t: dict, settings: dict) -> bool:
    """Does the newest commit of the ticket's branch have a PASS from every reviewer the merge
    gate needs? Uses the gate's own code (risk.latest_reviews / check_reviews, the same level
    and the same Claude-model rule). A review of an older commit never counts. Any trouble
    working it out (git, review files) means NOT settled: notes then flow as before."""
    try:
        repo = settings["projects_dir"] / t["repo"]
        branch = t["branch"]
        tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").stdout.strip()
        v = t.get("verdict")
        if len(tip) != 40 or not v or v.get("commit") != tip or v.get("result") != "PASS":
            return False   # cheap prefilter: the verdict on the ticket is for this tip and passed
        base = dp.main_branch(repo)
        rev_range = f"{base}...{branch}"
        rules = rk.rules_for_range(repo, rev_range)
        _, diff_level = rk.classify_all(rk.changed_files(repo, rev_range), rules)
        stored, _ = rk.level_for_branch(tk.list_tickets(), branch, rules, t["repo"])
        level = rk.higher(stored, diff_level)
        reviews = rk.latest_reviews(repo / ".worktrees" / branch.removeprefix("task/") / ".reviews", tip)
        if rk.check_reviews(reviews, level):
            return False
        claude = reviews.get("claude")
        return not (claude and not rk.STRONG_CLAUDE_RE.search(claude["reviewer"]))
    except Exception:   # deliberately broad: "could not tell" must mean "not settled", never a crash
        return False


def hold_reason(t: dict, settings: dict | None = None) -> str | None:
    """Why a note must NOT be pushed to ticket `t`'s agent now, or None. Looked at fresh
    (the ticket is read again) right before every send. Fails closed for the merge-run check:
    when the state of running merges cannot be read, the note is held. A trouble reading
    review files is not a hold: that is "not settled", and notes flow as they did before."""
    try:
        fresh = tk.load_ticket(t["id"])
    except (tk.TicketError, tk.TicketInvalid, OSError):
        return "the ticket could not be read"
    if fresh["status"] != "claimed":
        return f"the ticket is {fresh['status']}"
    try:
        if t["id"] in prep.shipping_ids():
            return "it is being merged by a ship run"
    except OSError:
        return "the state of running merges cannot be read"
    if settings is None:
        return None
    repo = settings["projects_dir"] / fresh["repo"]
    try:
        if dp.is_merged(repo, fresh["branch"], fresh.get("base_commit")):
            return "its branch is already merged"
    except OSError:
        pass
    if _settled(fresh, settings):
        return "its newest commit has every review PASS it needs"
    return None


def _file_leftovers(t: dict, text: str, warn) -> bool:
    """Put one SHOULD FIX leftovers note into the ticket's one leftovers ticket. True when it
    is there (or was before), False when this is not such a note or it could not be filed."""
    m = LEFTOVERS_RE.fullmatch(text)
    if not m:
        return False
    reviewer, sha, items = m.groups()
    try:
        new, what = tk.file_leftovers_items(t, f"From the {reviewer} review of {sha[:7]}:", items)
    except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
        warn(f"{t['id']}: could not file SHOULD FIX leftovers: {exc}")
        return False
    if what:
        kaneo.mirror_quietly(new, warn)
        try:
            dp.log("NOTE_LEFTOVERS", f"{t['id']} leftovers of {sha[:7]} {what} in {new['id']}")
        except OSError:
            pass
    return True


def push(tickets: list[dict], emit, warn, settings: dict | None = None) -> set[str]:
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
            held = hold_reason(t, settings)
            if held:
                # Leftovers go to the leftovers ticket and are counted; an ordinary note stays
                # (it is delivered if the ticket stops being held) and is logged once.
                while done < len(notes) and (notes[done].get("by") != "human"
                                             or _file_leftovers(t, notes[done].get("text") or "", warn)):
                    done += 1
                new[t["id"]] = done
                if done < len(notes) and (t["id"], done) not in _held_logged:
                    _held_logged.add((t["id"], done))
                    try:
                        dp.log("NOTE_HELD", f"{t['id']} note {done + 1} of {len(notes)} not sent: {held}")
                    except OSError:
                        pass
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
            if hold_reason(t, settings):
                continue   # it changed since the cycle began (a merge run, a merge): the next cycle decides
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
