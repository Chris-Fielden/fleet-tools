# Usage and fleet status on the Mac (EM-14)

Nothing runs on the Mac except display. LOIC builds one JSON snapshot; an
iTerm2 status bar component and a SwiftBar menu-bar plugin read it over the
tailnet. If the endpoint cannot be reached (or the answer is older than three
minutes) both show **fleet offline**, never old numbers.

## On LOIC

`fleet status --json` prints the snapshot. `fleet status-serve` serves it at
`http://127.0.0.1:8788/status.json` (read-only, loopback). Put it on the
tailnet the same way as Kaneo and ntfy:

    tailscale serve --bg --http=8788 http://127.0.0.1:8788

The server also checks each request itself: it answers 403 to anything that
is not from a tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48), or from
the local `tailscale serve` proxy on behalf of one, and to Funnel requests.
It refuses to listen on any other address. A request straight to loopback is
allowed (only counts and ticket ids are in it), so do not put a reverse proxy
that is not `tailscale serve` in front of it.

Snapshot fields: `tokens` (Claude tokens for the last 5 hours, today and the
week from local session logs; Codex logs hold only rate limits, so `codex` is
null), `plan_usage`, `governor` (`state` ok/paused/stopped, `weekly_pct`, `resumes_at`, `held`), `agents_running` (`id`, `minutes`), `ready_to_ship`,
`launches` (`left_today`), `dispatch_alive`, `herdr` (`server` and `workspace`, both
booleans: the server can run with the agent workspace gone), `kaneo_url`. It contains no
ticket text, paths or secrets.

### Prometheus metrics

The same server answers `GET /metrics` in Prometheus text format, with the
same tailnet-only checks. It is built fresh on every scrape and holds only
numbers and ticket ids (gauges): `fleet_tickets{status}` for every ticket
status, `fleet_launches_today`, `fleet_launches_max_per_day`,
`fleet_agents_running`, `fleet_ready_to_ship`, `fleet_dispatch_up`, and
`fleet_review_rounds{ticket}` for active tickets only.

### Real plan usage

Claude Code passes its rate limits (5-hour and weekly percentages and reset
times) to the status line command. `.claude/settings.json` sets that command to
`bin/fleet-statusline`, which saves just those numbers and a timestamp to
`$FLEET_HOME/plan-usage.json` and prints a short line (`5h 42% | wk 17%`).
Investigation (research) folders get the same status line. Nothing else from
the payload is stored.

`plan_usage.claude` shows that figure with its age and reset times, and marks
a window `stale` once its reset time has passed. With no sample, or one older
than five hours, `source` is `"estimate"`: only the token counts are available
and the widgets label them `(est)`.

## On the Mac

Copy the files in `contrib/mac/` into one folder (or symlink them). Set
`FLEET_STATUS_URL` to the tailnet address, for example
`http://loic:8788/status.json` (the default).

SwiftBar: install SwiftBar, choose a plugin folder, link `fleet.30s.py` and
`fleet_summary.py` into it, `chmod +x fleet.30s.py`, and put a `.swiftbarignore` file in the plugin folder
containing `fleet_summary.py` (it is a helper, not a plugin). Clicking the item or its
lines opens the Kaneo board.

iTerm2: enable the Python API (Settings > General > Magic), then
`pip3 install iterm2`, link `fleet_iterm.py` and `fleet_summary.py` into
`~/Library/Application Support/iTerm2/Scripts/AutoLaunch/`, restart iTerm2.
Add "Fleet status" in Profiles > Session > Configure Status Bar. Clicking it
opens the Kaneo board.
