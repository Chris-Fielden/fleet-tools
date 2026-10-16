# Approve and ship from a browser (fleet web endpoint)

Code: `fleet/web.py` (server), `fleet/webconf.py` (config), `bin/fleet-web` (start it),
`fleet/ship_cli.py` (`ship ID --non-interactive COMMIT`). Off unless `web.json` exists
in the fleet home.

## What it does

- A Kaneo card in **Proposed** gets an **Approve** link in its description; a card in
  **In Review** gets a **Ship** link. The "Ready to ship" ntfy ping has a **Ship**
  button, a proposed-ticket ping an **Approve** button. All of them open
  `/<approve|ship>/<ID>`, a mobile-friendly page showing the ticket ID, title, risk
  level, reviewed commit and verdicts. Opening it changes nothing.
- **Confirm** button. A HIGH-risk ticket also needs its ID typed (any case).
- **Ship** runs `bin/ship ID --non-interactive COMMIT` in the background (its own
  session, so closing the browser does not stop it), logging to
  `$FLEET_HOME/web/ship-<ID>-<time>.log`, and sends **one** ntfy ping: `Shipped`, or
  `Ship refused` with the reason. It applies the same gates as `ship`: the branch must
  still be at the commit the page showed, the gate must pass with reviews already on
  file (a web ship never runs a review), and `scripts/finish-task.sh` then runs all
  its own checks again. Below high risk the merge script asks nothing; at high risk
  its one prompt is answered with the typed ticket ID from the page. `--emergency` is not reachable.

## Audit log

Every refused request, approve and ship start appends one JSON line to
`$FLEET_HOME/web/audit.log` (mode 0600): `time`, `peer` address, `who` (tailnet node
name and login, if whois gave them), `action`, `ticket`, `commit` and `result`. It
never holds a token or form body. If the write fails the request is answered anyway.

## The device check (the real gate)

Every request, before anything else is read, runs `tailscale whois --json <peer
address>` and `tailscale status --json`. The request is refused (403) unless whois
names a tailnet node that is **not** this machine (LOIC, where the agents run). It
fails closed: whois failing, answering nothing usable, a loopback address, or this
machine's own address are all refused. The peer address is the socket's; no header is
trusted. The page and its token only prevent mistakes.

Bind to the tailnet address (`listen` in `web.json`); wildcard addresses are refused.
Do **not** put it behind `tailscale serve` (or any proxy on LOIC): the proxy's address
is LOIC's, so every request would be refused. Never use `tailscale funnel`.

## web.json (no secret in it)

```json
{"listen": "100.64.0.5:8787", "url": "http://loic.tailnet.ts.net:8787"}
```

`listen` is LOIC's tailnet address and a port; `url` is how your other devices reach
it (used in card links and ntfy actions). Start it as yourself (it refuses to run
inside an agent): `bin/fleet-web`.

## Tokens

Each page carries an HMAC token for that action, ticket and commit, made with a key
generated when the server starts. A POST with a missing or wrong token, or for a
commit that is no longer the branch tip, is refused ("out of date").

## Kaneo card addresses

`ticket sync` saves each card's browser address to `$FLEET_HOME/kaneo-cards.json`
(`<ui_url>/dashboard/workspace/<workspace>/project/<project>/task/<task>`), where
`ui_url` is an optional key in `kaneo.json` (default: `url`). The ntfy pings use it.
A ticket with no synced card gets no Click URL and no "Open card" button.
