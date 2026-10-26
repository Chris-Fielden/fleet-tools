# The controller (BD-129)

The human's only command is `ship ID`. Everything before it runs on its own.

## Handoffs

Drop a text file ending in `.txt` into `~/.fleet/inbox/`. Each watch cycle the controller reads up to
five files, parses each strictly and files its tickets. The format is `HANDOFF v1`:

```
HANDOFF v1
repo: fleet-tools
goal: one sentence
why: one sentence
tickets:
- title: Add the helper | type: build | files: src/notes.py,tests/test_notes.py
  accept: (1) the helper returns sorted names (2) a test covers the empty case
- title: Add the command | type: build | files: src/cli.py | after: Add the helper
  accept: (1) the command prints the list from the helper
not-in-scope: anything about sync
risk-note: no
```

* Closed fields only, at most 3500 characters, at most 12 tickets, no secrets, no control characters.
  Any unknown line rejects the whole file. Every ticket needs numbered `accept:` criteria, or nothing is filed.
* `type` is build, fix or investigate. A fix needs `after:` naming the existing ticket it fixes.
* `after:` names another ticket of the handoff (by title) or an existing ticket id. A ticket that waits carries
  a `Waits for:` line and stays proposed until those tickets are done. Tickets whose `files` overlap are chained
  in the order written.
* A good file moves to `inbox/done/`. A bad file moves to `inbox/rejected/` (with a `.reason.txt`) and one ntfy
  ping says why. The same text is never filed twice: tickets are found again by the marker on the first body line.
* One summary card (a `question` ticket, never launched) lists the handoff's tickets as a checklist and is
  refreshed every cycle. Its state is in `controller.json` in the fleet folder, which agents cannot write; it is
  rebuilt from the tickets after a restart.

## Nothing waits for a human

The dispatcher opens every proposed ticket (any type, any filer, any risk) within `AGENT_TICKET_LIMIT` per hour,
`max_auto_open_per_day`, `max_launches_per_day` and `auto_open_deny_repos`. Each auto-open is logged
(`AUTO_OPEN`). `ticket approve` stays as an optional shortcut. A leftovers ticket opens once its parent is
finished and starts at its parent's risk. Anyone may raise a ticket's risk; nothing lowers it.

## Proof and plain summaries

A ticket moves to review only when its exact tip commit has a passing verdict, the agent's final SUMMARY note,
a note starting `PROOF <tip commit id>` with the parts `What it does:`, `Demo:` (command and output, at most
40 lines), `Acceptance:` and `Leftovers:`, and a current plain summary. An agent writes the summary as a note
starting `PLAIN SUMMARY:`; the dispatcher copies it into the ticket's `summary` field. Spell out labels such as
"S8" or "[U]". `ticket show` and the Kaneo card print the summary first. The one "ready to ship" ping quotes
the first line of the PROOF note. A ticket is marked done only once its ship has written its `.result` file.

## Hard limits

The controller never ships, merges, closes a ticket, approves a high-risk ticket, lowers risk, launches an agent
(Cursor above all), edits a protected path or touches a main checkout. It calls no model and starts no program.
Handoff text is data; nothing the controller writes is run as a command.
