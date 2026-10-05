"""Duplicate tickets: same branch, or a very similar title, among unfinished tickets."""

from __future__ import annotations

import difflib
import re

from . import ticket as tk

THRESHOLD = 0.8


def normalize(title: str) -> str:
    """Lowercase, punctuation removed, spaces collapsed."""
    return " ".join(re.sub(r"[^\w\s]", "", title.lower()).split())


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _live(tickets: list[dict], repo: str) -> list[dict]:
    return [t for t in tickets if t["repo"] == repo and t["status"] in tk.ACTIVE_STATUSES]


def _follow_up(new: dict, other: dict) -> bool:
    """A ticket may share the branch and a title with its parent (a fix, a review or a
    build that follows up another ticket)."""
    return new.get("parent") == other["id"]


def branch_conflicts(fields: dict, tickets: list[dict]) -> list[dict]:
    """Unfinished tickets in the same repo that already use this branch."""
    return [t for t in _live(tickets, fields["repo"])
            if t["branch"] == fields["branch"] and not _follow_up(fields, t)]


def similar_titles(fields: dict, tickets: list[dict]) -> list[tuple[dict, float]]:
    """Unfinished tickets in the same repo with a very similar title, best first."""
    found = [(t, similarity(fields["title"], t["title"])) for t in _live(tickets, fields["repo"])
             if not _follow_up(fields, t)]
    return sorted(((t, r) for t, r in found if r >= THRESHOLD), key=lambda p: -p[1])


def pairs(tickets: list[dict]) -> list[dict]:
    """Likely duplicate pairs among unfinished tickets of one repo: those sharing a
    branch and/or with similar titles. Each is {a, b, repo, branch, ratio}: `branch`
    is the shared branch or None, `ratio` the title similarity."""
    live = sorted((t for t in tickets if t["status"] in tk.ACTIVE_STATUSES), key=tk.oldest_first_key)
    out = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if a["repo"] != b["repo"] or b.get("parent") == a["id"] or a.get("parent") == b["id"]:
                continue
            ratio = similarity(a["title"], b["title"])
            shared = a["branch"] == b["branch"]
            if shared or ratio >= THRESHOLD:
                out.append({"a": a["id"], "b": b["id"], "repo": a["repo"],
                            "branch": a["branch"] if shared else None, "ratio": ratio})
    return out


def pair_line(p: dict) -> str:
    why = [f"title {p['ratio']:.2f}"]
    if p["branch"]:
        why.insert(0, f"same branch {p['branch']}")
    return f"{p['a']}  {p['b']}  {p['repo']}  " + ", ".join(why)
