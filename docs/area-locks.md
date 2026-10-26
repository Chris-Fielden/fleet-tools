# Auto-open rules and file-area locks (BD-128)

Design source: ID-56 S8 and S9. Tests: `tests/test_area_locks.py` (accept tests 17 to 23).

## Auto-open (the dispatcher, never an agent command)

A ticket filed by an agent starts `proposed`, as before. Each dispatcher cycle,
`dispatch.auto_open` opens a proposed ticket only when all of these hold; otherwise it
stays proposed until a human runs `ticket approve` (kept until ship-only ships):

- a. An agent filed it with `origin` (`ticket new --origin ID`) naming a **handoff**: an
  investigate ticket a human filed, open or later, that was not itself auto-opened. It has
  acceptance criteria in the body (an `Acceptance:` line followed by at least one bullet of
  10+ characters). An investigate ticket instead needs the handoff as its `--parent`.
- b. Its risk is not high; it declares `areas`; no area meets a high rule of
  `risk-rules.json` (areas are compared as globs: `**`, `fleet/*.py` and `.claude` all count
  as high); its body names no high path.
- c. Its repo is not in `auto_open_deny_repos`.
- d. Caps: at most `AGENT_TICKET_LIMIT` (5) auto-opens an hour, `max_auto_open_per_day` a day,
  and `launches_today` below `max_launches_per_day`.

`ticket.open_proposed` does the opening and refuses any process with an agent environment.
Clearing `CLAUDECODE` is not a boundary by itself: `guard_bash` blocks agents from running
`dispatch`, `ticket approve` and `ticket release-area` however the command is wrapped.
Every auto-open is logged as `AUTO_OPEN` in `dispatch.log`.

## File-area locks

`areas` is a list of repo-relative path globs on a ticket (`--area`, repeatable). The lock
table is `$FLEET_HOME/area-locks.json`.

- The dispatcher refuses to launch a ticket whose areas intersect a live lock of another
  ticket in the same repo ("waiting for the area lock of ..."); `launch` takes the lock with
  the claim (a refusal puts the ticket back to open). Tickets without areas are never held.
- A lock is live until its lease ends (`area_lease_hours`, default 6) or its ticket leaves
  claimed/review. Each cycle renews the locks of tickets whose agent is running (herdr is asked whenever a
  claimed ticket holds areas) and of tickets in review (they wait for the human), and drops the rest. `ticket release-area ID` (human only) frees one by hand.
- Globs: `**` crosses folders, `*` and `?` do not; a path with no wildcard and no extension,
  or ending in `/`, also covers everything below it. Two globs that both contain wildcards in
  the same segment are assumed to meet (too coarse serialises; too fine would miss overlap).
- `review_watch` compares the real diffs (against main) of active branches in one repo and
  pings `needs_human` once per pair and file ("overlap").
- `ticket claim-area --repo R --branch task/x --title T --body B --area GLOB...` is what a chat
  runs before changing a repo: it files the build ticket and says which tickets it queues behind.

## Denying edits without a ticket (UNVERIFIED in practice)

With `require_ticket_for_edits: true` in `settings.json`, `guard_edits` refuses an edit in a task
worktree of a repo that has tickets unless the worktree's branch belongs to a claimed or
in-review ticket. Hooks see paths only, so this checks the branch, not who is editing. It is off
by default because it also stops a human's own ticketless worktree; a store that cannot be read
does not block. Whether Codex's patch hook enforces it the same way has not been checked.

Auto-open is a convenience, not a security boundary: the areas are declared by the filing agent, and the merge gate still judges the real diff and risk level.
