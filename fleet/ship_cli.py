"""The `ship` command: merge a ticket's work (in review, or still claimed) with its repo's own
scripts/finish-task.sh, which keeps every check and prompt it has.

`ship --quiesce ID` is the step finish-task.sh runs after the push and before it removes the
worktree: the ticket's agent is stopped and its herdr tab closed (only that ticket's own), and
changes the agent left in the worktree are saved to a keep/<id>-unmerged branch."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import dispatch as dp
from . import launch as lc
from . import prep
from . import risk as rk
from . import supervisor_run as sr
from . import ticket as tk


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"ship: {message}", file=sys.stderr)
        raise SystemExit(2)


_last_message = ""   # the line the run ended with, for the phone ping of a web ship


def _fail(message: str, code: int = 1) -> int:
    global _last_message
    _last_message = message
    print(f"ship: {message}", file=sys.stderr)
    return code


def _say(message: str) -> None:
    global _last_message
    _last_message = message
    print(f"ship: {message}")


def _reconcile() -> int:
    """Run `dispatch --reconcile` (the sibling bin/dispatch) so the card moves
    to Done at once."""
    dispatch = Path(__file__).resolve().parent.parent / "bin" / "dispatch"
    return subprocess.run([sys.executable, str(dispatch), "--reconcile"]).returncode


_ship_ticket = ""   # the ticket this run ships: the gate checks it is on the branch and asks for its id


def _gate(repo: Path, wt: Path, branch: str, *fields: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """`ticket gate` the way the merge script calls it: the exact same gate."""
    ticket = Path(__file__).resolve().parent.parent / "bin" / "ticket"
    base = dp.main_branch(repo) or "main"
    return subprocess.run(
        [sys.executable, str(ticket), "gate", "--base", base, "--branch", branch, "--dir", str(repo),
         "--worktree", str(wt), "--repo", repo.name,
         *(["--ticket", _ship_ticket] if _ship_ticket else []), *fields], capture_output=True, text=True,
        env=env)


def _confirm(question: str) -> bool:
    """Ask on the terminal; anything but an explicit yes (or no terminal) is no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


class _GateBroken(Exception):
    pass


def _field(repo: Path, wt: Path, branch: str, name: str, env: dict | None = None) -> str:
    """One gate field. The gate exits 1 when it has review problems, which is
    normal here; a crash (other exit code, or anything on stderr) is not."""
    done = _gate(repo, wt, branch, "--field", name, **({"env": env} if env is not None else {}))
    if done.returncode not in (0, 1) or done.stderr.strip():
        detail = done.stderr.strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise _GateBroken(f"ticket gate --field {name} failed ({detail[0]}); nothing was merged")
    return done.stdout.strip()


def _added_commits(repo: Path, old: str, tip: str) -> str:
    """Which commits the branch gained after `old` (the commit a verdict or confirmation was
    on), for a refusal or an offer: up to 8 one-line subjects, or that `old` is gone from it."""
    if dp._git(repo, "merge-base", "--is-ancestor", old, tip).returncode != 0:
        return f"it no longer contains {old[:8]} (the branch was rewritten)"
    out = dp._git(repo, "log", "--oneline", "--no-decorate", "-n", "9", f"{old}..{tip}")
    lines = out.stdout.strip().splitlines() if out.returncode == 0 else []
    if not lines:
        return "no commits were added"
    more = " (and more)" if len(lines) > 8 else ""
    return f"{len(lines[:8])} commit(s) added{more}: " + "; ".join(lines[:8])


def _moved_past_verdict(repo: Path, t: dict | None, branch: str) -> str | None:
    """When the ticket's recorded PASS is on a commit the branch has moved past: one line saying
    which commits were added. None when there is nothing to say."""
    v = (t or {}).get("verdict")
    tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").stdout.strip()
    if not v or v["result"] != "PASS" or not tip or v["commit"] == tip:
        return None
    return (f"{branch} has moved past the commit that passed review ({v['commit'][:8]}), "
            f"now at {tip[:8]}: {_added_commits(repo, v['commit'], tip)}")


