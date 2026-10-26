"""The Cursor reviewer (BD-136): a third reviewer whose vote counts only on proof.

Run by the review watcher (a fleet process), never by an agent and never by review.sh.
Agents never launch Cursor and never see its API key, which lives in the fleet store at
secrets/cursor-api-key (agents cannot read the fleet store).

A review of an exact commit:
  * the commit is copied to a fresh temp folder without .claude, .codex, .cursor, CLAUDE.md and
    AGENTS.md, with an empty throwaway HOME;
  * a native .cursor/hooks.json (fleet/cursor_hook.py) blocks shell and writes, allows reads, and
    records the model the read hook saw (never the payload, never the account email);
  * `agent -p --trust --mode ask --model composer-2.5 --output-format json` runs with the key in
    the environment of that one command only;
  * the review is saved in .reviews/ ending -cursor.md. Output with no VERDICT line is no review.

It COUNTS (one vote for company "cursor") only when a passing human probe result exists for the
installed CLI version (scripts/cursor-probe.sh writes it) and the model the hook saw is a Cursor
(composer) model. Otherwise it is recorded in .reviews/ and a note, and not counted.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

from . import risk as rk
from . import ticket as tk

MODEL = "composer-2.5"
REVIEWER = f"Cursor ({MODEL})"
EXCLUDED_DIRS = (".claude", ".codex", ".cursor")
EXCLUDED_FILES = ("CLAUDE.md", "AGENTS.md")
PROBES = ("a", "b", "c", "d", "e")
RUN_TIMEOUT = 1800
DIFF_MAX = 400_000
HOOK = Path(__file__).with_name("cursor_hook.py")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def agent_command() -> str:
    return os.environ.get("FLEET_CURSOR_AGENT") or "agent"


def key_path() -> Path:
    return tk.fleet_home() / "secrets" / "cursor-api-key"


def probe_result_path() -> Path:
    return tk.fleet_home() / "cursor-probe-result.json"


def _clean_env(home: Path, extra: dict | None = None) -> dict:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
           "XDG_CACHE_HOME": str(home / ".cache"), "LANG": "C.UTF-8"}
    env.update(extra or {})
    return env


def cli_version(home: Path) -> str | None:
    """The first line of `agent --version`, the same text the probe stored."""
    try:
        out = subprocess.run([agent_command(), "--version"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL, env=_clean_env(home))
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (out.stdout or out.stderr or "").strip().splitlines()
    return line[0].strip() if out.returncode == 0 and line else None


def counts(version: str | None, model_seen: str | None) -> tuple[bool, str]:
    """Does a Cursor review made with this CLI version and this seen model count? (yes, reason)."""
    if not model_seen:
        return False, "the review hook did not record which model ran"
    if not model_seen.lower().startswith("composer"):
        return False, f"the model that ran ({model_seen}) is not a Cursor model"
    if not version:
        return False, "the installed Cursor CLI version could not be read"
    try:
        data = json.loads(probe_result_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "no passing Cursor probe result exists; the human runs scripts/cursor-probe.sh"
    except (OSError, ValueError):
        return False, "the Cursor probe result file is unreadable; the human runs scripts/cursor-probe.sh again"
    if not isinstance(data, dict) or data.get("cli_version") != version:
        return False, (f"the Cursor probe was run for a different CLI version; installed is {version}, "
                       "the human runs scripts/cursor-probe.sh again")
    probes = data.get("probes")
    failed = [p for p in PROBES if not isinstance(probes, dict) or probes.get(p) != "pass"]
    if failed or data.get("passed") is not True:
        return False, f"the Cursor probe did not pass for {version} (probe {', '.join(failed) or '?'})"
    return True, "a passing probe result exists for this Cursor CLI version"


def _scrub(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[key removed]")
    return EMAIL_RE.sub("[email removed]", text)


def _extract_commit(repo: Path, tip: str, dest: Path) -> None:
    arc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", tip],
                         capture_output=True, timeout=300)
    if arc.returncode != 0:
        raise OSError("git archive failed")
    with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tar:
        tar.extractall(dest, filter="data")
    for root, dirs, files in os.walk(dest):
        for d in [d for d in dirs if d in EXCLUDED_DIRS]:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
            dirs.remove(d)
        for f in files:
            if f in EXCLUDED_FILES:
                os.unlink(os.path.join(root, f))


def _base_branch(repo: Path) -> str:
    ok = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "main"],
                        capture_output=True).returncode == 0
    return "main" if ok else "master"


def _prompt(branch: str) -> str:
    return f"""You are a code reviewer. The commit under review is the folder you are in; the changes
of branch '{branch}' against the base are in .fleet-review/changes.diff. You are read-only: shell and
writes are blocked, do not try them. Read the diff and the files it touches.

