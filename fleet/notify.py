"""Best-effort phone notifications through a self-hosted ntfy server.

Off unless notify.json exists in the fleet home. Only the ticket id, type,
status and a short title are ever sent: never the body or notes. A failure
only warns; it never fails the command that caused it. Standard library only.

notify.json: {"url": "http://100.x.y.z:2586", "topic": "fleet", "token": "tk_...",
              "follow_ups": false, "priorities": {"stalled": "low"}}
`token` is optional (sent as a bearer token); `follow_ups` turns on messages
for proposed tickets; `priorities` overrides the ntfy priority of an event
(min, low, default, high or max), see PRIORITIES for the defaults.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import ticket as tk
from . import webconf

TIMEOUT = 4  # seconds per socket operation (not DNS); use an IP url for a firm limit
TITLE_MAX = 60
# DEPRECATED_EVENTS (blocked, review_failed, stalled) are no longer sent (a stopped,
# stalled or blocked agent now brings in an on-call agent instead); the names stay
# valid only so an older "priorities" setting in notify.json keeps loading.
DEPRECATED_EVENTS = ("blocked", "review_failed", "stalled")
EVENTS = ("ready", "urgent", "failed", "proposed", "investigated", "ship", "blocked",
          "review_failed", "review_unrecorded", "stalled", "oncall", "needs_human",
          "shipped", "ship_failed", "dispatcher_down", "ship_list", "settings_invalid", "usage_pause", "usage_stop",
          "dialog", "leftovers", "ready_reminder", "reconcile_stuck")
_HEADLINE = {"ready": "Review ready", "urgent": "Urgent ticket", "failed": "Ticket failed",
             "proposed": "Follow-up proposed", "investigated": "Investigation done",
             "ship": "Ready to ship", "blocked": "Agent blocked", "review_failed": "Review FAILED",
             "review_unrecorded": "Review not recorded", "stalled": "Ticket stalled",
             "oncall": "On-call launched", "needs_human": "Needs a human",
             "shipped": "Shipped", "ship_failed": "Ship refused",
             "dispatcher_down": "Dispatcher down", "ship_list": "Ship list ready",
             "settings_invalid": "Settings invalid", "usage_pause": "Launches paused",
             "usage_stop": "Agents stopped: weekly usage", "dialog": "Agent dialog",
             "leftovers": "Ready with leftovers", "ready_reminder": "Still waiting for you",
             "reconcile_stuck": "Ticket stuck"}
# ntfy's own names for its five priorities.
PRIORITY_NUMBERS = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}
# Per event; override any of them under "priorities" in notify.json.
PRIORITIES = {"ready": "low", "urgent": "low", "failed": "high", "proposed": "low",
              "investigated": "low", "ship": "default", "blocked": "high",
              "review_failed": "high", "review_unrecorded": "low", "stalled": "default",
              "oncall": "low", "needs_human": "high", "shipped": "low", "ship_failed": "high",
              "dispatcher_down": "high", "ship_list": "default",
              "settings_invalid": "high", "usage_pause": "low", "usage_stop": "high",
              "dialog": "high", "leftovers": "high", "ready_reminder": "low",
              "reconcile_stuck": "low"}
_TAGS = {"ready": ["mag"], "urgent": ["rotating_light"], "failed": ["x"], "proposed": ["bulb"],
         "investigated": ["mag_right"], "ship": ["white_check_mark"], "blocked": ["hand"],
         "review_failed": ["x"], "review_unrecorded": ["warning"], "stalled": ["hourglass"],
         "oncall": ["pager"], "needs_human": ["sos"], "shipped": ["rocket"],
         "ship_failed": ["no_entry"], "dispatcher_down": ["rotating_light"], "ship_list": ["package"],
         "settings_invalid": ["warning"], "usage_pause": ["pause_button"],
         "usage_stop": ["octagonal_sign"], "dialog": ["hand"],
         "leftovers": ["broom"], "ready_reminder": ["bell"], "reconcile_stuck": ["hourglass"]}
# Events about a failure: their message says why (see _failure_lines).
FAILURE_EVENTS = ("failed", "oncall", "needs_human")
# Events whose `detail` is the whole point (the result of a web ship).
RESULT_EVENTS = ("shipped", "ship_failed")
BODY_MAX = 900


class NotifyError(Exception):
    """A plain one-line problem. Never contains the token."""


class NotConfigured(Exception):
    """There is no notify.json, so notifications are off."""


def config_path() -> Path:
    return tk.fleet_home() / "notify.json"


def load_config() -> dict:
    """Read notify.json. Raises NotConfigured if absent, NotifyError if unusable.
    No message here includes file contents, so the token cannot leak."""
    path = config_path()
    tk.refuse_real_store_in_tests(path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        raise NotConfigured() from None
    except OSError as exc:
        raise NotifyError(f"cannot read {path}: {exc.strerror or 'error'}") from None
    if mode & 0o077:
        raise NotifyError(f"{path} can be read by other users (mode {mode:04o}); "
                          f"refusing to use it. Fix with: chmod 600 {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise NotifyError(f"{path} is not readable JSON") from None
    if not isinstance(data, dict):
        raise NotifyError(f"{path} must contain a JSON object")
    for name in ("url", "topic"):
        if not isinstance(data.get(name), str) or not data[name]:
            raise NotifyError(f"{path} needs a non-empty '{name}'")
    token = data.get("token", "")
    if not isinstance(token, str) or (token and not re.fullmatch(r"[\x21-\x7e]+", token)):
        raise NotifyError(f"'token' in {path} must be printable text with no spaces or line breaks")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["topic"]):
        raise NotifyError(f"'topic' in {path} may only use letters, digits, - and _")
    try:
        parsed = urllib.parse.urlsplit(data["url"])
    except ValueError:
        raise NotifyError(f"'url' in {path} is not a valid URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise NotifyError(f"'url' in {path} must start with http:// or https://")
    given = data.get("priorities", {})
    if not isinstance(given, dict):
        raise NotifyError(f"'priorities' in {path} must be an object like {{\"stalled\": \"low\"}}")
    priorities = dict(PRIORITIES)
    for event, level in given.items():
        if event not in EVENTS:
            raise NotifyError(f"'priorities' in {path} has an unknown event (use: {', '.join(EVENTS)})")
        if not isinstance(level, str) or level not in PRIORITY_NUMBERS:
            raise NotifyError(f"the '{event}' priority in {path} must be one of: "
                              f"{', '.join(PRIORITY_NUMBERS)}")
        priorities[event] = level
    return {"url": data["url"].rstrip("/"), "topic": data["topic"], "token": token,
            "follow_ups": data.get("follow_ups") is True, "priorities": priorities}


def event_for_new(ticket: dict) -> str | None:
    """The event a just-created ticket stands for, if any."""
    if ticket.get("priority") == "urgent":
        return "urgent"
    if ticket["status"] == "open" and ticket["type"] == "review":
        return "ready"
    if ticket["status"] == "proposed":
        return "proposed"
    return None


def _short(text: str, limit: int = TITLE_MAX) -> str:
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _first_must_fix(ticket: dict) -> str:
    """The first line of the MUST FIX section of the saved review that failed
    this ticket's reviewed commit ('' when there is none to read). Best effort."""
    v = ticket.get("verdict")
    if v is None or v["result"] != "FAIL" or not ticket.get("repo") or not ticket.get("branch"):
        return ""
    try:
        from . import dispatch as dp
        from . import risk as rk
        repo = dp.load_settings()["projects_dir"] / ticket["repo"]
        reviews = repo / ".worktrees" / ticket["branch"].removeprefix("task/") / ".reviews"
        for _, info in sorted(rk.latest_reviews(reviews, v["commit"]).items()):
            if info["verdict"] != "FAIL" or Path(info["file"]).is_symlink():
                continue
            must = rk.review_items(Path(info["file"]).read_text(encoding="utf-8"), "MUST FIX")
            for line in must.splitlines():
                line = re.sub(r"^[\s*#>_`-]*(\d+[.)]\s*)?", "", line).strip()
                if line:
                    return line
    except Exception:  # best effort: a missing folder or review only means no line
        return ""
    return ""


