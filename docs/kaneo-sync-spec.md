# Kaneo sync spec (show tickets on the Kaneo board)

Goal: every ticket in the local store shows up as a card on the Kaneo board,
in the right column, so the human can watch the fleet in a browser.

This is a **one-way mirror**. The local ticket store (`$FLEET_HOME/tickets`)
stays the single source of truth, because that's where validation happens.
Kaneo is a view. Nothing is ever read back from Kaneo into a ticket in this
phase.

## Ground rules

- Python 3.12, standard library only (`urllib.request`, `json`). No new
  packages.
- Tests never touch the network or a real Kaneo. Use a fake Kaneo: a small
  `http.server` running on 127.0.0.1 inside the tests.
- Don't install anything, and don't read or write the real `~/.fleet`.
  Tests set `FLEET_HOME` to a temporary folder.
- Look up exact request and response shapes in Kaneo's API reference:
  https://kaneo.app/docs/llms.txt lists every page. Write down in a comment
  which endpoints you used.

## Configuration

A file `$FLEET_HOME/kaneo.json`, created by the human, never by the tool:

```json
{"url": "http://127.0.0.1:5173/api", "workspace_id": "...", "api_key": "..."}
```

- Requests send `Authorization: Bearer <api_key>`.
- If the file's permissions allow anyone but the owner to read it
  (anything looser than 0600), the tool refuses to use it and says how to
  fix it (`chmod 600 ~/.fleet/kaneo.json`).
- The API key must never be printed, logged, put in an error message or
  written anywhere else.
- No config file means sync is off: `ticket sync` says so and exits 0.

## Mapping

| Ticket | Kaneo |
| --- | --- |
| repo | A project with that exact name in the workspace. Create it if missing. |
| status | A column in the project, matched by name case-insensitively: proposed -> Proposed, open -> To Do (open and `priority` urgent -> Urgent), claimed -> In Progress, done -> Done, failed -> Failed, cancelled -> Done with " (cancelled)" on the title (Kaneo has no per-card archive). Missing columns are created, Done and Failed are final, and the order is made Proposed, Urgent, To Do, In Progress, In Review, Done, Failed. An urgent ticket sits in Urgent while it is open, then moves through In Progress and Done like any other. In Review is unused. Per-status columns made by the older sync (open, claimed, cancelled) are deleted only when empty. |
| id + title | Task title: `[BD-1] Phase 1 review follow-ups` (older tickets: `[T-20261002-001] ...`) |
| body + other fields | Task description: the body, then a fenced block listing type, branch, commit, parent, created_by, assignee and the verdict line if any. |
| risk | A workspace label `risk: low`, `risk: medium` or `risk: high` on the card (created with a colour if missing; endpoints `/labels`, `POST /tasks/{id}/labels/{labelId}` to attach, `DELETE` the same path to detach, `GET /tasks/{id}/labels`). A card has exactly one risk label; a raised level swaps it. Older tickets without a risk show `risk: medium`. |
| priority | Card priority `urgent` for urgent tickets, `no-priority` otherwise. A changed priority updates the card. |

Keep a map of ticket id to Kaneo task id in `$FLEET_HOME/kaneo-map.json`
(written atomically, like tickets). Use it to update the same card instead
of creating duplicates.

## Commands

- `ticket sync`: mirror every ticket. Create missing cards; for existing
  cards, update the title, description and column when they differ. Print
  one line per ticket: created, updated, unchanged or error.
- `ticket sync --dry-run`: print what would happen. No requests that change
  anything (reads are fine).
- After `ticket new` and `ticket approve` succeed, mirror that one ticket.
  If Kaneo can't be reached, print a one-line warning and still exit 0: the
  ticket is safely saved locally, and the next `ticket sync` catches up.

## Errors

- Network errors, timeouts (10 seconds per request) and non-2xx responses
  give a plain one-line message per ticket, never a traceback, and never
  include the API key.
- One failing ticket doesn't stop the others.

## Tests to include (at least)

1. Creates the project and columns when missing, then the card in the
   right column.
2. Second sync with no changes: unchanged, and no write requests sent.
3. Status change moves the card; title or body change updates it.
4. `--dry-run` sends no write requests.
5. Config looser than 0600 is refused; missing config means sync is off.
6. The API key never appears in output, errors or files other than the
   config (check stdout, stderr and the map file).
7. Kaneo down: `ticket new` still succeeds with a warning.
8. The Authorization header is sent on every request.

## Known gap (not this phase)

Agents run as the same Linux user, so an agent could read `kaneo.json`.
A later phase adds hook rules that block agents from reading anything under
`~/.fleet` except through the `ticket` command. Mention this in your summary.

## Done means

- `scripts/check.sh` passes in the task worktree.
- Work is committed on the task branch.
- `scripts/review.sh` has run and its verdict is PASS (fix any MUST FIX).
- A short summary for the human: what was built, how it was tested, the
  review verdict, and the known gap above.
