"""Risk levels (low, medium, high): rules, ticket starting level, merge gate.

Rules live in risk-rules.json in this repo. The level of a change is the
highest level of any file it touches. A ticket's stored level only ever goes
up: the higher of stored and diff always wins. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

LEVELS = ("low", "medium", "high")
RULES_NAME = "risk-rules.json"
REPO_ROOT = Path(__file__).resolve().parent.parent


class RiskError(Exception):
    """A plain one-line problem."""


def rank(level: str) -> int:
    return LEVELS.index(level)


def higher(a: str | None, b: str | None) -> str | None:
    """The higher of two levels; None counts as lowest."""
    if a is None:
        return b
    if b is None:
        return a
    return a if rank(a) >= rank(b) else b


# --- rules -------------------------------------------------------------------

def _glob_re(pattern: str) -> re.Pattern:
    """'**' crosses folders, '*' and '?' do not. No '/' in the pattern means
    the file name in any folder."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if "/" not in pattern:
        body = "(?:.*/)?" + body
    return re.compile(body)


def parse_rules(data) -> dict:
    """Validate a decoded rules file. Returns it with compiled matchers."""
    if not isinstance(data, dict):
        raise RiskError(f"{RULES_NAME} must contain a JSON object")
    default = data.get("default")
    if default not in LEVELS:
        raise RiskError(f"{RULES_NAME}: 'default' must be one of: {', '.join(LEVELS)}")
    rules = []
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise RiskError(f"{RULES_NAME}: 'rules' must be a list")
    for i, r in enumerate(raw_rules):
        if not isinstance(r, dict) or r.get("level") not in LEVELS:
            raise RiskError(f"{RULES_NAME}: rule {i + 1} needs a 'level' of {', '.join(LEVELS)}")
        glob, contains = r.get("glob"), r.get("contains")
        if (glob is None) == (contains is None) or not isinstance(glob or contains, str) \
                or not (glob or contains):
            raise RiskError(f"{RULES_NAME}: rule {i + 1} needs exactly one of 'glob' or 'contains'")
        if glob is not None:
            rules.append({"level": r["level"], "text": glob, "match": _glob_re(glob).fullmatch})
        else:
            needle = contains.lower()
            rules.append({"level": r["level"], "text": f"contains '{contains}'",
                          "match": lambda p, n=needle: n in p.lower()})
    start = data.get("ticket_start") or {}
    if not isinstance(start, dict):
        raise RiskError(f"{RULES_NAME}: 'ticket_start' must be an object")
    by_type, by_repo = start.get("by_type") or {}, start.get("by_repo") or {}
    for table in (by_type, by_repo):
        if not isinstance(table, dict) or any(v not in LEVELS for v in table.values()):
            raise RiskError(f"{RULES_NAME}: 'ticket_start' levels must be one of: {', '.join(LEVELS)}")
    return {"default": default, "rules": rules, "by_type": by_type, "by_repo": by_repo}


def load_rules(text: str | None = None) -> dict:
    """Rules from `text` if given, else FLEET_RISK_RULES or the repo's file."""
    if text is None:
        path = Path(os.environ.get("FLEET_RISK_RULES") or REPO_ROOT / RULES_NAME)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RiskError(f"cannot read {path}: {exc}") from None
    try:
        return parse_rules(json.loads(text))
    except ValueError as exc:
        raise RiskError(f"{RULES_NAME} is not valid JSON: {exc}") from None


def classify(path: str, rules: dict) -> tuple[str, str]:
    """(level, matching rule) for one path. The highest matching rule wins."""
    best = None
    for r in rules["rules"]:
        if r["match"](path) and (best is None or rank(r["level"]) > rank(best["level"])):
            best = r
    if best is None:
        return rules["default"], "no rule (default)"
    return best["level"], best["text"]


def classify_all(paths, rules: dict):
    """([(path, level, rule)], overall level). No files means the lowest."""
    rows = [(p, *classify(p, rules)) for p in paths]
    overall = None
    for _, level, _ in rows:
        overall = higher(overall, level)
    return rows, overall or LEVELS[0]


def starting_level(ticket_type: str, repo: str, rules: dict | None = None) -> str:
    """Level a new ticket starts at: its repo's if the repo has one, else its
    type's, else the default."""
    rules = rules or load_rules()
    return rules["by_repo"].get(repo) or rules["by_type"].get(ticket_type) or rules["default"]


