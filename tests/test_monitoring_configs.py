"""contrib/monitoring: file-level checks only (nothing is started)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "contrib" / "monitoring"

# fleet/metrics.py (BD-90) exposes exactly these, plus Prometheus's own `up`.
EXPORTED = {"fleet_tickets", "fleet_launches_today", "fleet_launches_max_per_day",
            "fleet_agents_running", "fleet_ready_to_ship", "fleet_dispatch_up",
            "fleet_review_rounds", "up"}
FUNCS = {"delta", "sum", "by", "count_over_time", "job", "kind", "status", "ticket", "instant"}


def text(rel):
    return (ROOT / rel).read_text()


def dashboards():
    return {p.name: json.loads(p.read_text()) for p in (ROOT / "grafana" / "dashboards").glob("*.json")}


def test_four_dashboards_with_unique_uids():
    d = dashboards()
    assert len(d) == 4
    assert len({x["uid"] for x in d.values()}) == 4


def test_dashboards_use_provisioned_datasources_and_exported_metrics():
    ds = text("grafana/provisioning/datasources/datasources.yaml")
    uids = set(re.findall(r"uid: (\S+)", ds))
    assert uids == {"fleet-prometheus", "fleet-loki"}
    for name, d in dashboards().items():
        for p in d["panels"]:
            assert p["datasource"]["uid"] in uids, name
            for t in p["targets"]:
                assert t["datasource"]["uid"] in uids, name
                if p["datasource"]["uid"] == "fleet-prometheus":
                    names = set(re.findall(r"[a-z_]+", re.sub(r'"[^"]*"|\{[^}]*\}|\[[^\]]*\]', "", t["expr"])))
                    assert names - FUNCS <= EXPORTED, (name, t["expr"])


def test_alerts_cover_the_four_cases_and_use_exported_metrics():
    a = text("grafana/provisioning/alerting/alerting.yaml")
    titles = re.findall(r"^        title: (.+)$", a, re.M)
    assert titles == ["Dispatcher down", "Launches at daily cap", "Failed tickets rising",
                      "Fleet exporter not scraped"]
    for expr in re.findall(r"expr: '([^']+)'", a):
        names = set(re.findall(r"[a-z_]+", re.sub(r'"[^"]*"|\{[^}]*\}|\[[^\]]*\]', "", expr)))
        assert names - FUNCS <= EXPORTED, expr


def test_contact_point_uses_a_bearer_token_from_the_environment():
    a = text("grafana/provisioning/alerting/alerting.yaml")
    assert "authorization_scheme: Bearer" in a
    assert re.search(r"authorization_credentials: \$NTFY_TOKEN\s*$", a, re.M)
    assert "payload:" in a and "template:" in a


def test_every_service_binds_loopback_and_nothing_is_published():
    compose = text("compose.yml")
    assert "ports:" not in compose
    assert compose.count("network_mode: host") == 4
    for needle in ("--web.listen-address=127.0.0.1:9090", "--server.http.listen-addr=127.0.0.1:12345",
                   "GF_SERVER_HTTP_ADDR: 127.0.0.1"):
        assert needle in compose
    assert "http_listen_address: 127.0.0.1" in text("loki/loki.yaml")
    assert "--storage.tsdb.retention.time=15d" in compose


def test_prometheus_scrapes_the_status_endpoint_and_alloy_not_promtail():
    assert "127.0.0.1:8788" in text("prometheus/prometheus.yml")
    assert "/metrics" in text("prometheus/prometheus.yml")
    assert "dispatch.log" in text("alloy/config.alloy")
    assert "promtail" not in text("compose.yml").lower()


def test_no_secret_values_and_no_funnel_instruction():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        body = p.read_text()
        assert not re.search(r"tk_[A-Za-z0-9]{20,}", body), p
        for line in body.splitlines():
            if "funnel" in line.lower():
                assert re.search(r"never|not|public", line, re.I), (p, line)
    readme = text("README.md")
    assert "tailscale serve --bg --http=3030 http://127.0.0.1:3030" in readme
    assert "One-alert test" in readme
