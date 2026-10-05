# Settings, the tracker interface and `fleet doctor`

## One settings file

`$FLEET_HOME/settings.json` holds every personal value. It is optional; a
missing key uses its default. The older `$FLEET_HOME/dispatch.json` is still
read, and `settings.json` wins over it. Unknown keys or bad values are an error
with a plain message.
**No secrets here**: the Kaneo API key stays in `$FLEET_HOME/kaneo.json`
(mode 600).

| key | default | meaning |
|---|---|---|
| `builder` | `claude` | agent that builds new work (`claude` or `codex`) |
| `models` | `{"claude": "sonnet", "codex": null}` | model per agent; null = tool default |
| `max_running` | `1` | tickets claimed at once (a ticket in review does not count) |
| `auto_investigate` | `true` | start proposed investigations without approval |
| `max_investigations_per_day` | `3` | |
| `watch_interval_seconds` | `120` | at least 30 |
| `max_launches_per_day` | `10` | |
| `prep_time` | `06:00` | HH:MM, America/Los_Angeles: the prep pass over tickets in review runs by then (or earlier, when nothing is left to build) |
| `projects_dir` | `~/projects` | a ticket's repo is `<projects_dir>/<repo>` |
| `research_dir` | `~/fleet-research` | folder for read-only research agents |
| `herdr_workspace` | `fleet` | herdr workspace agents are launched in |
| `tracker` | `local` | backend holding the tickets (see below) |
| `id_prefixes` | `build BD, fix FX, review RV, question QN, investigate ID` | two capital letters per ticket type |
| `emergency_prefix` | `EM` | prefix for any urgent ticket |
| `oncall_prefix` | `OC` | prefix for the on-call fix tickets filed after a true failure |
| `max_oncall_per_day` | `3` | on-call attempts a day (UTC); they do not count against `max_launches_per_day` |
| `fresh_session_tokens` | `100000` | When a claimed ticket gets a FAIL verdict and its agent's session context is over this many tokens, dispatch closes that agent's tab and starts a fresh session on the same branch (logged `FRESH_SESSION`); 0 turns it off. Only Claude sessions can be measured; a Codex session is never replaced |
| `fail_rounds_threshold` | `8` | FAIL verdicts on different commits (latest still FAIL) after which a claimed ticket is "going in circles" and truly fails; 1 or more |
| `kaneo.workspace_id` | `null` | used when `kaneo.json` lacks it (the url stays in `kaneo.json`: it decides where the key is sent) |
| `kaneo.project_icon` | `Layout` | icon of projects the mirror creates |

## The tracker interface (`fleet/tracker.py`)

`Tracker` operations: `list_tickets`, `list_eligible(settings)`, `show`,
`create`, `approve`, `claim`, `set_status`, `add_note`, `set_risk`, `close`,
and `check` (health rows for `fleet doctor`). The `ticket` command, the
`idea`/`alert` shortcuts and the dispatcher's claim/reopen steps go through it.

`LocalTracker` is the first backend: the JSON store under
`$FLEET_HOME/tickets` (source of truth) plus the one-way Kaneo mirror,
unchanged. A Kaneo problem is a warning, never a failed operation. A new
backend implements `Tracker` and is added to both `tracker.BACKENDS` and
`settings.TRACKERS` (a test keeps them in step). Id prefixes must all differ.

Kaneo columns: an urgent ticket sits in **Urgent** only while it is open.
Claiming it moves it to **In Progress**, then **In Review**, so Urgent shows
only emergencies nobody has picked up.

## `fleet doctor`

Read-only. Checks the settings, the tools on PATH (git and gitleaks are
required; herdr, claude, codex, kubeconform are optional) and that the tracker
backend works (ticket store readable, Kaneo reachable). Prints names, paths and
reasons, never a secret. Exits 1 if anything fails.
