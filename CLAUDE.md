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

## 3. A different reviewer looks at every change

- When your checks pass, run `scripts/review.sh`. It asks a model from the
  other company to review your branch read-only: Codex reviews Claude's
  work, and Claude reviews Codex's.
- Fix anything it marks **MUST FIX**, commit, and run the checks again.
- You never approve or merge your own work.
- For a second opinion mid-task (a hard design choice, a stubborn bug,
  anything security-related), Claude uses `scripts/ask-codex.sh` (the
  ask-codex skill) and Codex uses `scripts/ask-claude.sh`. Both are
  read-only consults. Tell the human what the other agent said.

This file is the rulebook for every agent in this repo, Codex included
(see `AGENTS.md`).

## 4. Only the human merges to main

- Merging is done by the human with `scripts/finish-task.sh <task-name>`.
  Do not run that script, and do not push to `main` or `master`.
- When you are done, say so, and list: what changed, how you verified it,
  and what the reviewer said.

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
- If you are unsure, stop and ask. If something failed, say so plainly.