def _review_if_needed(repo: Path, name: str, branch: str, script: Path, t: dict | None = None) -> str | None:
    """When the gate wants reviews and lacks them, run them now: both reviewers
    without asking at high risk, or (after a yes) the usual one when the tip
    has no review at all. The merge script still applies the gate afterwards.
    Returns a one-line problem, or None. Anything else the gate dislikes is
    left for the merge script to report: this never decides whether a merge
    is allowed."""
    wt = repo / ".worktrees" / name
    review = repo / "scripts" / "review.sh"
    if not wt.is_dir() or not review.is_file():
        return None
    try:
        ok, risk = _field(repo, wt, branch, "ok"), _field(repo, wt, branch, "risk")
        if ok == "yes":
            return None
        has_reviews = risk != "high" and _field(repo, wt, branch, "reviews")
    except _GateBroken as exc:
        return str(exc)
    # A branch that moved past its PASS says so and offers the reviews of the new tip, whatever the
    # risk level; no answer (or no terminal) refuses, as the web ship does.
    moved = _moved_past_verdict(repo, t, branch) if risk == "high" or not has_reviews else None
    if moved:
        print(f"ship: {moved}")
        if not _confirm(f"ship: run the reviews on {branch}'s new tip now?"):
            return f"{branch} moved past its reviewed commit and was not re-reviewed; nothing was merged"
    env = dict(os.environ)
    if risk == "high":
        print(f"ship: {branch} is high risk and lacks passing reviews; running both reviewers")
        env["REVIEWER"] = "both"
    elif moved:
        pass   # the question above was the confirmation
    else:
        # Below high risk, only the case where the tip has no review at all
        # (small fixes committed after the review) is offered; a review that
        # exists but is not a PASS is for the human to look at.
        if has_reviews:
            return None
        if not _confirm(f"ship: {branch} has no review of its latest commit. Run scripts/review.sh now?"):
            return None
    env.setdefault("FLEET_TICKET", str(Path(__file__).resolve().parent.parent / "bin" / "ticket"))
    try:
        done = subprocess.run(["bash", str(review)], cwd=wt, env=env)
    except OSError as exc:
        return f"could not run {review}: {exc}"
    if done.returncode != 0:
        return f"the reviews of {branch} did not complete (exit {done.returncode}); nothing was merged"
    return None


def _blocked_lines(repo: Path, wt: Path, branch: str, env: dict | None = None) -> str:
    """The gate's own BLOCKED lines, joined, for a one-line reason."""
    done = _gate(repo, wt, branch, **({"env": env} if env is not None else {}))
    lines = [line.strip().removeprefix("BLOCKED: ") for line in done.stdout.splitlines()
             if line.strip().startswith("BLOCKED:")]
    return "; ".join(lines) or "the risk level or the reviews do not allow it"


def _unattended_answer(repo: Path, wt: Path, branch: str, name: str, expect: str) -> str | int:
    """Everything a web ship checks BEFORE finish-task runs, because nobody is at a
    terminal to read what it says: the branch is still at the commit the page
    showed, and the gate is satisfied with no review being run on the way. Returns
    what finish-task's prompt is to be answered with (the typed ticket id at high
    risk, else y), or the exit code of a refusal. finish-task then applies every
    gate again itself, including the checks."""
    tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").stdout.strip()
    if tip != expect:
        return _fail(f"{branch} is now at {tip[:8]}, not the {expect[:8]} that was confirmed "
                     f"({_added_commits(repo, expect, tip)}); nothing was merged")
    try:
        if _field(repo, wt, branch, "ok") != "yes":
            return _fail(f"not allowed: {_blocked_lines(repo, wt, branch)}; nothing was merged")
        risk, word = _field(repo, wt, branch, "risk"), _field(repo, wt, branch, "confirm")
    except _GateBroken as exc:
        return _fail(str(exc))
    if not risk or not word:
        return _fail("could not work out the risk level; nothing was merged")
    return word if risk == "high" else "y"


def _ping_result(ticket_id: str, code: int) -> None:
    """The one ping of a web ship: shipped, or why not. Never raises."""
    from . import notify
    try:
        t = tk.load_ticket(ticket_id)
    except (tk.TicketError, tk.TicketInvalid):
        return
    if code == 0:
        notify.notify_quietly(t, "shipped", detail="merged" + (" and marked done" if t["status"] == "done"
                                                              else f", card still {t['status']}"))
    else:
        notify.notify_quietly(t, "ship_failed", detail=_last_message or f"exit {code}")


# --- merging with leftovers (EM-8 step G) ---------------------------------------------------------

REASON_MAX = 300


def _reason_problem(reason: str | None) -> str | None:
    if not reason or not reason.strip():
        return "--accept-leftovers needs --reason TEXT: why this merge may go ahead despite the Codex FAIL"
    if len(reason) > REASON_MAX or re.search(r"[\x00-\x1f\x7f]", reason):
        return f"--reason must be one line of 1 to {REASON_MAX} characters"
    leaks = tk.check_secrets("leftovers reason", reason)
    return leaks[0] if leaks else None