def _failure_lines(ticket: dict) -> list[str]:
    """Why a ticket failed, from what the store holds: the reason (the close
    note), who reviewed it, how many rounds failed, and the first MUST FIX line."""
    lines = []
    note = re.sub(r"^Failed:\s*", "", ticket.get("close_note") or "")
    reason = re.split(r";\s*(?:failed by:|FAIL rounds:)", note, maxsplit=1)[0].strip()
    if reason:
        lines.append(f"Reason: {_short(reason, 300)}")
    v = ticket.get("verdict")
    failed = v is not None and v["result"] == "FAIL"
    if failed:
        who = sorted(r["reviewer"] for r in v["reviews"].values() if r["result"] == "FAIL")
        lines.append(f"Reviewer: {_short(', '.join(who), 100)}")
    rounds = set(ticket.get("fail_commits", []))
    if failed:
        rounds.add(v["commit"])
    if rounds:
        lines.append(f"FAIL rounds: {len(rounds)}")
    must = _first_must_fix(ticket)
    if must:
        lines.append(f"MUST FIX: {_short(must, 300)}")
    return lines


def _actions(ticket: dict, event: str) -> list[dict]:
    """Buttons on the notification: open the card; Ship on a ready ticket and
    Approve on a proposed one. Both only open a confirmation page."""
    from . import kaneo
    out = []
    card = kaneo.card_url(ticket["id"])
    if card:
        out.append({"action": "view", "label": "Open card", "url": card})
    if event == "ship":
        link = webconf.action_url("ship", ticket["id"])
        if link:
            out.append({"action": "view", "label": "Ship", "url": link})
    elif event == "proposed":
        link = webconf.action_url("approve", ticket["id"])
        if link:
            out.append({"action": "view", "label": "Approve", "url": link})
    return out


