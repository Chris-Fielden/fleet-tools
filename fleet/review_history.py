"""Read-only helpers: the review rounds of a task folder, the MUST FIX of the last round,
the tests a branch added and its git log. Nothing here writes."""
import re
import subprocess
from pathlib import Path

from fleet import risk as rk

GIT_TIMEOUT = 60


def _git(repo, *args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 1, "", "cannot run git")


def _review_files(wt: Path) -> list[Path]:
    """Saved reviews in the task folder. The agent can write there: never follow a link out of it."""
    reviews = wt / ".reviews"
    if wt.is_symlink() or reviews.is_symlink() or not reviews.is_dir() or reviews.resolve().parent != wt.resolve():
        return []
    return [p for p in sorted(reviews.glob("*.md")) if p.is_file() and not p.is_symlink()]


def review_history(repo: Path, wt: Path, base: str, tip: str) -> list[dict]:
    """Every review round so far: [{commit, reviews: {kind: {reviewer, verdict, must_fix}}}],
    in the branch's order (commits not in base..tip first)."""
    out = _git(repo, "rev-list", "--reverse", f"{base}..{tip}")
    order = {c: i for i, c in enumerate(out.stdout.split())} if out.returncode == 0 else {}
    rounds: dict[str, dict] = {}
    for path in _review_files(wt):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        info = rk.parse_review(text)
        if not info["commit"] or not info["kind"]:
            continue
        rounds.setdefault(info["commit"], {})[info["kind"]] = {
            "reviewer": info["reviewer"], "verdict": info["verdict"],
            "must_fix": rk.review_items(text, "MUST FIX")}
    return [{"commit": c, "reviews": r}
            for c, r in sorted(rounds.items(), key=lambda kv: (order.get(kv[0], -1), kv[0]))]


def latest_must_fix(history: list[dict]) -> str:
    """The MUST FIX text of the last round, every reviewer's."""
    if not history:
        return ""
    return "\n\n".join(f"{r['reviewer'] or k}:\n{r['must_fix']}"
                       for k, r in sorted(history[-1]["reviews"].items()) if r["must_fix"])


def added_tests(repo: Path, base: str, tip: str) -> list[str]:
    """Names of the test functions the branch added, as file::name."""
    out = _git(repo, "diff", "-U0", f"{base}...{tip}")
    if out.returncode != 0:
        return []
    names, current = [], None
    for line in out.stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and "test" in current.lower():
            m = re.match(r"\+\s*(?:async\s+)?def (test\w*)", line)
            if m:
                names.append(f"{current}::{m.group(1)}")
    return names


def git_log(repo: Path, base: str, tip: str) -> list[str]:
    """One line per commit of base..tip, newest first."""
    out = _git(repo, "log", "--format=%h %s", f"{base}..{tip}")
    return out.stdout.splitlines() if out.returncode == 0 else []
