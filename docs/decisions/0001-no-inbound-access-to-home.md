# 0001. No inbound access to the home network

- **Date:** 2026-10-15
- **Status:** accepted
- **Decided by:** human

## Context

The fleet runs on a machine at home. Remote control, webhooks and dashboards
are tempting, but each open inbound path exposes the home network and the
secrets on this machine.

## Decision

Nothing outside may open a connection into the home network. No port
forwarding, no public tunnels, no inbound webhooks. The fleet only makes
outbound connections.

## Consequences

- Integrations poll or sync outward (for example the Kaneo sync) instead of
  being called.
- Remote views of the fleet need an outbound path, not an open port.
- Some things are slower than a push model. We accept that.

## Alternatives considered

- Port forwarding or a public tunnel: widens the attack surface of a
  machine holding credentials.
- Inbound webhooks: same exposure, so polling is used.