def _message(ticket: dict, event: str, cfg: dict, detail: str | None = None) -> dict:
    if event == "ship":
        title = f"{ticket['id']} ready to ship"
        text = f"{ticket.get('risk', 'unknown')} risk · {_short(ticket['title'])}"
    elif event == "oncall":   # `detail` is the on-call ticket's id
        title = f"{ticket['id']} failed, on-call {_short(detail or '?')} is on it"
        text = f"{ticket['type']} · {ticket['status']} · {_short(ticket['title'])}"
        detail = None
    elif event == "ship_list":   # `detail` is the whole message, e.g. "3 ready to ship, list ready"
        title = _short(detail or "ship list ready")
        text = "run `ship --list`"
        detail = None
    elif event == "dialog":   # `detail` is "label\x1ftab\x1fsanitized pane tail" (see review_watch)
        label, tab, tail = ((detail or "").split("\x1f", 2) + ["", ""])[:3]
        title = f"{_HEADLINE[event]}: {ticket['id']} ({_short(label, 30)})"
        text = f"tab {_short(tab, 40)}"
        if tail.strip():
            full = f"{text} · {_short(tail, 300)}"
            # Pane text is agent-controlled and this goes to a phone: never let a secret out.
            text = full if not tk.check_secrets("notification", full) else text
        detail = None
    elif event == "leftovers":   # `detail` is the supervisor's one-line reason: model text built from agent text
        title = f"{ticket['id']} ready with leftovers, see notes"
        text = f"{ticket['type']} · {ticket['status']} · {_short(ticket['title'])}"
        if detail:
            full = f"{text} · {_short(detail, 200)}"
            text = full if not tk.check_secrets("notification", full) else text   # it goes to a phone
        detail = None
    elif event == "needs_human":
        title = f"{ticket['id']} needs a human, see notes"
        text = f"{ticket['type']} · {ticket['status']} · {_short(ticket['title'])}"
    else:
        title = f"{_HEADLINE[event]}: {ticket['id']}"
        text = f"{ticket['type']} · {ticket['status']} · {_short(ticket['title'])}"
    if detail:
        text += f" · {_short(detail, 300 if event in RESULT_EVENTS else TITLE_MAX)}"
    if event in FAILURE_EVENTS:
        extra = _failure_lines(ticket)
        if extra:
            body = "\n".join([text, *extra])
            # Never let a pasted secret out in a phone notification.
            text = body if not tk.check_secrets("notification", body) else text
    message = {
        "topic": cfg["topic"],
        "title": title,
        "message": text[:BODY_MAX],
        "priority": PRIORITY_NUMBERS[cfg["priorities"][event]],
        "tags": _TAGS[event],
    }
    actions = _actions(ticket, event)
    if actions:
        if actions[0]["label"] == "Open card":
            message["click"] = actions[0]["url"]   # tapping the notification opens the card
        message["actions"] = actions[:3]
    return message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward the token to another address


