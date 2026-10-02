# Ticket system: phase 1 spec

This repo holds the tools that let Claude and Codex work through tickets
instead of talking to each other. The full design is the doc
"Ticket-Based Agent Design". This file covers **phase 1 only**: the ticket
format, the validator, and the `ticket` command. No dispatcher, no agent
launching, no hooks in this phase.

## Ground rules for the build

- Python 3.12, standard library only. No third-party packages at runtime.
- Tests use pytest and live in `tests/`. `scripts/check.sh` must pass.
- Code lives in `fleet/` (a package). The command is `bin/ticket`, a small
  wrapper that calls `fleet.ticket_cli:main`.
- Don't install anything into the user's home folder. The human installs it
  after merging.

## Where tickets are stored

- One JSON file per ticket: `$FLEET_HOME/tickets/<id>.json`.
- `FLEET_HOME` defaults to `~/.fleet`. Tests must set `FLEET_HOME` to a
  temporary folder and never touch the real one.
- Writes are atomic: write to a temp file in the same folder, then
  `os.replace` it into place.
- New ids come from a counter guarded by a lock file (`fcntl.flock`), so two
  `ticket new` commands at the same moment never get the same id.

## The ticket format

| Field | Type | Rules |
| --- | --- | --- |
| id | string | Assigned by the tool: `T-YYYYMMDD-NNN` (NNN restarts at 001 each day). Never accepted from the caller. |
| type | string | One of: build, review, fix, question. |
| repo | string | 1 to 64 characters: letters, digits, `.`, `_`, `-`. |
| branch | string | Must start with `task/`, then 1 to 64 characters: lowercase letters, digits, `-`. |
| commit | string or null | Required for review, otherwise null. 7 to 40 lowercase hex characters. |
| parent | string or null | Required for review and fix; must be the id of an existing ticket. Optional for question; null for build. |
| title | string | 1 to 100 characters, a single line. |
| body | string | 1 to 4,000 characters. |
| created_by | string | human, claude or codex. Set by the tool, never by the caller (see below). |
| assignee | string or null | Always null in phase 1. Only the dispatcher sets it later. |
| status | string | One of: proposed, open, claimed, done, failed, cancelled. |
| verdict | object or null | Always null in phase 1. |
| created_at | string | UTC time, ISO 8601, set by the tool. |
| updated_at | string | UTC time, ISO 8601, set by the tool. |

Unknown fields are rejected when reading a ticket file.

### Who created it

`created_by` comes from the environment, not from a flag:

- `CLAUDECODE` set → `claude`
- `CODEX_SANDBOX` or `CODEX_SANDBOX_NETWORK_DISABLED` set → `codex`
- otherwise → `human`

### Starting status

- A build ticket created by `human` starts as `open`.
- A build ticket created by `claude` or `codex` starts as `proposed` and
  waits for the human to approve it.
- review, fix and question tickets start as `open`.

### Secret check

Before saving, the title and body are piped to
`gitleaks stdin --no-banner --redact --log-level error`. If gitleaks finds
anything, the ticket is rejected. If gitleaks isn't installed, the ticket is
also rejected (fail closed) with a message saying how to install it. Tests
that don't need a real gitleaks may point the tool at a fake one through an
environment variable `FLEET_GITLEAKS` (default: `gitleaks`).

## The `ticket` command

```
ticket new --type TYPE --repo REPO --branch BRANCH --title TITLE
           (--body TEXT | --body-file PATH) [--commit SHA] [--parent ID]
ticket list [--status STATUS] [--repo REPO] [--json]
ticket show ID [--json]
ticket approve ID
```

- `new` validates everything, saves the ticket, and prints its id.
- `list` prints one line per ticket, newest first: id, status, type, repo,
  branch, title. `--json` prints a JSON array instead.
- `show` prints the whole ticket in a readable layout, or the raw JSON with
  `--json`.
- `approve` turns a `proposed` build ticket into `open`. It refuses when
  `created_by` would be claude or codex (only a human approves), and refuses
  for any ticket that isn't a proposed build ticket.
- Exit codes: 0 on success, 2 for invalid input (every problem listed, one
  per line, in plain words), 1 for anything else.

## Tests to include (at least)

1. Each field rule: one passing and one failing case.
2. `created_by` and starting status for each environment.
3. A caller can't set id, created_by, assignee or status.
4. review without commit or parent is rejected; parent must exist.
5. gitleaks finding something → rejected; gitleaks missing → rejected.
6. Two `ticket new` processes at once get different ids.
7. `approve`: works for a human on a proposed build ticket; refused for
   agents and for every other case.
8. A ticket file with an unknown field is rejected on read.

## Done means

- `scripts/check.sh` passes in the task worktree.
- Work is committed on the task branch.
- `scripts/review.sh` has run and its verdict is PASS (fix any MUST FIX).
- A short summary for the human: what was built, how it was tested, the
  review verdict.
