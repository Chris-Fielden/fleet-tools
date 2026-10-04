"""web.json: where the fleet web endpoint (approve / ship from a browser) listens
and the address other devices use to reach it. Off unless the file exists in the
fleet home. Holds no secret.

web.json: {"listen": "100.64.0.5:8787", "url": "http://loic.tailnet.ts.net:8787"}

`listen` is the tailnet address of this machine and a port. Wildcard addresses
(0.0.0.0, ::) are refused, so the endpoint cannot be reached off the tailnet by
accident. `url` is what links on Kaneo cards and phone notifications point at.
Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
from pathlib import Path

from . import ticket as tk

ACTIONS = ("approve", "ship")


class WebConfigError(Exception):
    """A plain one-line problem."""


class NotConfigured(Exception):
    """There is no web.json, so the endpoint and its links are off."""


def config_path() -> Path:
    return tk.fleet_home() / "web.json"


def load_config() -> dict:
    path = config_path()
    tk.refuse_real_store_in_tests(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotConfigured() from None
    except (OSError, UnicodeDecodeError) as exc:
        raise WebConfigError(f"cannot read {path}: {getattr(exc, 'strerror', None) or 'error'}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise WebConfigError(f"{path} is not readable JSON") from None
    if not isinstance(data, dict):
        raise WebConfigError(f"{path} must contain a JSON object")
    listen, url = data.get("listen"), data.get("url")
    if not isinstance(listen, str) or listen.count(":") != 1:
        raise WebConfigError(f"'listen' in {path} must look like 100.64.0.5:8787")
    host, _, port = listen.partition(":")
    try:
        addr = ipaddress.ip_address(host)
        port_number = int(port)
    except ValueError:
        raise WebConfigError(f"'listen' in {path} must look like 100.64.0.5:8787 (an IP address, then a port)") from None
    if addr.is_unspecified:
        raise WebConfigError(f"'listen' in {path} must be one address (the tailnet one), not {host}")
    if not 1 <= port_number <= 65535:
        raise WebConfigError(f"the port in 'listen' in {path} must be 1 to 65535")
    try:
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise WebConfigError(f"'url' in {path} must start with http:// or https://")
    return {"host": host, "port": port_number, "url": url.rstrip("/")}


def action_url(action: str, ticket_id: str) -> str | None:
    """The confirmation page for `action` on a ticket, or None when the endpoint
    is not configured. Opening it changes nothing: the page asks first."""
    if action not in ACTIONS:
        raise ValueError(action)
    try:
        return f"{load_config()['url']}/{action}/{ticket_id}"
    except (NotConfigured, WebConfigError):
        return None
