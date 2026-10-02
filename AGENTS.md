# Rules for Codex working in this repository

This repo is shared by two agents: Claude (Claude Code) and you (Codex).
You both follow the same rules, and the same hooks enforce them.

**Read `CLAUDE.md` at the start of every task.** Despite the name, it is the
rulebook for every agent here, you included. The short version:

1. Work only inside your task worktree (`.worktrees/<task-name>/`), on your
   `task/<name>` branch. The main checkout belongs to the human.
2. The work isn't done until `scripts/check.sh` passes and your changes are
   committed. Don't weaken or skip checks.
3. A different model reviews every change. **You are the author, so Claude
   reviews your work:** run `REVIEWER=claude scripts/review.sh`, then fix
   every MUST FIX item, commit, and run the checks again.
4. Only the human merges, with `scripts/finish-task.sh`. Never push to main.
5. GitOps: change the cluster by committing YAML under `k8s/`. Never run
   `kubectl apply`, `helm upgrade` or similar. Read-only `kubectl` is fine.
6. Secrets never go in files or git. Don't create `.env` files.

If a hook blocks you, read its message, fix the cause, and try again.
Never try to get around a hook.

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
