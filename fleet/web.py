"""The fleet web endpoint: approve a proposed ticket, or ship one in review, from a
browser on any of your tailnet devices EXCEPT the machine the agents run on.

Every request is checked FIRST, whatever it asks for: `tailscale whois` on the
connecting address must name a tailnet node, and that node must not be this
machine. Anything else (whois fails, answers nothing usable, names this machine,
or the address is loopback) is refused with 403 before the ticket store is read.
That check is the real gate; the confirmation page only prevents mistakes. The
address is the socket's, never a header. Run it bound to the tailnet address
(see webconf.py), not behind `tailscale serve`: a proxy on this machine would
make every request look like it came from here, and so be refused.

GET /approve/<ID> and GET /ship/<ID> show a confirmation page and change
nothing. The page's POST carries a token made for that action, ticket and
commit; a high-risk ticket also needs its ID typed. Ship runs `bin/ship ID
--non-interactive COMMIT` in the background (its own session, so a closed
browser does not stop it), logging to $FLEET_HOME/web/. That run applies the same
gates as the ship command and sends one phone ping with the result.

Programs it calls can be swapped for tests: $FLEET_TAILSCALE. Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dispatch as dp
from . import kaneo
from . import notify
from . import risk as rk
from . import ship_cli
from . import ticket as tk
from . import webconf

ROOT = Path(__file__).resolve().parent.parent
PATH_RE = re.compile(r"/(approve|ship)/(" + tk.ID_RE.pattern + r")", re.IGNORECASE)
MAX_FORM = 4096
WHOIS_TIMEOUT = 10


# --- who is asking -------------------------------------------------------------

def _tailscale(*args: str) -> dict | None:
    """Run tailscale and return its JSON, or None on any doubt."""
    try:
        words = shlex.split(os.environ.get("FLEET_TAILSCALE") or "tailscale")
        proc = subprocess.run([*words, *args], capture_output=True, text=True, timeout=WHOIS_TIMEOUT)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return data if isinstance(data, dict) else None


def _identities(node) -> set[str]:
    """Every name this node goes by (ids, host and DNS names), lower-cased."""
    if not isinstance(node, dict):
        return set()
    found = set()
    for key in ("ID", "StableID", "Name", "ComputedName", "HostName", "DNSName"):
        value = node.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            found.add(f"{str(value).strip().rstrip('.').lower()}")
    return found


def check_device(address: str) -> tuple[bool, str]:
    """(allowed, why) for a connection from `address`. Fails closed."""
    who = _tailscale("whois", "--json", address)
    node = who.get("Node") if who else None
    peer = _identities(node)
    if not peer:
        return False, "the address is not a tailnet device"
    status = _tailscale("status", "--json")
    mine = _identities(status.get("Self") if status else None)
    if not mine:
        return False, "cannot tell which node this machine is"
    if peer & mine:
        return False, "requests from the machine the agents run on are refused"
    return True, "ok"


# --- what a ticket looks like to the page ----------------------------------------

class Refused(Exception):
    """A plain reason, shown on the page."""


def _repo_of(t: dict) -> tuple[Path, str, Path]:
    try:
        projects = dp.load_settings()["projects_dir"]
    except dp.SettingsError as exc:
        raise Refused(str(exc)) from None
    repo = projects / (t.get("repo") or "")
    branch = t.get("branch") or ""
    if (not t.get("repo") or repo.resolve().parent != projects.resolve() or not repo.is_dir()
            or not branch.startswith("task/") or branch == "task/"):
        raise Refused(f"{t['id']} has no repo folder and task branch to work with")
    return repo, branch, repo / ".worktrees" / branch.removeprefix("task/")


def facts(action: str, t: dict) -> dict:
    """What the confirmation page shows, and what the POST rechecks."""
    want = {"approve": "proposed", "ship": "review"}[action]
    if t["status"] != want:
        raise Refused(f"{t['id']} is {t['status']}; only a {want} ticket can be {action}d here")
    base = {"id": t["id"], "title": t["title"], "verdicts": tk.verdict_line(t["verdict"]),
            "commit": t.get("commit") or "-", "problems": [], "confirm": t["id"]}
    if action == "approve":
        return {**base, "risk": rk.stored_level(t)}
    repo, branch, wt = _repo_of(t)
    if not wt.is_dir():
        raise Refused(f"no worktree at {wt}; nothing can be shipped")
    try:
        risk = ship_cli._field(repo, wt, branch, "risk")
        ok = ship_cli._field(repo, wt, branch, "ok")
        tip = rk._git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").strip()
    except (ship_cli._GateBroken, rk.RiskError) as exc:
        raise Refused(str(exc)) from None
    problems = [] if ok == "yes" else [ship_cli._blocked_lines(repo, wt, branch)]
    saved = rk.latest_reviews(wt / ".reviews", tip)   # the reviews the merge gate itself reads
    verdicts = ", ".join(f"{i['reviewer']}: {i['verdict'] or 'no verdict'}" for _, i in sorted(saved.items()))
    return {**base, "risk": risk, "commit": tip, "problems": problems,
            "verdicts": verdicts or "no review of this commit"}


# --- tokens ------------------------------------------------------------------------

class Tokens:
    """Per-run secret: a token only the page this server served can carry."""

    def __init__(self):
        self._key = os.urandom(32)

    def make(self, action: str, ticket_id: str, commit: str) -> str:
        return hmac.new(self._key, f"{action}|{ticket_id}|{commit}".encode(), hashlib.sha256).hexdigest()

    def ok(self, token: str, action: str, ticket_id: str, commit: str) -> bool:
        return hmac.compare_digest(token, self.make(action, ticket_id, commit))


# --- pages -------------------------------------------------------------------------

STYLE = ("body{font:18px system-ui,sans-serif;margin:0;padding:16px;max-width:34em;margin:auto}"
         "dt{color:#666;margin-top:.8em}dd{margin:0;word-break:break-all}"
         "button{font-size:1.2em;padding:.7em 1.4em;margin-top:1.2em;width:100%}"
         "input[type=text]{font-size:1.1em;padding:.5em;width:100%;box-sizing:border-box}"
         ".bad{color:#b00}.hi{color:#b00;font-weight:bold}")


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><h1>{html.escape(title)}</h1>{body}</body></html>").encode()


def confirm_page(action: str, f: dict, token: str, error: str = "") -> bytes:
    e = html.escape
    rows = [("Ticket", f["id"]), ("Title", f["title"]), ("Risk", f["risk"].upper()),
            ("Reviewed commit" if action == "ship" else "Commit", f["commit"]),
            ("Verdicts", f["verdicts"])]
    body = "<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in rows) + "</dl>"
    if error:
        body += f"<p class=bad>{e(error)}</p>"
    for problem in f["problems"]:
        body += f"<p class=bad>Not allowed: {e(problem)}</p>"
    typed = ""
    if f["risk"] == "high" and not f["problems"]:
        typed = (f"<p class=hi>HIGH RISK. Type the ticket ID ({e(f['id'])}) to confirm.</p>"
                 "<input type=text name=typed autocomplete=off autocapitalize=characters>")
    label = "Approve" if action == "approve" else "Ship (merge to main)"
    off = " disabled" if f["problems"] else ""   # the server refuses it too
    body += (f"<form method=post><input type=hidden name=token value={e(token)}>"
             f"<input type=hidden name=commit value={e(f['commit'])}>{typed}"
             f"<button type=submit{off}>{e(label)}: Confirm</button></form>")
    return _page(f"{'Approve' if action == 'approve' else 'Ship'} {f['id']}", body)


# --- the server --------------------------------------------------------------------

def web_dir() -> Path:
    return tk.fleet_home() / "web"


class Endpoint:
    """State shared by every request: tokens, and the ships still running."""

    def __init__(self):
        self.tokens = Tokens()
        self.running: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def start_ship(self, t: dict, commit: str) -> Path:
        """Start `ship` in its own session. Raises Refused if one is already running."""
        with self.lock:
            old = self.running.get(t["id"])
            if old is not None and old.poll() is None:
                raise Refused(f"a ship of {t['id']} is already running")
            logs = web_dir()
            tk.refuse_real_store_in_tests(logs)
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log = logs / f"ship-{t['id']}-{stamp}.log"
            ship = ROOT / "bin" / "ship"
            try:
                with open(log, "ab") as out:
                    self.running[t["id"]] = subprocess.Popen(
                        [sys.executable, str(ship), t["id"], "--non-interactive", commit],
                        stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
            except OSError as exc:
                raise Refused(f"could not start ship: {exc.strerror or type(exc).__name__}") from None
            return log


def _form(raw: bytes) -> dict:
    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def make_handler(endpoint: Endpoint, log=lambda m: None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fleet-web"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # No other site may frame a page (it would hide the Confirm button under its own).
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                             "form-action 'self'; frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _gate(self) -> bool:
            """THE device check, before anything else is looked at."""
            allowed, why = check_device(self.client_address[0])
            if not allowed:
                log(f"refused {self.command} from {self.client_address[0]}: {why}")
                self._send(403, b"Refused.\n", "text/plain; charset=utf-8")
            return allowed

        def _route(self):
            m = PATH_RE.fullmatch(self.path.split("?", 1)[0])
            if not m:
                self._send(404, b"Not found.\n", "text/plain; charset=utf-8")
                return None
            return m.group(1), m.group(2)

        def _ticket(self, action: str, ticket_id: str):
            try:
                t = tk.load_ticket(ticket_id)
                return t, facts(action, t)
            except (tk.TicketError, tk.TicketInvalid) as exc:
                self._send(404, _page("No such ticket", f"<p>{html.escape(str(exc))}</p>"))
            except Refused as exc:
                self._send(409, _page("Refused", f"<p class=bad>{html.escape(str(exc))}</p>"))
            return None

        def do_GET(self):
            if not self._gate():
                return
            route = self._route()
            if route is None:
                return
            action, ticket_id = route
            found = self._ticket(action, ticket_id)
            if found is None:
                return
            t, f = found
            self._send(200, confirm_page(action, f, endpoint.tokens.make(action, f["id"], f["commit"])))

        def do_POST(self):
            if not self._gate():
                return
            route = self._route()
            if route is None:
                return
            action, ticket_id = route
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_FORM:
                self._send(400, b"Bad request.\n", "text/plain; charset=utf-8")
                return
            form = _form(self.rfile.read(length))
            found = self._ticket(action, ticket_id)
            if found is None:
                return
            t, f = found
            token = endpoint.tokens.make(action, f["id"], f["commit"])
            # The page was made for one commit; a branch that moved since is refused.
            if form.get("commit") != f["commit"] or not endpoint.tokens.ok(form.get("token", ""), action,
                                                                          f["id"], form.get("commit", "")):
                self._send(409, _page("Refused", "<p class=bad>This page is out of date (the ticket or its "
                                      "commit changed). Open the link again.</p>"))
                return
            if f["problems"]:
                self._notify_refusal(t, f["problems"][0], action)
                self._send(409, confirm_page(action, f, token))
                return
            if f["risk"] == "high" and form.get("typed", "").strip().lower() != f["id"].lower():
                self._send(400, confirm_page(action, f, token, "Type the ticket ID exactly to confirm."))
                return
            try:
                if action == "approve":
                    tk.approve_ticket(f["id"])
                    kaneo.mirror_quietly(tk.load_ticket(f["id"]), lambda m: log(m))
                    log(f"approved {f['id']} from {self.client_address[0]}")
                    self._send(200, _page("Approved", f"<p>{html.escape(f['id'])} is approved and open.</p>"))
                else:
                    path = endpoint.start_ship(t, f["commit"])
                    log(f"ship of {f['id']} at {f['commit'][:8]} started from {self.client_address[0]}, log {path}")
                    self._send(202, _page("Shipping", f"<p>{html.escape(f['id'])} is shipping in the background. "
                                          "You can close this page: a phone ping reports the result.</p>"
                                          f"<p>Log: {html.escape(path.name)}</p>"))
            except (Refused, tk.TicketError, tk.TicketInvalid) as exc:
                self._notify_refusal(t, str(exc), action)
                self._send(409, _page("Refused", f"<p class=bad>{html.escape(str(exc))}</p>"))

        def _notify_refusal(self, t: dict, reason: str, action: str) -> None:
            if action == "ship":
                notify.notify_quietly(t, "ship_failed", lambda m: log(m), detail=reason)

    return Handler


def serve(cfg: dict | None = None, log=lambda m: None) -> ThreadingHTTPServer:
    """A bound (not yet serving) server. Call serve_forever() on it."""
    cfg = cfg or webconf.load_config()
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(Endpoint(), log))
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    who = tk.current_creator()
    if who != "human":
        print(f"fleet-web: refusing to run inside an agent (running as {who})", file=sys.stderr)
        return 1
    try:
        cfg = webconf.load_config()
    except webconf.NotConfigured:
        print(f"fleet-web: no web.json in {tk.fleet_home()}; see fleet/webconf.py", file=sys.stderr)
        return 1
    except webconf.WebConfigError as exc:
        print(f"fleet-web: {exc}", file=sys.stderr)
        return 1

    def log(message: str) -> None:
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)

    try:
        server = serve(cfg, log)
    except OSError as exc:
        print(f"fleet-web: cannot listen on {cfg['host']}:{cfg['port']} ({exc.strerror})", file=sys.stderr)
        return 1
    log(f"listening on {cfg['host']}:{cfg['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
