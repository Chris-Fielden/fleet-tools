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

Each message holds the ticket id, type, status and a title cut to 60
characters. The body and notes are never sent. A failed send (server down,
timeout of 4 seconds, HTTP error) prints one warning and never fails the
command. Redirects are not followed, so the token cannot be forwarded.

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
