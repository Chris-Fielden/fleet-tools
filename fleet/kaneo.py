"""One-way mirror of the local ticket store onto a Kaneo board (phase 2).

The local store stays the source of truth; Kaneo is only a view. Nothing is
ever read back into a ticket. Standard library only.

Kaneo endpoints used (https://kaneo.app/docs/llms.txt, API reference):
  GET  /project?workspaceId=...   list projects (find one by exact name)
  POST /project                   create a project (name, workspaceId, icon, slug)
  GET  /column/{projectId}        list a project's columns
  POST /column/{projectId}        create a column (name, isFinal)
  PUT  /column/{id}               mark an existing Done/Failed column final
  PUT  /column/reorder/{projectId}  set column positions (columns: [{id, position}])
  DELETE /column/{id}             remove a leftover column (409 if it holds tasks)
  GET  /task/tasks/{projectId}    board view; used to check a column is empty
  POST /task/{projectId}          create a task (title, description, priority, status)
  GET  /label/task/{taskId}       a card's labels
  POST /label                     create a label on a card (name, color, workspaceId, taskId)
  DELETE /label/{id}/task         detach a label from its card
  GET  /task/{id}?view=detail     read a task, to compare before updating
  PUT  /task/{id}                 update a task (title, description, priority,
                                  status, projectId, position)
A task's `status` is the slug of the column it sits in; Kaneo derives the slug
from the column name and returns it when the column is created or listed.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import risk as rk
from . import ticket as tk

TIMEOUT = 10  # seconds per request
# Columns, left to right. Tickets use Kaneo's own To Do / In Progress / Done;
# Proposed, Urgent and Failed are ours. An open urgent ticket sits in Urgent
# instead of To Do. In Review is created so the order is right,
# but nothing is put there yet (the dispatcher phase will).
COLUMNS = ("Proposed", "Urgent", "To Do", "In Progress", "In Review", "Done", "Failed")
FINAL_COLUMNS = ("Done", "Failed")
# Kaneo has no per-card archive call, so a cancelled ticket sits in Done,
# marked in its title.
STATUS_COLUMN = {"proposed": "Proposed", "open": "To Do", "claimed": "In Progress",
                 "done": "Done", "cancelled": "Done", "failed": "Failed"}
# Per-status columns an earlier version of the sync created.
LEGACY_COLUMNS = ("open", "claimed", "cancelled")
PRIORITY = "no-priority"
URGENT_PRIORITY = "urgent"
RISK_LABEL_PREFIX = "risk: "
RISK_LABEL_COLORS = {"low": "#2da44e", "medium": "#d4a72c", "high": "#cf222e"}


class KaneoError(Exception):
    """A plain one-line problem. Never contains the API key."""


class NotConfigured(Exception):
    """There is no kaneo.json, so sync is off."""


# --- configuration -----------------------------------------------------------

def config_path() -> Path:
    return tk.fleet_home() / "kaneo.json"


def map_path() -> Path:
    return tk.fleet_home() / "kaneo-map.json"


def load_config() -> dict:
    """Read kaneo.json. Raises NotConfigured if absent, KaneoError if unusable.

    No message here ever includes file contents, so the key cannot leak.
    """
    path = config_path()
    tk.refuse_real_store_in_tests(path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        raise NotConfigured() from None
    except OSError as exc:
        raise KaneoError(f"cannot read {path}: {exc.strerror or 'error'}") from None
    if mode & 0o077:
        raise KaneoError(
            f"{path} can be read by other users (mode {mode:04o}); "
            f"refusing to use it. Fix with: chmod 600 {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise KaneoError(f"{path} is not readable JSON") from None
    if not isinstance(data, dict):
        raise KaneoError(f"{path} must contain a JSON object")
    for name in ("url", "workspace_id", "api_key"):
        if not isinstance(data.get(name), str) or not data[name]:
            raise KaneoError(f"{path} needs a non-empty '{name}'")
    if not re.fullmatch(r"[\x21-\x7e]+", data["api_key"]):
        raise KaneoError(f"'api_key' in {path} must be printable text with no spaces or "
                         "line breaks (check for a trailing newline)")
    try:
        parsed = urllib.parse.urlsplit(data["url"])
    except ValueError:
        raise KaneoError(f"'url' in {path} is not a valid URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise KaneoError(f"'url' in {path} must start with http:// or https://")
    return {"url": data["url"].rstrip("/"), "workspace_id": data["workspace_id"],
            "api_key": data["api_key"]}


# --- HTTP --------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would resend the Authorization header
    to wherever the server points us."""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, cfg: dict):
        self._base = cfg["url"]
        self._key = cfg["api_key"]
        self.workspace_id = cfg["workspace_id"]
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, path: str, body=None, query=None):
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        what = f"{method} {path}"
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Deliberately ignore the response body: it is not ours to print.
            raise KaneoError(f"Kaneo answered HTTP {exc.code} to {what}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise KaneoError(f"Kaneo timed out after {TIMEOUT}s on {what}") from None
            raise KaneoError(f"Kaneo unreachable ({_reason(exc.reason)})") from None
        except TimeoutError:
            raise KaneoError(f"Kaneo timed out after {TIMEOUT}s on {what}") from None
        except ValueError:
            # e.g. a malformed URL; the message could quote the header, so say nothing of it
            raise KaneoError(f"Kaneo request could not be made ({what})") from None
        except (OSError, http.client.HTTPException) as exc:
            raise KaneoError(f"Kaneo request failed ({type(exc).__name__}) on {what}") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise KaneoError(f"Kaneo sent a reply that is not JSON to {what}") from None


def _reason(reason) -> str:
    if isinstance(reason, OSError):
        return reason.strerror or type(reason).__name__
    return type(reason).__name__ if not isinstance(reason, str) else reason


# --- ticket id -> task id map ------------------------------------------------

@contextlib.contextmanager
def _map_locked():
    home = tk.fleet_home()
    tk.refuse_real_store_in_tests(home)
    home.mkdir(parents=True, exist_ok=True)
    with open(home / ".kaneo-sync.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load_map() -> dict:
    path = map_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError):
        raise KaneoError(f"{path} is unreadable; fix or delete it (cards would be duplicated)") from None
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise KaneoError(f"{path} is not a map of ticket id to task id; fix or delete it")
    return data


# --- mapping -----------------------------------------------------------------

def is_urgent(t: dict) -> bool:
    return t.get("priority") == "urgent"


def card_priority(t: dict) -> str:
    return URGENT_PRIORITY if is_urgent(t) else PRIORITY


def card_title(t: dict) -> str:
    suffix = " (cancelled)" if t["status"] == "cancelled" else ""
    return f"[{t['id']}] {t['title']}{suffix}"


def card_description(t: dict) -> str:
    def val(name):
        return "-" if t[name] is None else t[name]

    lines = [f"{name}: {val(name)}" for name in
             ("type", "branch", "commit", "parent", "created_by", "assignee")]
    if t["verdict"] is not None:
        lines.append(f"verdict: {t['verdict']}")
    notes = "".join(f"\n\n**Note by {n['by']}, {n['at']}**\n\n{n['text']}"
                    for n in t.get("notes", []))
    return f"{t['body']}\n\n```\n" + "\n".join(lines) + "\n```" + notes


def _slug_for(repo: str) -> str:
    """Project slug: the prefix of Kaneo's task identifiers."""
    s = re.sub(r"[^A-Za-z0-9]", "", repo).upper()[:8]
    return s or "FLEET"


class _Board:
    """Per-run cache of projects and columns. With create=False (dry run) it
    never writes and returns None for anything that does not exist yet."""

    def __init__(self, client: Client, create: bool):
        self.c = client
        self.create = create
        self._projects: dict[str, str | None] = {}
        self._columns: dict[str, dict[str, dict]] = {}

    def project_id(self, repo: str):
        if repo in self._projects:
            return self._projects[repo]
        found = None
        listing = self.c.request("GET", "/project", query={"workspaceId": self.c.workspace_id})
        for p in listing or []:
            if isinstance(p, dict) and p.get("name") == repo:
                found = p["id"]
                break
        if found is None and self.create:
            made = self.c.request("POST", "/project", body={
                "name": repo, "workspaceId": self.c.workspace_id,
                "icon": "Layout", "slug": _slug_for(repo)})
            found = made["id"]
        self._projects[repo] = found
        return found

    def _columns_of(self, project_id: str) -> dict[str, dict]:
        """Columns by lower-cased name, in board order."""
        cols = self._columns.get(project_id)
        if cols is None:
            listing = self.c.request("GET", f"/column/{project_id}")
            found = [c for c in listing or [] if isinstance(c, dict)]
            found.sort(key=lambda c: c.get("position") or 0)
            cols = {}
            for c in found:
                cols.setdefault(c["name"].lower(), c)
            if self.create:
                # Cache only once the board is complete, so a failure here is
                # retried by the next ticket instead of leaving a half board.
                self._fix_board(project_id, cols)
            self._columns[project_id] = cols
        return cols

    def _fix_board(self, project_id: str, cols: dict[str, dict]) -> None:
        """Create the missing columns, mark Done/Failed final, fix the order."""
        for name in COLUMNS:
            final = name in FINAL_COLUMNS
            col = cols.get(name.lower())
            if col is None:
                cols[name.lower()] = self.c.request("POST", f"/column/{project_id}", body={
                    "name": name, "isFinal": final})
            elif final and not col.get("isFinal"):
                self.c.request("PUT", f"/column/{col['id']}", body={"isFinal": True})
                col["isFinal"] = True
        ours = [name.lower() for name in COLUMNS]
        extras = [k for k in cols if k not in ours]  # the human's own columns
        order = ours[:ours.index("done")] + extras + ours[ours.index("done"):]
        have = sorted(cols, key=lambda k: cols[k].get("position") or 0)
        if have != order:
            self.c.request("PUT", f"/column/reorder/{project_id}", body={
                "columns": [{"id": cols[k]["id"], "position": i} for i, k in enumerate(order)]})
            for i, k in enumerate(order):
                cols[k]["position"] = i

    def column_slug(self, project_id: str, t: dict):
        name = "Urgent" if t["status"] == "open" and is_urgent(t) else STATUS_COLUMN[t["status"]]
        col = self._columns_of(project_id).get(name.lower())
        return None if col is None else col["slug"]

    def prune(self) -> None:
        """Delete columns an earlier sync created (open, claimed, cancelled),
        but only when nothing is in them. Best effort: Kaneo itself also
        refuses (409) to delete a column that holds cards."""
        if not self.create:
            return
        for pid, cols in self._columns.items():
            for name in LEGACY_COLUMNS:
                col = cols.get(name)
                if col is None:
                    continue
                try:
                    if self._is_empty(pid, col["slug"]):
                        self.c.request("DELETE", f"/column/{col['id']}")
                        del cols[name]
                except (KaneoError, KeyError, TypeError, AttributeError):
                    pass  # leave it; the next sync tries again

    def _is_empty(self, project_id: str, slug: str) -> bool:
        board = self.c.request("GET", f"/task/tasks/{project_id}",
                               query={"status": slug, "limit": 1})
        columns = board["data"]["columns"]
        if not isinstance(columns, list):
            return False
        return not any(col["tasks"] for col in columns)


def _mirror_card(t: dict, board: _Board, tasks: dict, dry_run: bool) -> str:
    """Mirror one ticket's card. Returns created, updated or unchanged (a dry run
    returns 'would create' / 'would update' / 'unchanged'). Mutates `tasks`
    (the id map) on create; the caller saves it."""
    c = board.c
    verb = (lambda v: f"would {v}") if dry_run else (lambda v: v + "d")
    pid = board.project_id(t["repo"])
    existing = None
    if t["id"] in tasks:
        try:
            existing = c.request("GET", f"/task/{tasks[t['id']]}", query={"view": "detail"})
        except KaneoError as exc:
            if "HTTP 404" not in str(exc):
                raise
            # The card was deleted in Kaneo; make a fresh one.
    if pid is None:
        return verb("create")
    slug = board.column_slug(pid, t)
    title, desc = card_title(t), card_description(t)

    if existing is None:
        if slug is None:
            return verb("create")
        if dry_run:
            return verb("create")
        made = c.request("POST", f"/task/{pid}", body={
            "title": title, "description": desc, "priority": card_priority(t), "status": slug})
        tasks[t["id"]] = made["id"]
        return "created"

    if (slug is not None and existing.get("projectId") == pid
            and existing.get("title") == title
            and existing.get("description") == desc and existing.get("status") == slug
            and existing.get("priority") == card_priority(t)):
        return "unchanged"
    if dry_run:
        return verb("update")
    c.request("PUT", f"/task/{tasks[t['id']]}", body={
        "title": title, "description": desc,
        "priority": card_priority(t),
        "status": slug, "projectId": pid,
        "position": existing.get("position") or 0})
    return "updated"


def risk_label(t: dict) -> str:
    return f"{RISK_LABEL_PREFIX}{rk.stored_level(t)}"


def _sync_label(t: dict, board: _Board, task_id: str, dry_run: bool) -> bool:
    """Make the card carry exactly one risk label, the right one. Returns True
    if it had to change (or, on a dry run, would have to)."""
    c = board.c
    want = risk_label(t)
    have = c.request("GET", f"/label/task/{task_id}") or []
    names = {l["id"]: l.get("name") for l in have if isinstance(l, dict)}
    stale = [i for i, n in names.items()
             if isinstance(n, str) and n.startswith(RISK_LABEL_PREFIX) and n != want]
    if want in names.values() and not stale:
        return False
    if dry_run:
        return True
    for label_id in stale:
        c.request("DELETE", f"/label/{label_id}/task")
    if want not in names.values():
        c.request("POST", "/label", body={
            "name": want, "color": RISK_LABEL_COLORS[rk.stored_level(t)],
            "workspaceId": c.workspace_id, "taskId": task_id})
    return True


def _mirror(t: dict, board: _Board, tasks: dict, dry_run: bool) -> str:
    """Mirror one ticket: its card, then its risk label."""
    outcome = _mirror_card(t, board, tasks, dry_run)
    task_id = tasks.get(t["id"])
    if task_id is None or outcome.startswith("would create"):
        return outcome
    try:
        label_changed = _sync_label(t, board, task_id, dry_run)
    except KaneoError as exc:
        # The card itself is already mirrored; only the label is behind.
        raise KaneoError(f"card mirrored, but its risk label was not ({exc})") from None
    if label_changed and outcome == "unchanged":
        return "would update" if dry_run else "updated"
    return outcome


def sync(tickets: list[dict], dry_run: bool = False):
    """Mirror tickets. Yields (ticket_id, outcome) where outcome is one of
    created/updated/unchanged (or 'would ...' on a dry run) or 'error: ...'.
    Raises NotConfigured / KaneoError for problems with the setup itself."""
    client = Client(load_config())
    board = _Board(client, create=not dry_run)
    with _map_locked():
        tasks = _load_map()
        for t in tickets:
            try:
                before = dict(tasks)
                try:
                    # Re-read under the lock: the snapshot may be stale if another
                    # command changed this ticket while we waited.
                    outcome = _mirror(tk.load_ticket(t["id"]), board, tasks, dry_run)
                finally:
                    # Save a new card's id even if the label step failed after
                    # it, or the retry would make a duplicate card.
                    if tasks != before:
                        tk._write_atomic(map_path(), tasks)
            except (KaneoError, tk.TicketError, tk.TicketInvalid) as exc:
                outcome = f"error: {exc}"
            except OSError as exc:
                outcome = (f"error: could not save {map_path().name} ({exc.strerror or 'I/O error'}); "
                           "a card may be duplicated on the next sync")
            except (KeyError, TypeError, AttributeError):
                outcome = "error: Kaneo sent a reply in an unexpected shape"
            yield t["id"], outcome
        board.prune()


def mirror_quietly(ticket: dict, warn) -> None:
    """Mirror one ticket after `ticket new` / `ticket approve`. Never raises
    for Kaneo trouble: the ticket is saved locally and the next `ticket sync`
    catches up. `warn` receives a one-line message."""
    try:
        for tid, outcome in sync([ticket]):
            if outcome.startswith("error: "):
                warn(f"warning: {tid} saved, but not shown on Kaneo ({outcome[7:]}); "
                     "run `ticket sync` later")
    except NotConfigured:
        pass
    except (KaneoError, OSError) as exc:
        warn(f"warning: ticket saved, but Kaneo sync was skipped ({exc})")
