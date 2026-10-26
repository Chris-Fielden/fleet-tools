"""File-area locks: serialise tickets whose declared path globs intersect.

A ticket may declare `areas`, the repo-relative path globs its work touches. The
dispatcher takes a lock on them when it launches the ticket and refuses to launch
another ticket of the same repo whose areas intersect a live lock. A lock lives
for a lease (renewed while the ticket's agent runs) and is dropped when its ticket
leaves claimed/review, so a crashed chat or agent cannot hold an area forever.
`overlaps` also compares the real diffs of active branches (review_watch pings).

Globs: '**' crosses folders, '*' and '?' stay inside one folder. A path with no
wildcard and no file extension, or one ending in '/', also covers everything below it.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import risk as rk
from . import ticket as tk

DEFAULT_LEASE_HOURS = 6
HOLDING = ("claimed", "review")   # a lock belongs to a ticket only while it is in one of these
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class AreaConflict(Exception):
    pass


# --- glob intersection ---------------------------------------------------------

def _has_wild(text: str) -> bool:
    return any(c in text for c in "*?[")


def expand(area: str) -> list[str]:
    """The patterns one area stands for: itself, plus everything below it when it names a folder."""
    plain = area.rstrip("/")
    last = plain.rsplit("/", 1)[-1]
    if area.endswith("/") or (not _has_wild(last) and not rk.EXTENSION_RE.search(last)):
        return [area, plain + "/**"]
    return [area]


def _seg_ok(x: str, y: str) -> bool:
    if _has_wild(x) and _has_wild(y):
        return True   # cannot tell: assume they meet
    if _has_wild(x):
        return fnmatch.fnmatchcase(y, x)
    if _has_wild(y):
        return fnmatch.fnmatchcase(x, y)
    return x == y


def _meet(a: list[str], b: list[str]) -> bool:
    if not a and not b:
        return True
    if a and a[0] == "**":
        return _meet(a[1:], b) or (bool(b) and _meet(a, b[1:]))
    if b and b[0] == "**":
        return _meet(a, b[1:]) or (bool(a) and _meet(a[1:], b))
    if not a or not b:
        return False
    return _seg_ok(a[0], b[0]) and _meet(a[1:], b[1:])


def intersects(a: str, b: str) -> bool:
    """Could some path match both globs? Errs towards yes."""
    return any(_meet(x.split("/"), y.split("/")) for x in expand(a) for y in expand(b))


def first_overlap(mine, theirs) -> tuple[str, str] | None:
    for a in mine:
        for b in theirs:
            if intersects(a, b):
                return a, b
    return None


def high_areas(areas, rules: dict) -> list[str]:
    """The areas that may reach a file the risk rules call high. A rule's glob is compared as a
    glob (one without '/' means that name in any folder); a 'contains' rule is compared to the
    text of the area. Wildcards that could only reach a 'contains' path by luck are not caught
    here: the merge gate judges the real diff."""
    out = []
    for area in areas:
        for r in rules["rules"]:
            if r["level"] != "high":
                continue
            glob, needle = r.get("glob"), r.get("contains")
            if glob is not None:
                pattern = glob if "/" in glob else "**/" + glob
                hit = intersects(area, pattern)
            else:
                hit = needle is not None and needle in area.lower()
            if hit:
                out.append(area)
                break
    return out


# --- the lock table ------------------------------------------------------------

def table_path() -> Path:
    return tk.fleet_home() / "area-locks.json"


@contextlib.contextmanager
def _table_lock():
    path = table_path()
    tk.refuse_real_store_in_tests(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / "area-locks.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_TIME)


def _parse(text) -> datetime | None:
    try:
        return datetime.strptime(text, _TIME).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _read() -> dict:
    """{ticket id: {repo, areas, taken_at, lease_until}}. A missing file is empty; a damaged
    one raises OSError (callers fail closed: nothing is launched on a table they cannot read)."""
    try:
        data = json.loads(table_path().read_text(encoding="utf-8"))
        locks = data["locks"]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OSError(f"cannot read {table_path()}: {exc}") from None
    if not isinstance(locks, dict):
        raise OSError(f"{table_path()} has a bad lock table")
    good = {}
    for tid, e in locks.items():
        if (isinstance(e, dict) and isinstance(e.get("repo"), str) and _parse(e.get("lease_until"))
                and not tk.check_areas(e.get("areas"))):
            good[tid] = e
        else:
            raise OSError(f"{table_path()}: bad entry for {tid}")
    return good


def _write(locks: dict) -> None:
    path = table_path()
    tk.refuse_real_store_in_tests(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"locks": locks}, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def live(locks: dict, tickets: list[dict], now: datetime) -> dict:
    """The locks that still count: not past their lease, and their ticket still holds its branch."""
    holding = {t["id"] for t in tickets if t["status"] in HOLDING}
    return {tid: e for tid, e in locks.items()
            if tid in holding and _parse(e["lease_until"]) > now}


def blockers(ticket: dict, locks: dict, tickets: list[dict], now: datetime) -> list[tuple[str, str, str]]:
    """(holder id, my area, its area) for each live lock of another ticket in the same repo that
    this ticket's declared areas intersect. A ticket with no areas is never held back."""
    mine = ticket.get("areas")
    if not mine:
        return []
    out = []
    for tid, e in sorted(live(locks, tickets, now).items()):
        if tid == ticket["id"] or e["repo"] != ticket["repo"]:
            continue
        hit = first_overlap(mine, e["areas"])
        if hit:
            out.append((tid, *hit))
    return out


