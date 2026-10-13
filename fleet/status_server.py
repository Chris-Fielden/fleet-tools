"""`fleet status-serve`: the read-only, tailnet-only status endpoint.

Same pattern as Kaneo and ntfy: this listens on loopback only, and
`tailscale serve` puts it on the tailnet (docs/mac-widgets.md). It still
checks every request itself and answers 403 unless it came from a tailnet
address, or from the local `tailscale serve` proxy on behalf of one. Requests
that arrived through Tailscale Funnel (the public internet) are refused.

GET /status.json (and /) is the snapshot from status.snapshot(). It is cached
for a few seconds, because building it reads the session logs.
GET /metrics is the Prometheus text from metrics.render(), built fresh on each
scrape (it only reads tickets and counters).
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import metrics, status

DEFAULT_PORT = 8788
CACHE_SECONDS = 10.0
TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _ip(text: str):
    try:
        ip = ipaddress.ip_address(text.strip().split("%")[0])
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def is_tailnet(text: str) -> bool:
    ip = _ip(text)
    return ip is not None and any(ip in net for net in TAILNET_NETS)


def is_loopback(text: str) -> bool:
    ip = _ip(text)
    return ip is not None and ip.is_loopback


def allowed(peer: str, headers) -> bool:
    """Whether a request from `peer` (with these headers) may be answered."""
    if headers.get("Tailscale-Funnel-Request"):
        return False
    if is_tailnet(peer):
        return True
    if not is_loopback(peer):
        return False
    forwarded = headers.get("X-Forwarded-For")
    # A request straight to loopback is local; one relayed by tailscale serve
    # names the tailnet client it came from, and that must be a tailnet address.
    return forwarded is None or all(is_tailnet(a) for a in forwarded.split(","))


class _Cache:
    def __init__(self, build, seconds: float):
        self.build, self.seconds = build, seconds
        self.lock = threading.Lock()
        self.at = float("-inf")
        self.body = b""

    def get(self) -> bytes:
        with self.lock:
            if time.monotonic() - self.at >= self.seconds:
                self.body = json.dumps(self.build(), indent=2).encode()
                self.at = time.monotonic()
            return self.body


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, build=status.snapshot,
                cache_seconds: float = CACHE_SECONDS, render_metrics=metrics.render) -> ThreadingHTTPServer:
    ip = _ip(host)
    if ip is None or not (ip.is_loopback or is_tailnet(host)):
        raise ValueError(f"{host} is not a loopback or tailnet address; refusing to listen there")
    cache = _Cache(build, cache_seconds)

    class Handler(BaseHTTPRequestHandler):
        server_version = "fleet-status"

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            if not allowed(self.client_address[0], self.headers):
                return self._send(403, b'{"error": "tailnet only"}')
            path = self.path.split("?")[0]
            if path == "/metrics":
                try:
                    body = render_metrics().encode()
                except Exception:   # noqa: BLE001 - say nothing about the cause to the client
                    return self._send(500, b"metrics unavailable\n", "text/plain; charset=utf-8")
                return self._send(200, body, metrics.CONTENT_TYPE)
            if path not in ("/", "/status.json"):
                return self._send(404, b'{"error": "not found"}')
            try:
                body = cache.get()
            except Exception:   # noqa: BLE001 - say nothing about the cause to the client
                return self._send(500, b'{"error": "status unavailable"}')
            self._send(200, body)

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        daemon_threads = True

    return Server((host, port), Handler)


def serve(host: str, port: int) -> int:
    server = make_server(host, port)
    print(f"fleet status endpoint on http://{host}:{port}/status.json (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
