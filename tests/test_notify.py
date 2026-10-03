import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import new_args, run
from fleet import notify


class FakeNtfy:
    def __init__(self, status=200):
        self.status = status
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"auth": self.headers.get("Authorization"),
                                       "body": json.loads(self.rfile.read(n))})
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def write_config(url, mode=0o600, **extra):
    home = Path(os.environ["FLEET_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    path = home / "notify.json"
    path.write_text(json.dumps({"url": url, "topic": "fleet", "token": "tk_secret123", **extra}))
    path.chmod(mode)
    return path


@pytest.fixture
def ntfy():
    fake = FakeNtfy()
    write_config(fake.url)
    yield fake
    fake.stop()


def make(**over):
    r = run("new", *new_args(**over))
    assert r.returncode == 0, r.stderr
    return r


def make_review(**over):
    parent = make(branch="task/parent").stdout.split()[0]
    return make(type="review", branch="task/r", commit="a" * 40, parent=parent, **over)


def test_off_without_config():
    r = make_review()
    assert r.stderr == ""


def test_review_ticket_ready(ntfy):
    make_review(title="Check the thing", body="SECRET BODY text")
    assert len(ntfy.requests) == 1
    req = ntfy.requests[0]
    assert req["auth"] == "Bearer tk_secret123"
    assert req["body"]["topic"] == "fleet"
    text = json.dumps(req["body"])
    assert "Check the thing" in text and "review" in text and "open" in text
    assert "SECRET BODY" not in text


def test_plain_build_ticket_is_silent(ntfy):
    make()
    assert ntfy.requests == []


def test_urgent_created(ntfy):
    make(priority="urgent")
    assert len(ntfy.requests) == 1
    assert ntfy.requests[0]["body"]["priority"] == 5


def test_failed_close_notifies_but_done_does_not(ntfy):
    tid = make().stdout.split()[0]
    tid2 = make(branch="task/other").stdout.split()[0]
    assert run("close", tid, "--done").returncode == 0
    assert ntfy.requests == []
    assert run("close", tid2, "--failed").returncode == 0
    assert len(ntfy.requests) == 1
    assert "failed" in ntfy.requests[0]["body"]["message"]


def test_follow_ups_only_when_enabled(ntfy):
    make(type="investigate", branch="task/i")
    assert ntfy.requests == []
    write_config(ntfy.url, follow_ups=True)
    make(type="investigate", branch="task/j")
    assert len(ntfy.requests) == 1


def test_server_error_only_warns(ntfy):
    ntfy.status = 500
    r = make(priority="urgent")
    assert r.stdout.split()[0]
    assert "not sent" in r.stderr and "tk_secret123" not in r.stderr


def test_unreachable_only_warns():
    fake = FakeNtfy()
    url = fake.url
    fake.stop()
    write_config(url)
    r = make(priority="urgent")
    assert r.returncode == 0 and "not sent" in r.stderr


def test_loose_permissions_refused_and_warn():
    write_config("http://127.0.0.1:9", mode=0o644)
    r = make(priority="urgent")
    assert r.returncode == 0 and "chmod 600" in r.stderr


def test_redirect_not_followed():
    target = FakeNtfy()
    redirector = HTTPServer(("127.0.0.1", 0), type("R", (BaseHTTPRequestHandler,), {
        "do_POST": lambda s: (s.send_response(302), s.send_header("Location", target.url), s.end_headers()),
        "log_message": lambda s, *a: None}))
    threading.Thread(target=redirector.serve_forever, daemon=True).start()
    write_config(f"http://127.0.0.1:{redirector.server_port}")
    r = make(priority="urgent")
    redirector.shutdown(); redirector.server_close(); target.stop()
    assert r.returncode == 0 and target.requests == []


def test_title_is_shortened_and_cleaned():
    msg = notify._message({"id": "BD-1", "type": "build", "status": "open",
                           "title": "x" * 200 + "\n"}, "failed", "fleet")
    assert len(msg["message"]) < 100 and "\n" not in msg["message"]
