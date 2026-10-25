# Rules for agents working in this repository

These rules are enforced by hooks in `.claude/hooks/`. If a hook blocks you,
read its message, fix the cause, and try again. Never try to get around a hook.

## 1. Work only inside your task worktree

- Every task happens in its own folder under `.worktrees/<task-name>/`,
  on its own branch named `task/<task-name>`.
- The main checkout (the repository root) belongs to the human. Do not edit
  files there, and do not commit, merge, rebase or push from there.
- If you are in the main checkout, stop and tell the human to run
  `scripts/new-task.sh <task-name>` and start you inside that folder.

## 2. Nothing is finished until the checks pass

- `scripts/check.sh` is the definition of "working". Run it before you say
  you are done.
- If it fails, fix the problem. Do not delete, skip or weaken tests or checks
  to make them pass.
- Commit your work on your task branch before you stop. Uncommitted work
  does not count.
- Never edit the root `CHANGELOG.md` on a task branch. Only the release
  tagger writes it, at ship, from the ticket title; put the wording there.
  `scripts/check.sh` fails a branch whose copy differs from main's.

## 3. A different reviewer looks at every change

- When your checks pass, run `scripts/review.sh`. It asks a model from the
  other company to review your branch read-only: Codex reviews Claude's
  work, and Claude reviews Codex's.
- Fix anything it marks **MUST FIX**, commit, and run the checks again.
- You never approve or merge your own work.
- How many reviewers depends on the risk level (see below). On a high-risk
  branch `scripts/review.sh` runs both reviewers by default.
- For a second opinion mid-task (a hard design choice, a stubborn bug,
  anything security-related), Claude uses `scripts/ask-codex.sh` (the
  ask-codex skill) and Codex uses `scripts/ask-claude.sh`. Both are
  read-only consults. Tell the human what the other agent said.

This file is the rulebook for every agent in this repo, Codex included
(see `AGENTS.md`).

### Risk levels and reviewers

Every change has a risk level: **low**, **medium** or **high**. It comes from
`risk-rules.json`: each file gets the highest level of any rule it matches, a
change gets the highest level of its files, and unmatched files get the
default (`medium`). The rules are read from the base branch, so a branch can't
weaken its own rules.

- **low** (for example `docs/**`, `tests/**`) and **medium**: one review by
  the other model. As Claude, you get Codex: just run `scripts/review.sh`.
- **high** (for example `k8s/**`, `.claude/**`, `.codex/**`, `CLAUDE.md`,
  `AGENTS.md`, the core scripts, anything touching secrets or auth,
  dependency files, and the ticket/risk code): **both reviewers**, Codex and
  Claude, must PASS before the human can merge. `scripts/review.sh` with the
  default `REVIEWER=auto` runs both on a high-risk branch; `REVIEWER=both`
  forces it.

## 4. Only the human merges to main

- Merging is done by the human with `ship` (or `scripts/finish-task.sh
  <task-name>`). Do not run either, and do not push to `main` or `master`.
  Below high risk the human is not asked a y/N question; at high risk they
  must type the ticket id.
- `ship ID --accept-leftovers --reason TEXT` is human-only, from a terminal.
  It merges one exact commit despite a Codex FAIL, only when the supervisor
  marked that commit ready with leftovers and a saved Claude PASS exists on
  it. Never at high risk. Nothing else waives a reviewer. You can never use it.
- When you are done, say so, and list: what changed, how you verified it,
  and what the reviewer said.
- Record that as a final SUMMARY with `ticket note <ID> "SUMMARY"` (what
  changed, how you verified it, the review verdict; at most 4000 characters,
  no secrets), then repeat it in your reply. Don't stop until the note is
  saved. Use `ticket` only for your own ticket (`ticket show`, `ticket note`)
  unless the human says otherwise.

## 5. GitOps: git is how things get deployed

- The cluster is changed by committing YAML to git, which ArgoCD then applies.
- Do not change the cluster directly. `kubectl apply/create/delete/edit/patch`,
  `helm install/upgrade/uninstall` and similar are blocked.
- Read-only commands are fine: `kubectl get`, `describe`, `logs`, `diff`.
- Kubernetes manifests live under `k8s/`. They must pass `kubeconform`
  (part of `scripts/check.sh`).

## 6. Secrets never go in git

- Never write passwords, API keys, tokens or private keys into files.
- Never create or edit `.env` files. Refer to secrets by name and let the
  human put the real value in place.

## 7. Be careful and be honest

- No `rm -rf` on broad paths, no force-pushes, no `git reset --hard` outside
  your worktree.
- If you are unsure, pick the safer option (section 8). If something
  failed, say so plainly.

Do not launch Cursor (`agent`, `cursor-agent`, `cursor agent`) or use its
bypass flags; only the human or fleet scripts start it.

## 8. Never stop to wait for an answer

- No one is watching while you work, so a question
  stalls the fleet. Never wait on a reply.
- When a choice is unclear, pick the safer option,
  write a ticket note (`ticket note`) saying what
  you chose and why, and continue.
- If no option is safe, write a ticket note saying
  what is blocked and end your turn.