def blocked_reason(ticket: dict, tickets: list[dict], now: datetime | None = None) -> str | None:
    """Why this ticket must wait for an area lock, or None. Reads the table; an unreadable
    table blocks (fail closed)."""
    if not ticket.get("areas"):
        return None
    now = now or datetime.now(timezone.utc)
    try:
        hits = blockers(ticket, _read(), tickets, now)
    except OSError as exc:
        return f"the area lock table can't be read ({exc})"
    if not hits:
        return None
    tid, mine, theirs = hits[0]
    return f"waiting for the area lock of {tid} ({mine} meets {theirs})"


def acquire(ticket: dict, tickets: list[dict], now: datetime | None = None,
            lease_hours: float = DEFAULT_LEASE_HOURS) -> bool:
    """Take the lock for a ticket about to start (or already started). False when it has no
    areas. Raises AreaConflict when a live lock meets its areas. Idempotent for its own lock."""
    if not ticket.get("areas"):
        return False
    now = now or datetime.now(timezone.utc)
    with _table_lock():
        locks = _read()
        hits = blockers(ticket, locks, tickets, now)
        if hits:
            raise AreaConflict(f"{ticket['id']} {hits[0][0]} holds {hits[0][2]}")
        keep = live(locks, tickets + [ticket], now)   # the new ticket itself may not be claimed yet
        keep[ticket["id"]] = {"repo": ticket["repo"], "areas": list(ticket["areas"]),
                              "taken_at": _stamp(now),
                              "lease_until": _stamp(now + timedelta(hours=lease_hours))}
        _write(keep)
    return True


def release(ticket_id: str) -> bool:
    with _table_lock():
        locks = _read()
        if ticket_id not in locks:
            return False
        del locks[ticket_id]
        _write(locks)
    return True


def sweep(tickets: list[dict], running, now: datetime | None = None,
          lease_hours: float = DEFAULT_LEASE_HOURS) -> list[str]:
    """Once per dispatcher cycle: drop locks that are expired or whose ticket is no longer
    holding its branch, and renew those of tickets whose agent is still running (`running`:
    lowercase ids of agents that have not stopped; None = herdr could not be asked, so
    nothing is renewed). Returns the ids dropped."""
    now = now or datetime.now(timezone.utc)
    with _table_lock():
        locks = _read()
        renewed = _stamp(now + timedelta(hours=lease_hours))
        in_review = {t["id"] for t in tickets if t["status"] == "review"}   # waits for the human: no agent, lock stays
        keep = live({tid: ({**e, "lease_until": renewed} if tid in in_review else e) for tid, e in locks.items()},
                    tickets, now)
        for tid in keep:
            if running is not None and tid.lower() in running:
                keep[tid] = {**keep[tid], "lease_until": renewed}
        dropped = sorted(set(locks) - set(keep))
        if dropped or keep != locks:
            _write(keep)
    return dropped


def overlaps(tickets: list[dict], diffs: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    """(id, other id, file) for each shared file of each pair of branch holders of one repo whose real diffs share a
    file. `diffs` maps ticket id to the files its branch changed against main."""
    holders = sorted((t for t in tickets if t["status"] in HOLDING and t["id"] in diffs),
                     key=lambda t: t["id"])
    out = []
    for i, a in enumerate(holders):
        for b in holders[i + 1:]:
            if a["repo"] != b["repo"]:
                continue
            for path in sorted(set(diffs[a["id"]]) & set(diffs[b["id"]])):
                out.append((a["id"], b["id"], path))
    return out
