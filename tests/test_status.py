import http.client
import importlib.util
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT
from fleet import planusage, status, status_server
from fleet import ticket as tk

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
PAYLOAD = {  # a Claude Code status line payload (extra fields must not be kept)
    "session_id": "abc", "cwd": "/secret/path", "model": {"id": "x"},
    "rate_limits": {
        "five_hour": {"used_percentage": 42.5, "resets_at": int((NOW + timedelta(hours=2)).timestamp())},
        "seven_day": {"used_percentage": 17, "resets_at": int((NOW + timedelta(days=3)).timestamp())}},
}
MAC = ROOT / "contrib" / "mac"


@pytest.fixture(autouse=True)
def _no_real_session_logs(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def _mac(name, filename=None):
    sys.path.insert(0, str(MAC))
    spec = importlib.util.spec_from_file_location(name, MAC / (filename or f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- plan usage from the status line -------------------------------------------

def test_record_keeps_only_the_rate_limits():
    assert planusage.record(PAYLOAD, NOW)
    saved = json.loads(planusage.path().read_text())
    assert set(saved) == {"saved_at", "five_hour", "weekly"}
    assert saved["five_hour"]["used_percent"] == 42.5
    assert "secret" not in planusage.path().read_text()


def test_payload_without_limits_leaves_the_old_figure():
    planusage.record(PAYLOAD, NOW)
    assert not planusage.record({"session_id": "x"}, NOW + timedelta(minutes=1))
    assert not planusage.record("junk", NOW)
    assert planusage.load(NOW + timedelta(minutes=2))["five_hour"]["used_percent"] == 42.5


def test_load_reports_age_and_marks_stale_after_reset():
    planusage.record(PAYLOAD, NOW)
    got = planusage.load(NOW + timedelta(minutes=10))
    assert got["available"] and got["age_seconds"] == 600
    assert not got["five_hour"]["stale"]
    later = planusage.load(NOW + timedelta(hours=2, minutes=1))
    assert later["five_hour"]["stale"] and not later["weekly"]["stale"]
    assert not planusage.load(NOW + timedelta(hours=6))["available"]   # too old to call real


def test_load_without_a_file():
    assert planusage.load(NOW)["available"] is False


def test_statusline_command():
    exe = [sys.executable, str(ROOT / "bin" / "fleet-statusline")]
    payload = {"rate_limits": {"five_hour": {"used_percentage": 9, "resets_at": 4102444800}}}
    proc = subprocess.run(exe, input=json.dumps(payload), capture_output=True, text=True)
    assert proc.returncode == 0 and "5h 9%" in proc.stdout
    assert subprocess.run(exe, input="not json", capture_output=True, text=True).returncode == 0


def test_settings_wire_the_status_line():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    assert "fleet-statusline" in settings["statusLine"]["command"]


# --- the snapshot -----------------------------------------------------------------

def test_snapshot_shape_estimate_then_real():
    s = status.snapshot(NOW)
    assert set(s) == {"generated_at", "tokens", "plan_usage", "governor", "agents_running", "ready_to_ship",
                      "launches", "dispatch_alive", "herdr", "kaneo_url"}
    assert set(s["tokens"]["claude"]) == {"five_hour", "today", "week"}
    assert s["plan_usage"]["claude"]["estimate"] is True
    assert s["agents_running"] == [] and s["ready_to_ship"] == []
    assert s["launches"]["left_today"] == s["launches"]["max_per_day"]
    assert s["dispatch_alive"] is False
    planusage.record(PAYLOAD, NOW - timedelta(minutes=5))
    c = status.snapshot(NOW)["plan_usage"]["claude"]
    assert c["estimate"] is False and c["five_hour"]["used_percent"] == 42.5
    assert c["five_hour"]["age_seconds"] == 300 and c["five_hour"]["resets_at"]


def test_snapshot_cli_json():
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "fleet"), "status", "--json"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout)["plan_usage"]


def test_snapshot_lists_agents_and_ready_tickets(monkeypatch):
    monkeypatch.setattr(tk, "list_tickets", lambda: [
        {"id": "BD-1", "status": "claimed", "started_at": "2026-10-04T11:30:00Z", "updated_at": "x"},
        {"id": "BD-2", "status": "review", "updated_at": "2026-10-04T11:00:00Z"},
        {"id": "BD-3", "status": "open", "updated_at": "2026-10-04T11:00:00Z"}])
    s = status.snapshot(NOW)
    assert s["agents_running"] == [{"id": "BD-1", "minutes": 30}]
    assert s["ready_to_ship"] == ["BD-2"]


# --- the endpoint -----------------------------------------------------------------

@pytest.mark.parametrize("peer,headers,ok", [
    ("100.101.102.103", {}, True),
    ("fd7a:115c:a1e0::1", {}, True),
    ("127.0.0.1", {}, True),
    ("127.0.0.1", {"X-Forwarded-For": "100.64.0.5"}, True),
    ("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, False),
    ("127.0.0.1", {"X-Forwarded-For": "100.64.0.5, 203.0.113.9"}, False),
    ("127.0.0.1", {"Tailscale-Funnel-Request": "?1"}, False),
    ("203.0.113.9", {}, False),
    ("192.168.1.20", {}, False),
    ("not-an-ip", {}, False),
])
def test_allowed(peer, headers, ok):
    assert status_server.allowed(peer, headers) is ok


def _get(server, path="/status.json", headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(method, path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read()


@pytest.fixture
def server():
    srv = status_server.make_server("127.0.0.1", 0, build=lambda: {"hello": 1}, cache_seconds=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_endpoint_serves_json_to_tailnet_proxy(server):
    code, body = _get(server, headers={"X-Forwarded-For": "100.64.0.5"})
    assert code == 200 and json.loads(body) == {"hello": 1}


def test_endpoint_refuses_non_tailnet_and_funnel(server):
    assert _get(server, headers={"X-Forwarded-For": "198.51.100.7"})[0] == 403
    assert _get(server, headers={"Tailscale-Funnel-Request": "?1"})[0] == 403
    assert _get(server, "/other")[0] == 404
    assert _get(server, method="POST")[0] == 501


def test_endpoint_refuses_to_listen_publicly():
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(ValueError):
            status_server.make_server(host, 0)


def test_endpoint_error_hides_the_cause():
    def boom():
        raise RuntimeError("secret detail")
    srv = status_server.make_server("127.0.0.1", 0, build=boom, cache_seconds=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body = _get(srv)
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 500 and b"secret" not in body


# --- the Mac widgets ----------------------------------------------------------------

def _snap(**over):
    s = status.snapshot(NOW)
    s.update(over)
    return s


def test_widget_summary_estimate_and_real():
    fs = _mac("fleet_summary")
    s = _snap(agents_running=[{"id": "BD-1", "minutes": 3}], ready_to_ship=["BD-2", "BD-3"])
    assert fs.summarize(s, NOW).endswith("· 1 running · 2 to ship")
    assert "(est)" in fs.summarize(s, NOW)
    planusage.record(PAYLOAD, NOW)
    assert fs.summarize(status.snapshot(NOW), NOW).startswith("5h 42.5% ·")


def test_widget_offline_states():
    fs = _mac("fleet_summary")
    assert fs.summarize(None, NOW) == "fleet offline"
    assert fs.summarize({"nonsense": 1}, NOW) == "fleet offline"
    assert fs.summarize(_snap(), NOW + timedelta(minutes=10)) == "fleet offline"   # too old to show
    assert fs.fetch("http://127.0.0.1:9/status.json", timeout=1) is None           # nothing listening


def test_widget_fetch_reads_endpoint(server):
    fs = _mac("fleet_summary")
    assert fs.fetch(f"http://127.0.0.1:{server.server_address[1]}/status.json") == {"hello": 1}


def test_swiftbar_plugin_offline_and_board_link():
    bar = _mac("swiftbar_plugin", "fleet.30s.py")
    assert bar.render(None) == ["fleet offline | color=red"]
    lines = bar.render(_snap(kaneo_url="https://kaneo.example/board"), NOW)
    assert lines[0].endswith(" | href=https://kaneo.example/board") and lines[1] == "---"
    assert lines[0].count("|") == 1 and all(l.count("|") == 1 for l in lines[2:])
    assert all(l.endswith(" | href=https://kaneo.example/board") for l in lines[2:])


def test_iterm_component_text_and_click_target():
    it = _mac("fleet_iterm")
    assert it.component_text(None) == "fleet offline" and it._board["url"] is None
    assert "running" in it.component_text(_snap(kaneo_url="https://kaneo.example/b"), NOW)
    assert it._board["url"] == "https://kaneo.example/b"