def _accept_facts(repo: Path, name: str, branch: str, t: dict):
    """Every fact the merge with leftovers rests on, read-only and before anything runs: a dict
    {tip, mark, level, codex_file, claude_file}, or a one-line reason it cannot go ahead."""
    wt = repo / ".worktrees" / name
    if not wt.is_dir():
        return f"no worktree at {wt}"
    tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tip):
        return f"cannot read the current commit of {branch}"
    state = sr.load_state()
    mark = sr.leftovers_for(state, t["id"], tip)
    if mark is None:
        old = state.get("leftovers", {}).get(t["id"])
        return (f"{t['id']} is not marked ready with leftovers at {tip[:8]}"
                + (f" (the mark is on {old['commit'][:8]}: the branch moved)" if old else " (no mark)"))
    try:
        level = _field(repo, wt, branch, "risk")
    except _GateBroken as exc:
        return str(exc)
    if level == "high":
        return f"{branch} is high risk: leftovers can never be accepted there"
    if level not in rk.LEVELS:
        return "could not work out the risk level"
    v = t.get("verdict") or {}
    got = {k: r.get("result") for k, r in (v.get("reviews") or {}).items()}
    if v.get("commit") != tip or got.get("claude") != "PASS" or got.get("codex") != "FAIL":
        return (f"the recorded verdict of {t['id']} is not Claude PASS with Codex FAIL on {tip[:8]} "
                f"(it says {v.get('result')} on {str(v.get('commit'))[:8]}: {got})")
    saved = rk.latest_reviews(wt / ".reviews", tip)
    claude, codex = saved.get("claude"), saved.get("codex")
    if not claude or claude["verdict"] != "PASS":
        return f"no saved Claude PASS review of {tip[:8]} in {wt}/.reviews"
    if not codex or codex["verdict"] != "FAIL":
        return f"no saved Codex FAIL review of {tip[:8]} in {wt}/.reviews (nothing to accept)"
    env = dict(os.environ)
    env[rk.ACCEPT_ENV] = tip
    try:
        ok = _field(repo, wt, branch, "ok", env=env)
    except _GateBroken as exc:
        return str(exc)
    if ok != "yes":
        return f"the merge gate does not accept {tip[:8]} even with leftovers: {_blocked_lines(repo, wt, branch, env)}"
    return {"tip": tip, "mark": mark, "level": level, "codex_file": Path(codex["file"]).name,
            "claude_file": Path(claude["file"]).name}


def _confirm_accept(t: dict, branch: str, facts: dict, reason: str) -> str | None:
    """Show what is being accepted and have the human type the first 7 characters of the commit.
    Needs the terminal: the typed commit is the barrier that does not depend on the environment."""
    m = facts["mark"]
    print(f"ACCEPT LEFTOVERS: {t['id']} {branch} at {facts['tip']}")
    print(f"  Codex FAIL ({facts['codex_file']}) is accepted; Claude PASS ({facts['claude_file']}); "
          f"risk {facts['level']}")
    print(f"  supervisor's reason: {m['reason']}")
    print(f"  remaining items:     {m['ticket'] or 'none filed'}")
    print(f"  your reason:         {reason}")
    try:
        typed = input(f"Type the first 7 characters of the commit ({facts['tip'][:7]}) to merge it anyway: ")
    except (EOFError, KeyboardInterrupt):
        print()
        typed = ""
    if typed.strip().lower() != facts["tip"][:7]:
        return "cancelled; nothing changed"
    return None


def _record_acceptance(repo: Path, t: dict, facts: dict, reason: str) -> str | None:
    """After the merge: check what merged is the commit that was checked, then leave the permanent
    record (ticket note and dispatcher log) and consume the mark. Returns a loud problem, or None."""
    tip, mark = facts["tip"], facts["mark"]
    base = dp.main_branch(repo) or "main"
    on_main = dp._git(repo, "merge-base", "--is-ancestor", tip, base).returncode == 0
    who = getpass.getuser()
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not on_main:
        text = (f"LEFTOVERS ACCEPTED by {who} (human) on {at}: {t['id']} was merged, but the checked commit "
                f"{tip} is NOT in {base}; check what actually merged. Human reason: {reason}")
    else:
        text = (f"LEFTOVERS ACCEPTED by {who} (human) on {at}: merged {tip} despite Codex FAIL "
                f"({facts['codex_file']}); human reason: {reason}; supervisor reason: {mark['reason']}; "
                f"remaining items: {mark['ticket'] or 'none filed'}.")
    text = text[:tk.NOTE_MAX]
    dp.log("ACCEPT_LEFTOVERS", f"{t['id']} {tip} by {who}: {text}")
    try:
        tk.add_note(t["id"], text)
    except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
        print(f"ship: could not save the note ({exc}); save it by hand:\n{text}", file=sys.stderr)
    if not on_main:
        return f"{tip[:8]} is not in {base} after the merge; the mark is kept and a warning was recorded"
    try:
        state = sr.load_state()
        state["leftovers"].pop(t["id"], None)   # one use
        sr.save_state(state)
    except OSError as exc:
        return f"merged and recorded, but the mark could not be consumed ({exc}); remove it from supervisor.json"
    return None


def _conflict_agent(repo: Path, t: dict, settings: dict) -> str | None:
    """The merge script stops with the merge half done when the branch conflicts with main.
    If that is what happened, put main back as it was and start a short reconcile agent.
    Returns a one-line message, or None when there was no merge conflict."""
    probe = dp._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if probe.returncode != 0:
        return None
    # The script refuses a main checkout with changes, so aborting restores exactly what was there.
    if dp._git(repo, "merge", "--abort").returncode != 0:
        return (f"{t['branch']} conflicts with main, and the half-done merge in {repo} "
                "could not be aborted: run git merge --abort there")
    try:
        started = prep.start_reconcile(t, settings, lambda m: print(f"ship: {m}", file=sys.stderr))
    except (lc.LaunchError, OSError, subprocess.TimeoutExpired) as exc:
        return (f"{t['branch']} conflicts with main (the merge was aborted); "
                f"no reconcile agent could be started ({exc})")
    return (f"{t['branch']} conflicts with main (the merge was aborted); {started}. "
            "You will get a ping when it is ready: ship it again then")


