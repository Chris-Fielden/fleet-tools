"""Best-effort phone notifications through a self-hosted ntfy server.

Off unless notify.json exists in the fleet home. Only the ticket id, type,
status and a short title are ever sent: never the body or notes. A failure
only warns; it never fails the command that caused it. Standard library only.

notify.json: {"url": "http://100.x.y.z:2586", "topic": "fleet", "token": "tk_...",
              "follow_ups": false}
`token` is optional (sent as a bearer token); `follow_ups` turns on messages
for proposed tickets.
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

TIMEOUT = 4  # seconds per socket operation (not DNS); use an IP url for a firm limit
TITLE_MAX = 60
EVENTS = ("ready", "urgent", "failed", "proposed")
_HEADLINE = {"ready": "Review ready", "urgent": "Urgent ticket", "failed": "Ticket failed",
             "proposed": "Follow-up proposed"}
_PRIORITY = {"ready": 3, "urgent": 5, "failed": 4, "proposed": 2}
_TAGS = {"ready": ["mag"], "urgent": ["rotating_light"], "failed": ["x"], "proposed": ["bulb"]}


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
    return {"url": data["url"].rstrip("/"), "topic": data["topic"], "token": token,
            "follow_ups": data.get("follow_ups") is True}


def event_for_new(ticket: dict) -> str | None:
    """The event a just-created ticket stands for, if any."""
    if ticket.get("priority") == "urgent":
        return "urgent"
    if ticket["status"] == "open" and ticket["type"] == "review":
        return "ready"
    if ticket["status"] == "proposed":
        return "proposed"
    return None


def _short(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", text).strip()
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def _message(ticket: dict, event: str, topic: str) -> dict:
    return {
        "topic": topic,
        "title": f"{_HEADLINE[event]}: {ticket['id']}",
        "message": f"{ticket['type']} · {ticket['status']} · {_short(ticket['title'])}",
        "priority": _PRIORITY[event],
        "tags": _TAGS[event],
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward the token to another address


def send(ticket: dict, event: str) -> bool:
    """Send one notification. Returns False if it was filtered out by config;
    raises NotConfigured or NotifyError."""
    if event not in EVENTS:
        raise NotifyError(f"unknown event: {event}")
    cfg = load_config()
    if event == "proposed" and not cfg["follow_ups"]:
        return False
    headers = {"Content-Type": "application/json"}
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    body = json.dumps(_message(ticket, event, cfg["topic"])).encode("utf-8")
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


def notify_quietly(ticket: dict, event: str | None, warn=None) -> None:
    """Send a notification for a real transition. Never raises: the ticket is
    already saved. `warn` receives a one-line message (default: stderr)."""
    if event is None:
        return
    if warn is None:
        def warn(message):
            print(f"ticket: {message}", file=sys.stderr)
    try:
        send(ticket, event)
    except NotConfigured:
        pass
    except NotifyError as exc:
        warn(f"warning: {ticket.get('id', 'ticket')} saved, but the phone notification "
             f"was not sent ({exc})")
    except Exception as exc:  # best effort: nothing here may fail a command
        warn(f"warning: {ticket.get('id', 'ticket')} saved, but the phone notification "
             f"was not sent ({type(exc).__name__})")
