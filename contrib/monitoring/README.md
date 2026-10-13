# Fleet monitoring (config files only)

Prometheus, Loki, Alloy and Grafana for the fleet. **Nothing here is started or
deployed by the fleet or by an agent.** A human runs it, once, when they want it.

| Piece | What it does | Listens on |
|---|---|---|
| Prometheus | Scrapes `http://127.0.0.1:8788/metrics` (BD-90) every 30s, keeps 15 days | 127.0.0.1:9090 |
| Loki | Single binary, filesystem storage, keeps 15 days | 127.0.0.1:3100 |
| Alloy | Tails `$FLEET_HOME/dispatch.log` into Loki (replaces Promtail, EOL 2026-03-02) | 127.0.0.1:12345 |
| Grafana | 4 provisioned dashboards, 4 alert rules, an ntfy contact point | 127.0.0.1:3030 |

All four use the host network and bind `127.0.0.1` themselves (that is what lets
Prometheus reach the loopback-only status endpoint). Nothing is published on a LAN
address. Only Grafana is meant to be reached from other devices, through `tailscale
serve`, below.

## Files

```
compose.yml                                  the four services
prometheus/prometheus.yml                    scrape job "fleet"
loki/loki.yaml                               single binary, 15 day retention
alloy/config.alloy                           dispatch.log -> Loki, label "kind"
grafana/provisioning/datasources/            Prometheus and Loki
grafana/provisioning/dashboards/             loads grafana/dashboards/*.json
grafana/provisioning/alerting/alerting.yaml  rules, contact point, routing
grafana/dashboards/                          overview, launches, tickets, dispatcher log
```

Dashboards: **Fleet overview** (dispatcher and exporter up, agents running, ready to
ship, launches vs cap, tickets by status), **Fleet launches**, **Fleet tickets**
(status over time, failed per hour, review rounds), **Fleet dispatcher log**.

Alert rules (folder "Fleet"):

| Alert | Fires when | Severity |
|---|---|---|
| Dispatcher down | `fleet_dispatch_up < 1` for 5m | critical |
| Launches at daily cap | `fleet_launches_today >= fleet_launches_max_per_day` | warning |
| Failed tickets rising | `delta(fleet_tickets{status="failed"}[1h]) > 0` | warning |
| Fleet exporter not scraped | `up{job="fleet"} < 1` for 5m, or no data | critical |

## Secrets

The repo holds no secret. Put the real values in a file **outside the repo** with
`chmod 600`, for example `~/.config/fleet-monitoring/env`:

```
GRAFANA_ADMIN_PASSWORD=...
NTFY_URL=http://100.x.y.z:2586
NTFY_TOPIC=fleet-grafana
NTFY_TOKEN=tk_...
# optional: FLEET_HOME=...   GRAFANA_ROOT_URL=http://<tailnet-name>:3030
```

`NTFY_URL` is the ntfy **root** (no topic in the path); the topic is in the JSON
payload. The token is sent as `Authorization: Bearer ...` by the Grafana webhook
contact point and is read from the environment only.

## Start (human, by hand)

```
cd contrib/monitoring
docker compose --env-file ~/.config/fleet-monitoring/env up -d
```

The fleet status endpoint must be running for Prometheus to have anything to scrape
(`docs/mac-widgets.md`). Do not run these from an agent session.

## Reach Grafana from your tailnet

```
tailscale serve --bg --http=3030 http://127.0.0.1:3030
```

**Never `tailscale funnel`**: that puts Grafana on the public internet. Check with
`tailscale serve status` that nothing is marked Funnel on.

## One-alert test

1. Check the token and topic reach your phone, without Grafana:
   `curl -H "Authorization: Bearer $NTFY_TOKEN" -d '{"topic":"'"$NTFY_TOPIC"'","message":"grafana path test"}' "$NTFY_URL"`
2. In Grafana: **Alerting > Contact points > fleet-ntfy > Test**. A phone
   notification titled `Fleet ...` should arrive.
3. End to end: stop the status endpoint. After about 5 minutes **Fleet exporter not
   scraped** fires and pings the phone; start it again and a resolved ping follows.

## Open question for the human

Should Grafana publish to its **own ntfy topic with a write-only token** (the default
here is topic `fleet-grafana`), instead of sharing the fleet's topic and token? A
separate token means a leaked Grafana credential can only post alerts, and cannot
read the fleet's notifications. This needs an `ntfy token add` / `ntfy access` on the
ntfy server and your phone subscribed to the new topic.

## Not verified here

These files were validated for syntax and cross-references only (`docker compose
config`, YAML and JSON parse, `tests/test_monitoring_configs.py`). Nothing was started.
Image tags are pinned; check them, and the Grafana webhook `payload.template`
(needs Grafana 12), on first start.