def _ship_one(ticket_id: str, in_list: bool = False, expect: str | None = None, preset=None,
              accept: dict | None = None) -> int:
    """Ship one ticket; prints what happened and returns the exit code. The ticket is reserved
    for the whole run, so no prep or reconcile agent can start on its branch meanwhile."""
    try:
        t = tk.load_ticket(ticket_id)
    except tk.TicketInvalid as exc:
        return _fail(f"unknown ticket ID: {exc}")
    except tk.TicketError as exc:
        return _fail(str(exc))
    try:
        problem = prep.reserve_shipping(t["id"])
    except OSError as exc:   # fail closed: an agent may be working on it
        return _fail(f"cannot tell whether an agent is working on {t['id']} ({exc}); nothing was merged")
    if problem:
        return _fail(problem)
    try:
        with _repo_lock(t.get("repo")) as busy:
            if busy:
                return _fail(f"another ship is already merging into {t.get('repo')}; wait for it to finish")
            return _ship_reserved(t["id"], in_list, expect, preset, accept)
    finally:
        prep.release_shipping(t["id"])


@contextlib.contextmanager
def _repo_lock(repo_name):
    """One merge at a time per repo, held across the merge-in-progress check, the merge script and
    the conflict cleanup: otherwise one run could abort the half-done merge of another. Yields
    True (without the lock) when another run holds it."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(repo_name))
    path = tk.fleet_home() / f"ship-{safe}.lock"
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield True
            return
        try:
            yield False
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _resolve(t: dict):
    """Where a ticket's work lives: (repo, branch, name, script, settings), or the exit code
    of a refusal that was already printed."""
    repo_name, branch = t.get("repo"), t.get("branch")
    if not repo_name or not branch:
        return _fail(f"{t['id']} has no repo and branch to ship")
    if not branch.startswith("task/") or len(branch) == len("task/"):
        return _fail(f"{t['id']} branch {branch} is not a task/<name> branch")
    name = branch.removeprefix("task/")
    global _ship_ticket
    _ship_ticket = t["id"]
    try:
        settings = dp.load_settings()
    except dp.SettingsError as exc:
        return _fail(str(exc), 2)
    projects_dir = settings["projects_dir"]
    repo = projects_dir / repo_name
    if repo.resolve().parent != projects_dir.resolve() or not repo.is_dir():
        return _fail(f"repo folder {repo} not found directly inside {projects_dir}")
    script = repo / "scripts" / "finish-task.sh"
    if not script.is_file():
        return _fail(f"{repo} has no scripts/finish-task.sh")
    return repo, branch, name, script, settings


def _ship_reserved(ticket_id: str, in_list: bool, expect: str | None = None, preset=None,
                   accept: dict | None = None) -> int:
    try:
        t = tk.load_ticket(ticket_id)
    except tk.TicketInvalid as exc:
        return _fail(f"unknown ticket ID: {exc}")
    except tk.TicketError as exc:
        return _fail(str(exc))
    if t["status"] not in ("claimed", "review"):
        return _fail(f"{t['id']} is {t['status']}; only a ticket in review (or still claimed) can be shipped")
    if in_list and t["status"] != "review":
        return _fail(f"{t['id']} is {t['status']}, not in review any more, so it is no longer ready")
    resolved = _resolve(t)
    if isinstance(resolved, int):
        return resolved
    repo, branch, name, script, settings = resolved

    answer = None
    if preset:
        # The detached job: the questions were asked on the terminal, and the reviews done there.
        tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").stdout.strip()
        if tip != preset[1]:
            return _fail(f"{branch} is now at {tip[:8]}, not the {preset[1][:8]} that was confirmed "
                         f"({_added_commits(repo, preset[1], tip)}); nothing was merged. "
                         "Run the command again to review the new tip")
        answer = preset[0]
    elif expect:
        # No terminal: only a ticket already in review, at the confirmed commit.
        if t["status"] != "review":
            return _fail(f"{t['id']} is {t['status']}; only a ticket in review can be shipped from the web")
        wt = repo / ".worktrees" / name
        if not wt.is_dir():
            return _fail(f"no worktree at {wt}; nothing was merged")
        answer = _unattended_answer(repo, wt, branch, name, expect)
        if isinstance(answer, int):
            return answer
    elif accept:
        # Merging with leftovers: reviews must already exist (none is ever run on this path, it would
        # change what is judged), and the human types the commit.
        facts = _accept_facts(repo, name, branch, t)
        if isinstance(facts, str):
            return _fail(f"{facts}; nothing was merged, nothing was recorded")
        problem = _confirm_accept(t, branch, facts, accept["reason"])
        if problem:
            return _fail(problem)
        now = dp._git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()
        if now != facts["tip"]:   # while the human was typing: what was checked is not what would merge
            return _fail(f"{branch} moved to {now[:8]} while you confirmed {facts['tip'][:8]}; "
                         "nothing was merged, nothing was recorded")
    else:
        problem = _review_if_needed(repo, name, branch, script, t)  # no preset: the unattended and list paths
        if problem:
            return _fail(problem)

    # A merge already under way is the human's: never touch it. Any MERGE_HEAD seen after the
    # merge script fails is then this run's own conflict.
    if dp._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        return _fail(f"a merge is already in progress in {repo}; finish or abort it first")

    # Same terminal, same prompts: nothing is answered or skipped on the
    # human's behalf. FLEET_HOME and the rest of the environment carry over.
    # (A web ship has no terminal: finish-task's one prompt is answered with
    # what the confirmation page already made the person do.)
    env = dict(os.environ)
    env.pop(rk.ACCEPT_ENV, None)
    if accept:
        env[rk.ACCEPT_ENV] = facts["tip"]   # for this one run only; the gate re-checks all of it
    try:
        done = subprocess.run(["bash", str(script), name, "--ticket", t["id"]], cwd=repo, env=env,
                              input=answer + "\n" if answer else None, text=True)
    except OSError as exc:
        return _fail(f"could not run {script}: {exc}")
    if done.returncode != 0:
        if accept:
            if dp._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                dp._git(repo, "merge", "--abort")
            return _fail(f"finish-task refused {branch} (exit {done.returncode}); nothing was recorded, "
                         "the mark is kept")
        if t["status"] == "review":
            prep.release_shipping(t["id"])   # the reconcile agent must be able to reserve it
            conflict = _conflict_agent(repo, t, settings)
            if conflict:
                return _fail(conflict)
        return _fail(f"finish-task refused {branch} (exit {done.returncode}); nothing was reconciled")

    # finish-task also exits 0 when the human answers no at its prompt, so ask
    # git, not the exit code, whether the merge happened.
    if not dp.is_merged(repo, branch, t.get("base_commit")):
        return _fail(f"{t['id']} was not merged (cancelled?); it is still {t['status']}")
    if accept:
        problem = _record_acceptance(repo, t, facts, accept["reason"])
        if problem:
            _fail(problem)
    later = "; run `dispatch --reconcile` to move the card to Done"
    try:
        _reconcile()
        status = tk.load_ticket(t["id"])["status"]
    except (OSError, tk.TicketError) as exc:
        _say(f"{t['id']} merged, but the card was not updated ({exc}){later}")
        return 0
    if status == "done":
        _say(f"{t['id']} merged and marked done")
    else:
        # e.g. a running `dispatch --watch` holds the lock; it reconciles on its next pass
        _say(f"{t['id']} merged, but is still {status}{later}")
    return 0


def _keep_unmerged(wt: Path, ticket_id: str) -> str | None:
    """Commit what is left in the worktree `wt` (changes, new files) to a new branch
    keep/<id>-unmerged and leave the worktree clean on it. Returns the branch name, or None when
    there was nothing to save. Raises OSError / RuntimeError when git refuses."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "--no-pager", "-C", str(wt), *args], capture_output=True, text=True, timeout=120)

    status = git("status", "--porcelain")
    if status.returncode != 0:
        raise RuntimeError(f"git status failed in {wt}: {status.stderr.strip()}")
    if not status.stdout.strip():
        return None
    base = f"keep/{ticket_id.lower()}-unmerged"
    name, n = base, 1
    while git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
        n += 1
        name = f"{base}-{n}"
    # A repo with no git identity must not stop the save: fall back to a fixed one for this commit.
    who = ["-c", "user.name=fleet", "-c", "user.email=fleet@localhost"] \
        if not git("config", "user.email").stdout.strip() else []
    orig = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()

    def undo() -> None:   # leave the worktree exactly as it was found
        git("reset", "-q")
        if orig:
            git("checkout", "-q", orig)
        git("branch", "-D", name)

    def refuse(why: str):
        undo()
        raise RuntimeError(why)

    done = git("checkout", "-b", name)
    if done.returncode != 0:
        raise RuntimeError(f"git failed in {wt}: {(done.stderr or done.stdout).strip()}")
    done = git("add", "-A")
    if done.returncode != 0:
        refuse(f"git add failed in {wt}: {(done.stderr or done.stdout).strip()}")
    # Rule 6: nothing that looks like a secret goes into git, and these edits were never checked.
    leaks = os.environ.get("FLEET_GITLEAKS") or "gitleaks"
    try:
        scan = subprocess.run([leaks, "git", "--staged", "--no-banner", "--redact", "--log-level", "error", str(wt)],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        refuse(f"the changes could not be scanned for secrets ({exc})")
    if scan.returncode != 0:
        refuse("the changes look like they hold a secret (or the scan failed); nothing was committed")
    # Commit hooks of the repo run: no --no-verify.
    done = git(*who, "commit", "-q", "-m", f"{ticket_id}: changes left in the worktree after the merge (not merged)")
    if done.returncode != 0:
        refuse(f"git commit failed in {wt}: {(done.stderr or done.stdout).strip()}")
    return name


def _quiesce(ticket_id: str) -> int:
    """Stop the merged ticket's agent and close its tab, then save leftover edits. Idempotent.
    Only ever acts on a ticket whose branch is merged, and only on that ticket's own agent."""
    try:
        t = tk.load_ticket(ticket_id)
        settings = dp.load_settings()
    except (tk.TicketError, tk.TicketInvalid, dp.SettingsError) as exc:
        return _fail(f"cannot quiesce {ticket_id}: {exc}")
    repo = settings["projects_dir"] / str(t.get("repo"))
    branch = t.get("branch") or ""
    if not repo.is_dir() or repo.resolve().parent != settings["projects_dir"].resolve() or not branch.startswith("task/"):
        return _fail(f"cannot quiesce {t['id']}: no repo folder or task branch")
    if not dp.is_merged(repo, branch, t.get("base_commit")):
        return _fail(f"{t['id']} is not merged; its agent is left alone")
    ok, why = lc.quiesce_agent(t["id"], settings)
    lc._safe_log("AGENT_QUIESCED" if ok else "AGENT_QUIESCE_FAILED", f"{t['id']} {why}")
    if not ok:
        return _fail(f"{t['id']} merged, but its agent could not be stopped ({why}); "
                     "its worktree is left as it is")
    _say(f"{t['id']} agent stopped ({why})")
    wt = repo / ".worktrees" / branch.removeprefix("task/")
    if not wt.is_dir():
        return 0
    try:
        kept = _keep_unmerged(wt, t["id"])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return _fail(f"{t['id']} has changes in {wt} that could not be saved ({exc}); the worktree is kept")
    if kept:
        lc._safe_log("UNMERGED_KEPT", f"{t['id']} changes after the merge saved on {kept}")
        _say(f"{t['id']} had changes after the merge: they are NOT in main, saved on branch {kept} "
             f"(the worktree can now be removed)")
    return 0


def _ship_list() -> int:
    """Show the ship list and ship each ticket in order, with the merge script's own
    confirmation for each. Stops at the first failure and says why."""
    try:
        entries = prep.read_list()
    except OSError as exc:
        return _fail(f"cannot read the ship list: {exc}")
    if not entries:
        return _fail(f"there is no ship list ({prep.list_path()}); the prep pass writes it")
    print("Ship list:")
    for _, line in entries:
        print(f"  {line}")
    listed = {i for i, _ in entries}
    reported = _report_unlisted(listed, set())   # before anything ships, so a stop below cannot hide it
    shipped = 0
    for n, (ticket_id, _) in enumerate(entries, 1):
        try:
            status = tk.load_ticket(ticket_id)["status"]
        except (tk.TicketError, tk.TicketInvalid) as exc:
            return _fail(f"stopped at {ticket_id}: {exc}")
        if status == "done":
            print(f"ship: {ticket_id} is already done; skipping")
            continue
        print(f"\nship: {ticket_id} ({n} of {len(entries)})")
        if _ship_one(ticket_id, in_list=True) != 0:
            return _fail(f"stopped at {ticket_id}; the rest of the list was not shipped")
        shipped += 1
    print(f"ship: the list is done ({shipped} shipped)")
    _report_unlisted(listed, reported)
    return 0


def _report_unlisted(listed: set[str], already: set[str]) -> set[str]:
    """Say plainly which tickets are in review but were not on the list (they reached review after
    it was written, e.g. bounced by the prep pass and back). They were not shipped: the list is
    only what the prep pass reconciled, and a late ticket has not been reconciled against it."""
    try:
        late = [t for t in prep.review_tickets(tk.list_tickets()) if t["id"] not in listed]
        locked = prep.locked_ids() if late else set()
    except (OSError, tk.TicketError, tk.TicketInvalid) as exc:
        print(f"ship: could not check for tickets missing from the list ({exc})", file=sys.stderr)
        return already
    for t in late:
        if t["id"] in already:
            continue
        why = ("an agent is working on it now; wait for its ping" if t["id"] in locked else
               "it reached review after the list was written, so it was not reconciled with the listed "
               f"tickets; run `ship {t['id']}` (a conflict starts a reconcile agent) or wait for the next prep pass")
        print(f"ship: {t['id']} is in review but NOT on the ship list, so it was not shipped: {why}",
              file=sys.stderr)
    return already | {t["id"] for t in late}


# --- the detached ship: questions on the terminal, the rest in a background job ------------

def _job_files(ticket_id: str) -> dict:
    d = tk.fleet_home() / "ship"
    tk.refuse_real_store_in_tests(d)
    return {"dir": d, "log": d / f"{ticket_id}.log", "result": d / f"{ticket_id}.result",
            "lock": d / f"{ticket_id}.lock"}


def _ask_to_ship(repo: Path, wt: Path, branch: str, tip: str) -> str | int:
    """Every question of the merge, asked before anything long runs. Returns the answer
    finish-task's one prompt is to get (the ticket id at high risk, else y), or an exit code."""
    try:
        if _field(repo, wt, branch, "ok") != "yes":
            return _fail(f"not allowed: {_blocked_lines(repo, wt, branch)}; nothing was merged")
        risk, word = _field(repo, wt, branch, "risk"), _field(repo, wt, branch, "confirm")
    except _GateBroken as exc:
        return _fail(str(exc))
    if not risk or not word:
        return _fail("could not work out the risk level; nothing was merged")
    base = dp.main_branch(repo) or "main"
    others = _gate(repo, wt, branch, "--field", "others").stdout.strip()
    if others:
        print(f"WARNING: shipping {branch} also ships the work of these unfinished tickets on it:")
        print("".join(f"  {line}\n" for line in others.splitlines()), end="")
    print(dp._git(repo, "log", "--oneline", f"{base}..{tip}").stdout, end="")
    print(dp._git(repo, "diff", "--stat", f"{base}...{tip}").stdout, end="")
    try:
        if risk == "high":
            typed = input(f"HIGH RISK change. Type the ticket id ({word}) to ship {branch} into {base}: ")
            yes = typed.strip().lower() == word.lower()
        else:
            yes = input(f"Ship {branch} into {base}? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        yes = False
    if not yes:
        return _fail("cancelled; nothing changed")
    return word if risk == "high" else "y"


def _prepare_detached(t: dict) -> tuple[str, str] | int:
    """Under the ticket's reservation: validate, review if needed, ask. Returns (answer, tip)
    or an exit code. The reservation ends here; the job takes its own."""
    try:
        problem = prep.reserve_shipping(t["id"])
    except OSError as exc:
        return _fail(f"cannot tell whether an agent is working on {t['id']} ({exc}); nothing was merged")
    if problem:
        return _fail(problem)
    try:
        if t["status"] not in ("claimed", "review"):
            return _fail(f"{t['id']} is {t['status']}; only a ticket in review (or still claimed) can be shipped")
        resolved = _resolve(t)
        if isinstance(resolved, int):
            return resolved
        repo, branch, name, script, _ = resolved
        problem = _review_if_needed(repo, name, branch, script, t)
        if problem:
            return _fail(problem)
        if dp._git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            return _fail(f"a merge is already in progress in {repo}; finish or abort it first")
        tip = dp._git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").stdout.strip()
        wt = repo / ".worktrees" / name
        if not tip or not wt.is_dir():
            return _fail(f"no worktree at {wt}; nothing was merged")
        answer = _ask_to_ship(repo, wt, branch, tip)
        return answer if isinstance(answer, int) else (answer, tip)
    finally:
        prep.release_shipping(t["id"])


def _follow(proc: subprocess.Popen, log: Path, ticket_id: str) -> int:
    """Show the job's log as it grows until the job ends. Ctrl-C only stops watching."""
    pos = 0
    try:
        while True:
            running = proc.poll() is None
            with open(log, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
            if chunk:
                pos += len(chunk)
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
            if not running:
                return proc.returncode
            time.sleep(0.2)
    except KeyboardInterrupt:
        print(f"\nship: still shipping {ticket_id} in the background; `ship --status {ticket_id}` shows how far it got")
        return 0


def _ship_detached(ticket_id: str) -> int:
    try:
        t = tk.load_ticket(ticket_id)
    except tk.TicketInvalid as exc:
        return _fail(f"unknown ticket ID: {exc}")
    except tk.TicketError as exc:
        return _fail(str(exc))
    files = _job_files(t["id"])
    files["dir"].mkdir(parents=True, exist_ok=True)
    lock = open(files["lock"], "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return _fail(f"{t['id']} is already being shipped; `ship --status {t['id']}` shows how far it got")
    try:
        prepared = _prepare_detached(t)
        if isinstance(prepared, int):
            return prepared
        answer, tip = prepared
        files["result"].unlink(missing_ok=True)
        ship = Path(__file__).resolve().parent.parent / "bin" / "ship"
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(files["log"], "w") as log:
            log.write(f"ship: {t['id']} started {started}\n")
            log.flush()
            # A new session, its output in the log, and the lock handed over: closing the
            # terminal or sleeping the laptop cannot stop it, and a second ship is refused
            # until it ends (flock lives as long as any copy of the open file).
            proc = subprocess.Popen(
                [sys.executable, str(ship), "--job", t["id"], "--tip", tip, "--answer", answer],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                pass_fds=[lock.fileno()], env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except OSError as exc:
        return _fail(f"could not start the ship job: {exc}")
    finally:
        lock.close()   # the job's copy keeps the lock
    print(f"ship: {t['id']} is shipping in the background (log {files['log']})")
    return _follow(proc, files["log"], t["id"])


def _run_job(ticket_id: str, tip: str, answer: str) -> int:
    """The detached job itself: checks, merge and push through finish-task with the answers
    already given, then the one phone ping and the result file `ship --status` reads."""
    code = _ship_one(ticket_id, preset=(answer, tip))
    files = _job_files(ticket_id)
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        files["result"].write_text(json.dumps({"code": code, "message": _last_message, "finished": finished}))
    except OSError as exc:
        print(f"ship: could not write the result file: {exc}", file=sys.stderr)
    _ping_result(ticket_id, code)
    return code


def _status(ticket_id: str) -> int:
    if not tk.ID_RE.fullmatch(ticket_id):
        return _fail(f"unknown ticket ID: {ticket_id}")
    files = _job_files(ticket_id)
    tail = ""
    if files["log"].is_file():
        tail = "".join(files["log"].read_text(errors="replace").splitlines(True)[-8:])
    running = False
    if files["lock"].is_file():
        with open(files["lock"], "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                running = True
    if running:
        print(f"ship: {ticket_id} is still shipping; the log so far:")
        print(tail, end="")
        return 0
    if not files["log"].is_file():
        return _fail(f"no ship of {ticket_id} has been run")
    try:
        result = json.loads(files["result"].read_text())
    except (OSError, ValueError):
        print(tail, end="")
        return _fail(f"{ticket_id} stopped without a result (the job was killed?); see {files['log']}")
    if result.get("code") == 0:
        print(f"ship: {ticket_id} shipped ({result.get('finished')}): {result.get('message')}")
        return 0
    print(tail, end="")
    return _fail(f"{ticket_id} was not shipped ({result.get('finished')}): {result.get('message')}")


def main(argv=None) -> int:
    p = _Parser(prog="ship")
    p.add_argument("id", nargs="?", type=tk.normalize_id, help="ticket id, like BD-17 (any case)")
    p.add_argument("--status", metavar="ID", type=tk.normalize_id,
                   help="show how far a ship of ID got, or how it ended")
    p.add_argument("--job", metavar="ID", type=tk.normalize_id, help=argparse.SUPPRESS)
    p.add_argument("--quiesce", metavar="ID", type=tk.normalize_id,
                   help="after a merge: stop ID's agent, close its tab, save left-over edits "
                        "(finish-task runs this itself)")
    p.add_argument("--tip", help=argparse.SUPPRESS)
    p.add_argument("--answer", help=argparse.SUPPRESS)
    p.add_argument("--list", action="store_true", help="ship every ticket on the ship list, in order")
    p.add_argument("--accept-leftovers", action="store_true",
                   help="human only, from a terminal: merge ID despite the Codex FAIL, only when the "
                        "supervisor marked that exact commit ready with leftovers and Claude passed it; "
                        "never at high risk; needs --reason")
    p.add_argument("--reason", help="with --accept-leftovers: why, in one line (recorded on the ticket)")
    p.add_argument("--non-interactive", metavar="COMMIT", dest="expect",
                   help="used by the web endpoint: ship without a terminal, only if the branch is "
                        "still at COMMIT and the gate passes with the reviews already on file; "
                        "sends one phone ping with the result")
    args = p.parse_args(argv)
    # Nothing ship runs (the merge script included) may stop at a pager: a detached ship sat at
    # `less (END)` overnight holding its reservation (EM-27).
    os.environ["GIT_PAGER"] = "cat"
    os.environ.pop(rk.ACCEPT_ENV, None)   # only this command's own flag may turn the tolerance on
    if args.accept_leftovers or args.reason is not None:
        if not args.accept_leftovers:
            p.error("--reason goes with --accept-leftovers")
        if (not args.id or args.list or args.expect or args.job or args.status or args.quiesce
                or args.tip or args.answer):
            p.error("--accept-leftovers takes one ticket id and --reason, nothing else")
        who = tk.current_creator()
        if who != "human":
            return _fail(f"refusing to run inside an agent (running as {who}); only a human accepts leftovers")
        if not sys.stdin.isatty():
            return _fail("--accept-leftovers needs a terminal: you type the commit to confirm it")
        problem = _reason_problem(args.reason)
        if problem:
            return _fail(problem)
        return _ship_one(args.id, accept={"reason": args.reason.strip()})
    if args.status:
        if args.id or args.list or args.job:
            p.error("--status takes its ticket id and nothing else")
        return _status(args.status)
    if args.quiesce:
        if args.id or args.list or args.job or args.expect or args.status:
            p.error("--quiesce takes its ticket id and nothing else")
        who = tk.current_creator()
        if who != "human":
            return _fail(f"refusing to run inside an agent (running as {who}); only a human ships")
        return _quiesce(args.quiesce)
    if args.job:
        if not (args.tip and args.answer) or args.id or args.list or args.expect:
            p.error("--job is internal: it needs --tip and --answer and nothing else")
    elif args.list == bool(args.id):
        p.error("give a ticket id, or --list, not both and not neither")
    if args.list and args.expect:
        p.error("--non-interactive ships one ticket, not the list")
    who = tk.current_creator()
    if who != "human":
        return _fail(f"refusing to run inside an agent (running as {who}); only a human ships")
    if args.job:
        return _run_job(args.job, args.tip, args.answer)
    if args.list:
        return _ship_list()
    if not args.expect and sys.stdin.isatty():
        return _ship_detached(args.id)   # no terminal: the old way, since nobody could be asked
    code = _ship_one(args.id, expect=args.expect)
    if args.expect:
        _ping_result(args.id, code)
    return code
