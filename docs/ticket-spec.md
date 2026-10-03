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
- New ids come from counters kept in `$FLEET_HOME/tickets/.counters`, updated
  under the store lock file (`fcntl.flock`), so two `ticket new` commands at
  the same moment never get the same id. A counter never goes below the
  highest id already on disk, so a lost counters file cannot cause a repeat.

## The ticket format

| Field | Type | Rules |
| --- | --- | --- |
| id | string | Assigned by the tool: a short id, prefix plus number (see "Ticket ids"). Never accepted from the caller. Older `T-YYYYMMDD-NNN` ids are still valid. |
| type | string | One of: build, review, fix, question, investigate. An investigate ticket is read-only research: no commit, parent optional. |
| repo | string | 1 to 64 characters: letters, digits, `.`, `_`, `-`. |
| branch | string | Must start with `task/`, then 1 to 64 characters: lowercase letters, digits, `-`. |
| commit | string or null | Required for review, otherwise null. 7 to 40 lowercase hex characters. |
| parent | string or null | Required for review and fix; must be the id of an existing ticket. Optional for question; for build, null, or the id of an investigate ticket (a follow-up). |
| title | string | 1 to 100 characters, a single line. |
| body | string | 1 to 4,000 characters. |
| created_by | string | human, claude or codex. Set by the tool, never by the caller (see below). |
| assignee | string or null | null until the dispatcher claims the ticket; then claude or codex. |
| status | string | One of: proposed, open, claimed, done, failed, cancelled. |
| priority | string, optional | `normal` (the default when absent) or `urgent`; nothing else is accepted. Supplied by the caller (`ticket new --priority`, or `alert`). |
| risk | string, optional | `low`, `medium` or `high`. Set by the tool when the ticket is filed (see "Risk levels"); never supplied by the caller. Absent on older tickets, which count as `medium`. Only ever goes up. |
| verdict | object or null | Null until a review is recorded. Then `{result: PASS or FAIL, commit: full id of the commit reviewed, reviewer: the latest, at, reviews: {codex or claude: {result, reviewer}}}`. Written by `ticket review-result` (run by `scripts/review.sh` from the saved review); two reviews of one commit combine (FAIL wins), a review of a new commit replaces it. Information only: the merge gate reads the saved review and the commit itself. Shown by `ticket show` and on the Kaneo card. |
| notes | list, optional | 1 to 50 notes, each `{at, by, text}`: a UTC time, the creator kind, and 1 to 4000 characters (gitleaks-checked). Appended by `ticket note`; shown by `ticket show` and on the Kaneo card. |
| started_at | time, optional | When the dispatcher launched an investigation. Counts toward `max_investigations_per_day`. |
| close_note | string, optional | 1 to 500 characters, gitleaks-checked. Set by `ticket close --note`. Absent otherwise. |
| base_commit | string, optional | Git commit id (7 to 40 hex characters): main's tip when the dispatcher claimed the ticket. Reconcile only counts a `Merge <branch>` commit made after it. Absent on tickets claimed before this field existed (they count any merge in main's history). |
| seq | integer, optional | Creation sequence, 1 or more, counting up across all tickets. Set by the tool; the tie-breaker when `created_at` is equal. Absent on tickets made before short ids (they count as 0). |
| created_at | string | UTC time, ISO 8601, set by the tool. |
| updated_at | string | UTC time, ISO 8601, set by the tool. |

Unknown fields are rejected when reading a ticket file. The counters file is not a ticket and is ignored by `list`.

### Ticket ids

A new ticket's id is a prefix by kind plus a number that counts up per prefix,
with no padding: `BD-1`, `BD-2`, `FX-1`, `EM-1`.

| Prefix | For |
| --- | --- |
| BD | build |
| FX | fix |
| RV | review |
| QN | question |
| ID | investigate (ideas) |
| EM | any ticket whose `priority` is `urgent` (wins over the type prefix) |

- The prefix comes from one lookup table (`TYPE_PREFIX`, plus
  `EMERGENCY_PREFIX`) in `fleet/ticket.py`.
- The prefix is fixed when the ticket is created and never changes, even if
  its priority changes later.
- Existing `T-YYYYMMDD-NNN` tickets keep their ids and work everywhere (show,
  close, approve, parent links, Kaneo cards, the map file).
- Nothing reads meaning out of an id. Anything that sorts oldest first
  (the dispatcher, reconcile) uses `created_at`, then `seq`, then the id only
  to keep the order stable. `ticket list` is the same order, reversed.
- Kaneo card titles use the id: `[BD-1] title`.

### Who created it

`created_by` comes from the environment, not from a flag:

- `CLAUDECODE` set → `claude`
- `CODEX_SANDBOX` or `CODEX_SANDBOX_NETWORK_DISABLED` set → `codex`
- otherwise → `human`

### Starting status

- An investigate ticket always starts as `proposed`, whoever files it, and
  is not worked on until it is approved, with one exception: unless
  `auto_investigate` is off, the dispatcher may start a read-only research
  agent on a `proposed` investigate ticket without approval (see the
  dispatcher spec).
- Every other ticket (build, review, fix, question) starts as `open`, whoever
  files it. Agents start work freely; the human approves at merge.
- Exception: a build ticket with a `parent` (a follow-up filed from an
  investigation) starts as `proposed`. Its parent must be an `investigate`
  ticket; no other build ticket may have a parent.

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
           [--priority normal|urgent]
