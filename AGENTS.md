# Rules for Codex working in this repository

This repo is shared by two agents: Claude (Claude Code) and you (Codex).
You both follow the same rules, and the same hooks enforce them.

**Read `CLAUDE.md` at the start of every task.** Despite the name, it is the
rulebook for every agent here, you included. The short version:

1. Work only inside your task worktree (`.worktrees/<task-name>/`), on your
   `task/<name>` branch. The main checkout belongs to the human.
2. The work isn't done until `scripts/check.sh` passes and your changes are
   committed. Don't weaken or skip checks. Never edit the root `CHANGELOG.md`
   on a task branch: only the release tagger writes it, at ship, from the
   ticket title.
3. A different model reviews every change. **You are the author, so Claude
   reviews your work:** run `scripts/review.sh` (it picks Claude for you, and
   runs both reviewers on high-risk branches; if it ever picks Codex, rerun
   with `REVIEWER=claude`), then fix every MUST FIX item, commit, and run the
   checks again.
4. Only the human merges, with `ship` (or `scripts/finish-task.sh`). Never run
   either, and never push to main. Below high risk the human is not asked a
   y/N question; at high risk they must type the ticket id.
   `ship ID --accept-leftovers --reason TEXT` is human-only, from a terminal.
   It merges one exact commit despite a Codex FAIL, only when the supervisor
   marked that commit ready with leftovers and a saved Claude PASS exists on
   it. Never at high risk. Nothing else waives a reviewer. You can never use it.
5. GitOps: change the cluster by committing YAML under `k8s/`. Never run
   `kubectl apply`, `helm upgrade` or similar. Read-only `kubectl` is fine.
6. Secrets never go in files or git. Don't create `.env` files.
7. When you finish, record a final SUMMARY with
   `ticket note <ID> "SUMMARY"` (what changed, how you verified it, the review
   verdict; at most 4000 characters, no secrets), then repeat it in your reply.
   Don't stop until the note is saved. Use `ticket` only for your own ticket
   (`ticket show`, `ticket note`) unless the human says otherwise.

If a hook blocks you, read its message, fix the cause, and try again.
Never try to get around a hook.

## Risk levels and reviewers

Every change has a risk level: **low**, **medium** or **high**. It comes from
`risk-rules.json`: each file gets the highest level of any rule it matches, a
change gets the highest level of its files, and unmatched files get the
default (`medium`). The rules are read from the base branch, so a branch can't
weaken its own rules.

- **low** (for example `docs/**`, `tests/**`) and **medium**: one review by
  the other model. As the author, you get Claude: just run
  `scripts/review.sh`.
- **high** (for example `k8s/**`, `.claude/**`, `.codex/**`, `CLAUDE.md`,
  `AGENTS.md`, the core scripts, anything touching secrets or auth,
  dependency files, and the ticket/risk code): **both reviewers**, Codex and
  Claude, must PASS before the human can merge. `scripts/review.sh` with the
  default `REVIEWER=auto` runs both on a high-risk branch; `REVIEWER=both`
  forces it.

Fix every MUST FIX item, commit, and run the checks again. You never approve
or merge your own work.

## Asking Claude for a second opinion

For a hard design choice, a stubborn bug, or anything security-related:

```bash
scripts/ask-claude.sh "Self-contained question that names the files involved"
```

Claude can read the repo but can't change anything, and it doesn't see your
conversation, so make the question stand alone. Tell the human what Claude
said and whether you agree.

`ask-claude.sh` and the Claude review both need network access. If they fail
with a network error, tell the human: network access for Codex is set in
`.codex/config.toml`.

## Never stop to wait for an answer

No one is watching while you work. When a choice is
unclear, pick the safer option, write a ticket note
saying what you chose and why, and continue. If no
option is safe, write a ticket note saying what is
blocked and end your turn.
