import http.client
import re
import threading
from datetime import datetime, timezone

import pytest

from fleet import metrics, status_server
from fleet import ticket as tk

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
SECRET_TITLE = "Rotate the db password in /home/x/.env"


def _samples(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


@pytest.fixture
def fake_tickets(monkeypatch):
    tickets = [
        {"id": "BD-1", "status": "claimed", "title": SECRET_TITLE, "review_commits": ["a" * 40]},
        {"id": "BD-2", "status": "review", "title": "t", "review_commits": ["a" * 40, "b" * 40]},
        {"id": "BD-3", "status": "open", "title": "t"},
        {"id": "BD-4", "status": "done", "title": "t", "review_commits": ["c" * 40]},
        {"id": "BD-5", "status": "failed", "title": "t"},
    ]
    monkeypatch.setattr(tk, "list_tickets", lambda: tickets)


def test_empty_store_still_lists_every_status():
    s = _samples(metrics.render(NOW))
    for status in tk.STATUSES:
        assert s[f'fleet_tickets{{status="{status}"}}'] == 0
    assert s["fleet_agents_running"] == 0 and s["fleet_ready_to_ship"] == 0
    assert s["fleet_dispatch_up"] == 0 and s["fleet_launches_today"] == 0
    assert s["fleet_launches_max_per_day"] > 0
    assert not any(k.startswith("fleet_review_rounds{") for k in s)


def test_counts_and_active_only_review_rounds(fake_tickets):
    text = metrics.render(NOW)
    s = _samples(text)
    assert s['fleet_tickets{status="claimed"}'] == 1 and s['fleet_tickets{status="done"}'] == 1
    assert s["fleet_agents_running"] == 1 and s["fleet_ready_to_ship"] == 1
    rounds = {k: v for k, v in s.items() if k.startswith("fleet_review_rounds{")}
    assert rounds == {'fleet_review_rounds{ticket="BD-1"}': 1, 'fleet_review_rounds{ticket="BD-2"}': 2,
                      'fleet_review_rounds{ticket="BD-3"}': 0}
    assert SECRET_TITLE not in text and "password" not in text


def test_launch_counter_unreadable_leaves_the_sample_out(monkeypatch):
    def boom(day):
        raise OSError("nope")
    monkeypatch.setattr(metrics.dp, "launches_today", boom)
    text = metrics.render(NOW)
    assert "fleet_launches_today" not in text and "fleet_launches_max_per_day" in text


def test_exposition_format(fake_tickets):
    text = metrics.render(NOW)
    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith("# HELP ") or line.startswith("# TYPE ") or re.fullmatch(
            r'[a-z_]+(\{[a-z]+="[^"\\]*"\})? \d+', line), line


def _get(srv, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.getheader("Content-Type"), r.read()


def _serve(**kw):
    srv = status_server.make_server("127.0.0.1", 0, build=lambda: {}, cache_seconds=0, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_metrics_endpoint_is_fresh_and_tailnet_only(fake_tickets):
    srv = _serve()
    try:
        code, kind, body = _get(srv, "/metrics")
        assert code == 200 and kind.startswith("text/plain; version=0.0.4")
        assert b'fleet_tickets{status="open"} 1' in body
        assert _get(srv, "/metrics", {"X-Forwarded-For": "198.51.100.7"})[0] == 403
        assert _get(srv, "/metrics", {"Tailscale-Funnel-Request": "?1"})[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_metrics_error_hides_the_cause():
    def boom():
        raise RuntimeError("secret detail")
    srv = _serve(render_metrics=boom)
    try:
        code, _, body = _get(srv, "/metrics")
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 500 and b"secret" not in body