ticket list [--status STATUS] [--repo REPO] [--json]
ticket show ID [--json]
ticket approve ID
ticket note ID TEXT
ticket risk [ID] [--diff BASE...BRANCH] [--dir REPO]
ticket risk ID --raise low|medium|high
ticket gate --base BASE --branch BRANCH --worktree DIR [--dir REPO] [--field risk|confirm|ok]
```

- `new` validates everything, saves the ticket, and prints its id.
- `list` prints one line per ticket, newest first: id, status, type, repo,
  branch, title. `--json` prints a JSON array instead.
- `show` prints the whole ticket in a readable layout, or the raw JSON with
  `--json`.
- `approve` turns a `proposed` ticket into `open`. It refuses when the
  caller is claude or codex (only a human approves), and refuses for any
  ticket that isn't proposed.
- `note` appends a note (max 4000 characters, gitleaks-checked, at most 50
  per ticket) recording who wrote it. Anyone may add one.
- Exit codes: 0 on success, 2 for invalid input (every problem listed, one
  per line, in plain words), 1 for anything else.

## Risk levels

Every change has a level, **low**, **medium** or **high**, so the merge step
can ask for more care where a mistake costs more.

**Rules.** `risk-rules.json` (repo root) maps file patterns to levels. A
file gets the highest level of any rule it matches; a change gets the
highest level of its files. Patterns: `**` crosses folders, `*` and `?` stay
inside one folder, a pattern with no `/` matches the file name in any
folder, and `contains` matches anywhere in the path, ignoring case.
- High: `k8s/**`, `.claude/**`, `.codex/**`, `CLAUDE.md`, `AGENTS.md`,
  `scripts/check.sh`, `scripts/finish-task.sh`, `scripts/review.sh`,
  `scripts/new-task.sh`, any path containing `secret`, `auth` or `.env`,
  `requirements*.txt`, `pyproject.toml`, `package.json`, `go.mod`,
  `compose.yml`, `fleet/dispatch*.py`, and (so a task cannot loosen its own
  rules) `risk-rules.json`, `fleet/risk.py`, `fleet/ticket.py`, `fleet/ticket_cli.py` and `bin/ticket`.
- Low: `docs/**`, `tests/**`. A name like `tests/test_auth.py` is still high.
- Everything else: medium.
- The rules are read from the **base** branch (`git show BASE:risk-rules.json`),
  so a branch cannot weaken the rules it is judged by. If base has no rules
  file, the tool's own copy is used.

**Starting level.** `ticket new` stores a `risk` from `ticket_start` in the
rules file: by repo first (`sandbox` is low), then by type (`investigate`,
`question` and `review` are low; `build` and `fix` are medium). Callers
cannot pick it.

**Only up.** The higher of the stored level and the diff level always wins.
Nothing lowers a stored level. `ticket risk ID --raise LEVEL` raises it; it
is human-only (the library refuses agents and the guard hook blocks them) and
refuses an equal or lower level. `ticket risk --diff BASE...BRANCH` prints
each changed file with its level and matching rule (renames count as delete
plus add), then `diff level:` and `risk:`. With an ID it also shows the stored
level and the higher of the two; it never writes the ticket.

**Merge gate.** `ticket gate` prints the level (and why), the exact commit,
and each review, and exits 1 if merging is not allowed. A review only counts
if its `Commit:` header is the full id of the branch tip. The newest review
per reviewer decides, and any FAIL blocks. Medium and low need one PASS. High
needs a PASS from both Codex and Claude, with Claude on a stronger model
(`opus` or `fable`): `REVIEWER=both scripts/review.sh`. Tickets on the branch
are found by branch name and repo name (`--repo`; the merge script passes the
main checkout's folder name); the highest stored level counts.

**Merge script.** `scripts/finish-task.sh` runs the gate, refuses to merge if
it fails, shows the level, and merges the exact commit it checked (it stops if
the branch moved meanwhile). For high risk the human types the ticket id
instead of `y` (the task name if no ticket is found). For a real emergency,
`scripts/finish-task.sh NAME --emergency "why"` skips the review requirement
after the human types `OVERRIDE`; it is announced loudly, appended to
`$FLEET_HOME/override.log` (default `~/.fleet/override.log`) and written into
the merge commit message. The checks are never skipped.

Known limit: review files are plain files in the task's `.reviews/` folder,
so the gate trusts that folder. It checks the commit and the verdict but
cannot prove who wrote the file.

## Shortcut commands

Two commands in `bin/` file a ticket in one line. Both print the new ticket id
(and mirror it to Kaneo like `ticket new`). With no text they print plain usage
help to stderr and exit 2. Invalid input gives the usual one-line problems and
exit 2.

```
idea TEXT
alert REPO TEXT
```

- `idea TEXT` files an `investigate` ticket in repo `ideas`, branch
  `task/idea-TIMESTAMP` (UTC, `YYYYMMDDHHMMSSffffff`, with microseconds so two filed in the same second differ). Title: the first 100
  characters of TEXT (line breaks become spaces). Body: all of TEXT. It
  starts as `proposed`.
- `alert REPO TEXT` files a `build` ticket in REPO with priority `urgent`,
  branch `task/alert-TIMESTAMP`, title and body as for `idea`. Its id is
  `EM-n`, and it starts `open`.
- TEXT is all the remaining arguments joined with spaces.

## Dispatcher order

The dispatcher considers open tickets urgent first, then normal, oldest first
within each group, whoever filed them.

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