def send(ticket: dict, event: str, detail: str | None = None) -> bool:
    """Send one notification. Returns False if it was filtered out by config;
    raises NotConfigured or NotifyError."""
    if event not in EVENTS:
        raise NotifyError(f"unknown event: {event}")
    cfg = load_config()
    if event == "proposed" and not cfg["follow_ups"]:
        return False
    return _post(cfg, _message(ticket, event, cfg, detail), cfg["priorities"][event])


def send_alert(event: str, title: str, text: str) -> bool:
    """Send a notification about the fleet itself, not a ticket. Raises
    NotConfigured or NotifyError."""
    if event not in EVENTS:
        raise NotifyError(f"unknown event: {event}")
    try:
        cfg = load_config()
        message = {"topic": cfg["topic"], "title": _short(title, 120), "message": text[:BODY_MAX],
                   "priority": PRIORITY_NUMBERS[cfg["priorities"][event]], "tags": _TAGS[event]}
        sent = _post(cfg, message, cfg["priorities"][event])
    except NotConfigured:
        raise   # notifications are off: no line, as for a ticket ping
    except NotifyError as exc:
        log_ping("PING_FAILED", "fleet", event, str(exc))
        raise
    except Exception as exc:  # e.g. a malformed priorities entry: still one line, then the caller sees it
        log_ping("PING_FAILED", "fleet", event, type(exc).__name__)
        raise
    log_ping("PING_SENT", "fleet", event)
    return sent


def _post(cfg: dict, message: dict, priority: str) -> bool:
    headers = {"Content-Type": "application/json"}
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    # ntfy reads the priority from the JSON body; the header says the same.
    headers["Priority"] = priority
    body = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            resp.read(1024)
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"ntfy answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise NotifyError(f"ntfy unreachable ({type(reason).__name__})") from None
    return True


def log_ping(kind: str, ticket_id: str, event: str, reason: str = "") -> None:
    """One dispatch.log line for a ping: PING_SENT or PING_FAILED, with the reason. Ids, event
    names and reasons only. Best effort: a log that cannot be written never changes the ping."""
    try:
        from . import dispatch as dp
        text = f"{event} {ticket_id}" + (f": {_short(reason, 160)}" if reason else "")
        dp.log(kind, text)
    except Exception:
        pass


def notify_quietly(ticket: dict, event: str | None, warn=None, detail: str | None = None,
                   log_unconfigured: bool = False) -> bool:
    """Send a notification for a real transition. Never raises: the ticket is
    already saved. `warn` receives a one-line message (default: stderr).
    Returns False only when a configured server could not be reached, so a
    caller that remembers what it has sent can try again later. Every attempt
    leaves a PING_SENT or PING_FAILED line in dispatch.log (event and ticket id,
    plus the reason for a failure). Notifications that are off, and events that
    notify.json filters out, write no line, except when `log_unconfigured` is set
    for a ping somebody is waiting on (the prep ship list), which records that
    it could not be sent."""
    if event is None:
        return True
    if warn is None:
        def warn(message):
            print(f"ticket: {message}", file=sys.stderr)
    tid = ticket.get("id", "ticket")
    try:
        sent = send(ticket, event, detail)
    except NotConfigured:
        if log_unconfigured:
            log_ping("PING_FAILED", tid, event, "notifications are off (no notify.json)")
        return True   # nothing to retry
    except NotifyError as exc:
        log_ping("PING_FAILED", tid, event, str(exc))
        warn(f"warning: {tid} saved, but the phone notification was not sent ({exc})")
        return False
    except Exception as exc:  # best effort: nothing here may fail a command
        log_ping("PING_FAILED", tid, event, type(exc).__name__)
        warn(f"warning: {tid} saved, but the phone notification was not sent ({type(exc).__name__})")
        return False
    if sent:
        log_ping("PING_SENT", tid, event)
    return True   # not sent because notify.json filters the event: nothing to log or retry
