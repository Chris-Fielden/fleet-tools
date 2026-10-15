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
| `max_launches_per_day` | `10` | launch attempts a day; the day ends at local midnight in `timezone` |
| `timezone` | `America/Los_Angeles` | IANA name; the daily launch and on-call caps reset at midnight here, not at UTC midnight |
| `prep_time` | `06:00` | HH:MM, America/Los_Angeles: the prep pass over tickets in review (two or more waiting) runs once a day at this time (or when nothing is left to build) |
| `prep_pass_gap_hours` | `4` | from `prep_time` on, at most one prep pass starts per this many hours; 0 turns it off |
| `ship_prompt_timeout_s` | `900` | a merge that prints nothing for this many seconds (it is waiting at a prompt) is cancelled cleanly, its reservation freed, and one needs-human ping sent; 0 turns it off |
| `ship_step_timeout_s` | `3600` | wall-clock limit for the merge script as a whole, for `review.sh` and for each `ticket gate` run of a ship; one past it is stopped and a needs-human ping sent; 0 turns it off |
| `ship_reservation_max_age_s` | `7200` | a ship's reservation older than this, or whose pid now runs a different command, no longer blocks its ticket; 0 turns the age limit off |
| `projects_dir` | `~/projects` | a ticket's repo is `<projects_dir>/<repo>` |
| `research_dir` | `~/fleet-research` | folder for read-only research agents |
| `herdr_workspace` | `fleet` | herdr workspace agents are launched in |
| `tracker` | `local` | backend holding the tickets (see below) |
| `id_prefixes` | `build BD, fix FX, review RV, question QN, investigate ID` | two capital letters per ticket type |
| `emergency_prefix` | `EM` | prefix for any urgent ticket |
| `oncall_prefix` | `OC` | prefix for the on-call fix tickets filed after a true failure |
| `max_oncall_per_day` | `3` | on-call attempts a day (local day in `timezone`); they do not count against `max_launches_per_day` |
| `fresh_session_tokens` | `100000` | When a claimed ticket gets a FAIL verdict and its agent's session context is over this many tokens, dispatch closes that agent's tab and starts a fresh session on the same branch (logged `FRESH_SESSION`); 0 turns it off. Only Claude sessions can be measured; a Codex session is never replaced |
| `pause_weekly_pct` | `90` | weekly plan usage (from the status line) at which no agent of any kind is launched: normal, urgent, on-call, investigations, reconcile, prep, relaunches, fresh sessions. Checked every dispatch cycle; logs `LAUNCH_PAUSED` and sends one low ping on crossing. Lifts at the weekly reset; 0 turns it off |
| `hard_stop_weekly_pct` | `95` | weekly usage at which running agents are stopped too: each gets up to 20 s to finish its tool call, then is interrupted (Escape); work stays in its task folder; its ticket gets a note and is held (not failed) and a new agent starts after the weekly reset; one high ping. Shown in `fleet status`; 0 turns it off |
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