def stored_level(ticket: dict, rules: dict | None = None) -> str:
    """A ticket from before risk existed counts as the default level."""
    return ticket.get("risk") or (rules or load_rules())["default"]


# --- git ---------------------------------------------------------------------

def _git(repo_dir, *args) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RiskError(f"cannot run git: {exc}") from None
    if proc.returncode != 0:
        raise RiskError(f"git {args[0]} failed: {proc.stderr.strip() or 'unknown error'}")
    return proc.stdout


def changed_files(repo_dir, rev_range: str) -> list[str]:
    """Files changed in BASE...BRANCH. Renames count as a delete plus an add so
    both paths are judged."""
    if "..." not in rev_range or rev_range.startswith("-"):
        raise RiskError("range must look like BASE...BRANCH")
    out = _git(repo_dir, "diff", "--name-only", "--no-renames", "-z", rev_range, "--")
    return sorted(p for p in out.split("\0") if p)


def rules_for_range(repo_dir, rev_range: str) -> dict:
    """The rules as they are on BASE, so a branch cannot weaken its own rules.
    Falls back to this tool's own file when BASE has none."""
    base = rev_range.split("...", 1)[0]
    try:
        text = _git(repo_dir, "show", f"{base}:{RULES_NAME}")
    except RiskError:
        return load_rules()
    return load_rules(text)


def level_for_branch(tickets: list[dict], branch: str, rules: dict | None = None):
    """(highest stored level or None, ticket ids) over the tickets on a branch."""
    found = [t for t in tickets if t["branch"] == branch]
    level = None
    for t in found:
        level = higher(level, stored_level(t, rules))
    return level, [t["id"] for t in found]


# --- review files ------------------------------------------------------------

VERDICT_RE = re.compile(r"^[\s*#>_`-]*VERDICT[\s*_`]*:[\s*_`]*(PASS|FAIL)\b", re.I)
STRONG_CLAUDE_RE = re.compile(r"opus|fable", re.I)


def parse_review(text: str) -> dict:
    """review.sh writes header lines (Reviewer:, Commit:), a blank line, then
    the review. The verdict is the first VERDICT line after the header."""
    lines = text.splitlines()
    head = {}
    body_start = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            body_start = i + 1
            break
        k, _, v = line.partition(":")
        head[k.strip().lower()] = v.strip()
    verdict = None
    for line in lines[body_start:]:
        m = VERDICT_RE.match(line)
        if m:
            verdict = m.group(1).upper()
            break
    reviewer = head.get("reviewer", "")
    low = reviewer.lower()
    kind = "codex" if low.startswith("codex") else "claude" if low.startswith("claude") else None
    return {"kind": kind, "reviewer": reviewer, "commit": head.get("commit"), "verdict": verdict}


def latest_reviews(reviews_dir: Path, full_sha: str) -> dict:
    """The newest review of exactly `full_sha`, per reviewer kind."""
    out = {}
    if not reviews_dir.is_dir():
        return out
    files = sorted((p for p in reviews_dir.glob("*.md") if p.is_file()),
                   key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            info = parse_review(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if info["commit"] == full_sha and info["kind"]:
            info["file"] = str(p)
            out[info["kind"]] = info
    return out


def check_reviews(reviews: dict, level: str) -> list[str]:
    """Problems that stop a merge (empty list = fine)."""
    problems = []
    if not reviews:
        problems.append("no review of this exact commit; run scripts/review.sh")
    for kind, info in reviews.items():
        if info["verdict"] != "PASS":
            problems.append(f"the latest {kind} review of this commit is "
                            f"{info['verdict'] or 'unreadable'}, not PASS ({info['file']})")
    if level == "high":
        for kind in ("codex", "claude"):
            if kind not in reviews:
                problems.append(f"high risk needs a PASS review from {kind.capitalize()} of this "
                                "commit; run REVIEWER=both scripts/review.sh")
        c = reviews.get("claude")
        if c and not STRONG_CLAUDE_RE.search(c["reviewer"]):
            problems.append(f"high risk needs a stronger Claude model (opus or fable), got: {c['reviewer']}")
    return problems
