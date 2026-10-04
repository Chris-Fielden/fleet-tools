# Phone notifications (ntfy)

Best-effort pushes to your phone through a **self-hosted** [ntfy](https://ntfy.sh)
server on your tailnet. Code: `fleet/notify.py`. Off unless `notify.json` exists
in the fleet home (the folder `$FLEET_HOME` points to; the same place as `kaneo.json`).

## What is sent, and when

Only on a real transition, never on a re-run or a note:

| Event | When |
|---|---|
| `ready` | A review ticket is created open (a branch is ready for review). |
| `urgent` | An urgent ticket is created (wins over `ready`). |
| `failed` | A ticket is closed as failed, or an investigation ends failed. |
| `proposed` | A follow-up ticket is proposed. Off unless `"follow_ups": true`. |
| `investigated` | An investigation ends done (`dispatch`). |
| `ship` | `dispatch --watch` moved a ticket to In Review: "<ID> ready to ship", with risk level and title. |
| `review_failed` | No longer sent: a mid-loop FAIL never pings (see `oncall`). The name stays valid in `priorities`. |
| `review_unrecorded` | A review for the current tip is saved in `.reviews/` but is not on the ticket (`ticket review-result` failed or was never run). |
| `blocked`, `stalled` | No longer sent: a blocked (20 min) or stalled (45 min) agent is a true failure (see `oncall`). The names stay valid in `priorities`. |
| `oncall` | The dispatcher closed a ticket as failed and filed an on-call ticket: "<ID> failed, on-call <OC ID> is on it". Low. |
| `needs_human` | A failed ticket the on-call agent will not (or could not) fix: "<ID> needs a human, see notes". Low. |

The last four (`ship`, `review_unrecorded`, `oncall`, `needs_human`) come from `dispatch --watch` (see `fleet/review_watch.py`). They
are decided from the stored ticket (verdict, notes), git, the review files, herdr
and the dispatcher log, never from what the agent says in its summary. Each is
sent once (per commit, or once per failed ticket); what was sent is kept in
`$FLEET_HOME/review-watch.json`, so a restart does not repeat it, and a send that
failed is tried again on the next cycle.

### Priorities

Each message carries its ntfy priority in the JSON body and in a `Priority`
header. Defaults: `urgent`, `failed`, `blocked`, `review_failed`,
`review_unrecorded` = `high`; `ready`, `ship`, `stalled` = `default`;
`proposed`, `investigated`, `oncall`, `needs_human` = `low`. Override any of them in `notify.json`:
`"priorities": {"stalled": "low", "ship": "high"}` (values: `min`, `low`,
`default`, `high`, `max`). An unknown event or value makes the file unusable.

Each message holds the ticket id, type, status and a title cut to 60
characters. The body and notes are never sent. A failed send (server down,
4-second timeout per network step, HTTP error) prints one warning and never fails the
command. Redirects are not followed, so the token cannot be forwarded. The send is
synchronous, so a dead server can delay a command by a few seconds; use an IP
address in `url` for a firm limit.

## notify.json

```json
{"url": "http://100.64.0.5:2586", "topic": "fleet", "token": "tk_...", "follow_ups": false}
```

`url` and `topic` are required. `token` is optional but you should use one.
The file must be `chmod 600`; it is refused otherwise. Never commit it.

## One-time server setup (a human does this; not part of the code)

1. Install ntfy on the tower. In `server.yml`:
   - `auth-default-access: "deny-all"`, then add a user and a token
     (`ntfy user add`, `ntfy token add`) and grant it write on your topic;
     your phone's user gets read.
   - Do **not** set `upstream-base-url`: no relay through ntfy.sh. (On iOS this
     means instant delivery is not available; Android works without it.)
   - Bind it to the tailscale IP only (`listen-http: "100.x.y.z:2586"`), or keep
     it on localhost and publish it with `tailscale serve`. **Never use
     `tailscale funnel`**: that puts it on the public internet.
2. Install the ntfy Android app, point it at your server URL, log in, and
   subscribe to the topic. Use the app's "Instant delivery" (foreground service).
3. Android: Settings > Apps > ntfy > Battery > Unrestricted, so the system
   does not stop the connection. Keep Tailscale connected on the phone too.
4. Test: `curl -H "Authorization: Bearer tk_..." -d '{"topic":"fleet","message":"hi"}' http://100.x.y.z:2586`.