Check for: bugs, missing error handling, security problems, secrets in the code, tests that were
weakened or skipped, and anything that breaks the rules in the repository's rules file if present.
Do not ask questions: you cannot get an answer. Always finish with the verdict.

MUST FIX is only: (1) SECURITY, (2) DATA LOSS, (3) WRONG BEHAVIOR in normal operation, (4) TESTS
failing or weakened. Everything else is SHOULD FIX.

Reply in this format:
VERDICT: PASS or FAIL
MUST FIX: numbered list (empty if none): file, line, problem, fix, reason.
SHOULD FIX: numbered list of smaller suggestions.
SUMMARY: two or three sentences."""


def _result_text(stdout: str) -> str | None:
    """The `result` text of the last result event, or None (failed run, no result event)."""
    found = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            found = ev
    if found is None or found.get("is_error") or not isinstance(found.get("result"), str):
        return None
    return found["result"]


def has_verdict(text: str) -> str | None:
    for line in text.splitlines():
        m = rk.VERDICT_RE.match(line)
        if m:
            return m.group(1).upper()
    return None


def review_path(wt: Path, branch: str, tip: str) -> Path:
    return wt / ".reviews" / f"{branch.replace('/', '-')}-{tip[:7]}-cursor.md"


def _save(path: Path, text: str) -> None:
    """Write the review under .reviews without following anything an agent put there: the folder
    must be a real folder, the temporary file is created exclusively with a random name (never a
    predictable name an agent could have made a symlink), and the final rename replaces the name
    itself, never the target of a link."""
    folder = path.parent
    if folder.is_symlink():
        raise OSError(f"{folder} is a symlink")
    folder.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_review(repo: Path, wt: Path, branch: str, tip: str) -> dict:
    """Review the exact commit `tip` and save the review under wt/.reviews. Returns
    {"status": "no-key"|"failed"|"no-verdict"|"saved", "counted": bool, "reason": str,
     "verdict": "PASS"|"FAIL"|None, "file": str|None, "version": str|None}. Nothing here logs or
    stores the key or hook payloads."""
    truncated = False
    out = {"status": "failed", "counted": False, "reason": "", "verdict": None, "file": None,
           "version": None}
    try:
        key = key_path().read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if not key:
        out.update(status="no-key", reason="no Cursor API key file; the human puts it in place")
        return out
    with tempfile.TemporaryDirectory(prefix="cursor-review-") as tmp_s:
        tmp = Path(tmp_s)
        copy, home, state = tmp / "copy", tmp / "home", tmp / "state"
        for d in (copy, home, state):
            d.mkdir()
        try:
            _extract_commit(repo, tip, copy)
            d = subprocess.run(["git", "-C", str(repo), "diff", f"{_base_branch(repo)}...{tip}"],
                               capture_output=True, text=True, timeout=300)
            if d.returncode != 0:
                raise OSError("git diff failed")
            diff = d.stdout
            if len(diff) > DIFF_MAX:   # the reviewer cannot see all of it: it may be recorded, never counted
                truncated = True
                diff = diff[:DIFF_MAX] + "\n[diff truncated: too large]\n"
            (copy / ".fleet-review").mkdir()
            (copy / ".fleet-review" / "changes.diff").write_text(diff, encoding="utf-8")
            (copy / ".cursor").mkdir()
            hooks = subprocess.run([sys.executable, "-I", str(HOOK), "json", str(state), str(copy)],
                                   capture_output=True, text=True, check=True).stdout
            (copy / ".cursor" / "hooks.json").write_text(hooks, encoding="utf-8")
            out["version"] = cli_version(home)
            run = subprocess.run(
                [agent_command(), "-p", "--trust", "--mode", "ask", "--model", MODEL,
                 "--output-format", "json", _prompt(branch)],
                cwd=copy, env=_clean_env(home, {"CURSOR_API_KEY": key}), capture_output=True,
                text=True, timeout=RUN_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            out["reason"] = f"the Cursor review could not run ({type(exc).__name__})"
            return out
        if run.returncode != 0:
            out["reason"] = f"the Cursor CLI exited {run.returncode}"
            return out
        body = _result_text(run.stdout)
        verdict = has_verdict(body) if body else None
        if verdict is None:
            out.update(status="no-verdict", reason="the Cursor output had no VERDICT line: no review")
            return out
        try:
            seen = (state / "model.txt").read_text(encoding="utf-8").strip() or None
        except OSError:
            seen = None
    counted, why = counts(out["version"], seen)
    if truncated:
        counted, why = False, "the diff was too large to show the reviewer in full"
    text = _scrub(body, key).strip()
    header = (f"Reviewer: {REVIEWER}\nCommit: {tip}\nModel: {seen or 'none'}\n"
              f"Counted: {'yes' if counted else 'no'}\nReason: {why}\n\n")
    path = review_path(wt, branch, tip)
    try:
        _save(path, header + text + "\n")   # only now can the merge gate see it
    except OSError as exc:
        out["reason"] = f"the Cursor review could not be saved ({exc})"
        return out
    out.update(status="saved", counted=counted, reason=why, verdict=verdict, file=str(path))
    return out


# --- the watcher side ----------------------------------------------------------------

_JOBS: dict[tuple[str, str], dict] = {}
_LOCK = threading.Lock()
_SETTLED: set[tuple[str, str]] = set()   # saved reviews already taken up by this process


def _job(repo: Path, wt: Path, branch: str, tip: str, entry: dict) -> None:
    try:
        entry["result"] = run_review(repo, wt, branch, tip)
    except Exception as exc:   # a thread always leaves a result behind
        entry["result"] = {"status": "failed", "counted": False, "verdict": None, "file": None,
                           "reason": f"the Cursor review crashed ({type(exc).__name__})"}


def wait() -> None:
    with _LOCK:
        threads = [e["thread"] for e in _JOBS.values()]
    for th in threads:
        th.join()


def reset() -> None:
    wait()
    with _LOCK:
        _JOBS.clear()
        _SETTLED.clear()


def _outcome(t: dict, tip: str, counted: bool, verdict: str, reason: str, emit, warn, ping, note) -> None:
    tid = t["id"]
    if counted:
        v = t.get("verdict")
        if v is not None and v["commit"] == tip and v["reviews"].get("cursor", {}).get("result") == verdict:
            return   # already on the ticket
        try:
            tk.record_verdict(t["branch"], t["repo"], verdict, tip, REVIEWER)
        except (tk.TicketError, tk.TicketInvalid, OSError) as exc:
            warn(f"{tid}: could not record the Cursor vote ({exc})")
            return
        emit(f"{tid}  Cursor review of {tip[:8]}: {verdict} (counted)")
        return
    emit(f"{tid}  Cursor review of {tip[:8]}: {verdict} (recorded, not counted)")
    detail = f"Cursor's {verdict} on {tip[:7]} was recorded but not counted: {reason}"
    note(tid, f"Cursor review of {tip[:8]} not counted", detail)
    ping(t, "needs_human", f"{tid}:cursor_uncounted:{tip}", detail)


def _settle_saved(t: dict, saved: dict, tip: str, emit, warn, ping, note) -> None:
    """A Cursor review file exists for this tip but this process did not make it (it restarted):
    make the ticket agree with it. Every step is idempotent (a recorded vote, a note marker and
    a ping key are each checked), so a repeat changes nothing."""
    if (t["id"], tip) in _SETTLED:
        return
    if saved["verdict"] in ("PASS", "FAIL"):
        _SETTLED.add((t["id"], tip))
        _outcome(t, tip, bool(saved["counted"]), saved["verdict"],
                 "see the Reason line of the saved review", emit, warn, ping, note)


def consider(t: dict, repo: Path, tip: str, settings: dict, emit, warn, ping, note) -> None:
    """Called every watch cycle for a claimed or in-review ticket. When the setting is on, the
    tip already has a recorded PASS and no Cursor review exists, start one in a background thread;
    on a later cycle take up its result: record a counted vote on the ticket, or (not counted) a
    note and one plain ping naming the reason."""
    if not settings.get("cursor_review"):
        return
    tid, branch = t["id"], t["branch"]
    wt = repo / ".worktrees" / branch.removeprefix("task/")
    key = (tid, tip)
    with _LOCK:
        entry = _JOBS.get(key)
    if entry is None:
        v = t.get("verdict")
        if v is None or v["commit"] != tip or v["result"] != "PASS" or not wt.is_dir():
            return
        saved = rk.latest_reviews(wt / ".reviews", tip).get("cursor")
        if saved is not None:   # a restart: take up a saved review whose outcome never reached the ticket
            _settle_saved(t, saved, tip, emit, warn, ping, note)
            return
        if not key_path().is_file():
            return
        entry = {"result": None}
        entry["thread"] = threading.Thread(target=_job, args=(repo, wt, branch, tip, entry),
                                           name=f"cursor-{tid}", daemon=True)
        with _LOCK:
            for k in [k for k in _JOBS if k[0] == tid]:
                del _JOBS[k]
            _JOBS[key] = entry
        entry["thread"].start()
        emit(f"{tid}  Cursor review of {tip[:8]} started")
        return
    if entry["thread"].is_alive() or entry.get("handled"):
        return
    entry["handled"] = True
    res = entry["result"]
    if res["status"] != "saved":
        warn(f"{tid}: no Cursor review of {tip[:8]}: {res['reason']}")
        return
    _outcome(t, tip, res["counted"], res["verdict"], res["reason"], emit, warn, ping, note)
