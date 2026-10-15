# Rollback runbook

How a human takes fleet-tools back to a known-good state. Agents never run
these steps: merging, tagging and pushing are human-only (CLAUDE.md rule 4),
and the guard hook blocks force-pushes.

## 1. Known-good points: `ship-YYYY.MM.DD.N`

Every ship is tagged automatically. After the post-merge checks on `main`
pass, `scripts/release-tag.sh` (run by the merge script, human-only) creates
an **annotated** tag `ship-<UTC date>.<N>` (N counts that day's ships from 1)
and a `CHANGELOG.md` entry naming the ticket:

```
git tag -l --sort=version:refname 'ship-*'   # oldest first; name order puts .10 before .2
git show ship-2026.10.15.1                   # the tag message names the ticket
```

- These tags are the rollback points; `CHANGELOG.md` says what each holds.
- A tag marks a ship that passed its checks, not one that has run well in
  use: pick a tag from before the problem started.
- Agents do not create or move tags, and this runbook does not define a
  second tag scheme.

In the commands below `ship-X` stands for the tag you are returning to.

## 2. Normal rollback: revert, never rewrite

Revert keeps history and needs no force-push. From the main checkout, on
`main`, clean tree:

1. **Quiesce launches first** (section 4), so no agent starts on code that is
   about to change.
2. Revert everything after the tag, without committing yet:
   ```
   git revert --no-commit ship-X..HEAD
   ```
   Merge commits need a mainline, so if the range holds merges do not use
   the range form. List the first-parent commits newest first
   (`git log --first-parent --format=%h ship-X..HEAD`) and revert each in
   that order: `git revert --no-commit -m 1 <merge>` for a merge (parent 1
   is `main`), plain `git revert --no-commit <sha>` for any other commit.
   Never revert a merge and also the commits it brought in.
3. **Check it is exact**: the index must now equal the tag.
   ```
   git diff --cached ship-X      # must print nothing
   ```
   A non-empty diff means a commit was missed, or a revert conflicted and was
   resolved differently. Fix that before committing.
4. `git commit -m "Revert to ship-X"`, then push as usual.
5. Verify (section 5), then restart the dispatcher if needed (section 6).

## 3. Emergency hard reset (last resort)

`git reset --hard ship-X` followed by a force-push rewrites `main`. Know
what that costs:

- Every other clone and every task branch based on the discarded commits now
  disagrees with `main`. Work already merged is silently dropped.
- Force-pushing is blocked for agents by `.claude/hooks/guard_bash.py:55`
  (`FORCE_PUSH`). It is a human decision, run from a terminal, and the guard
  is not something to work around.
- Prefer section 2 unless history itself must go (for example a secret was
  committed; then rotate the secret as well, since it stays in clones).
- Before doing it, record the current `HEAD` (`git rev-parse HEAD`) somewhere
  so it can be recovered with `git branch rescue/<date> <sha>`.

## 4. Quiesce launches

`fleet stop` stops only the dispatcher; running agents are left alone. With
no dispatcher nothing new is launched. Then check with `fleet status` and
`ticket list` which agents are still running, and let them finish or
decide per ticket (section 7).

## 5. Verify after rolling back

- **Dispatcher log**: a running `dispatch --watch` notices that the checkout's
  `HEAD` changed and logs `WATCH_RESTART` ("new code in the fleet-tools
  checkout (<old> -> <new>); restarting"), then re-executes on the new code.
  Look for that line in `fleet logs`. If the dispatcher was stopped, there is
  nothing to restart yet.
- `fleet status`: dispatcher, herdr and web should be up.
- `ticket list`: tickets still show the expected statuses; nothing has
  vanished or changed type.
- `dispatch --dry-run`: launches nothing, but it writes decision logs and
  takes the dispatcher lock; with a dispatcher running it exits without
  showing decisions. Run it while the dispatcher is still stopped. It should
  run without errors and match the queue you expect.
- `scripts/check.sh` on the rolled-back tree passes.

## 6. Restart the dispatcher if it is dead

If the dispatcher exited (for example it was stopped in section 4, or it
failed to re-exec), the human restarts it with the service verb:

```
fleet start        # or: fleet restart
```

Agents cannot do this for you. Then repeat the verification in section 5.

## 7. In-flight branches: rebase or not?

Task branches (`task/<name>`) created before the rollback may contain, or
depend on, the reverted changes.

- **Branch does not touch the reverted area**: leave it; the reviewer and the
  checks run against current `main` when it is merged.
- **Branch built on reverted work**: do not rebase silently. Either
  re-cut it from the rollback commit (`scripts/new-task.sh`) and carry over
  the useful commits, or cancel the ticket. The ticket's owner and the
  human decide, case by case.
- Rebasing is never done by an agent on a branch it did not create.

## 8. State-compat rule

Rolled-back code must still read state written by newer code, because
`$FLEET_HOME` (tickets, heartbeat, governor, counters) is not rolled back by
git. So changes to stored state follow **expand, then contract**:

1. *Expand*: new fields are optional. Old code ignores unknown fields; new
   code tolerates their absence. Ship this first.
2. *Contract*: only in a later change, after the expand has been live and
   rollback to before it is no longer wanted, remove the old field or make
   the new one required.

Never rename or repurpose a field in a single step.

## 9. Rehearse it

Practise on a copy, not the real store:

```
git worktree add ../rollback-rehearsal ship-X     # detached, at the tag
cp -a "${FLEET_HOME:-$HOME/.fleet}" /path/to/fleet-home-copy
cd ../rollback-rehearsal
FLEET_HOME=/path/to/fleet-home-copy bin/dispatch --dry-run
FLEET_HOME=/path/to/fleet-home-copy bin/ticket list
```

If the tag's code reads the copied state and `--dry-run` is sane, that shows
partial compatibility only (tickets load, decisions preview); it does not
prove the whole rollback will work. Remove the worktree and the copy afterwards (human step).
Tests and rehearsals always use a temporary `FLEET_HOME`, never the live one.

## 10. Argo CD (design only)

**Nothing is deployed to a cluster from this repo today** (there is no
`k8s/` directory). This section is the intended design if that changes; it is
not a procedure to run now.

- The cluster follows git (CLAUDE.md rule 5), so a rollback is the same git
  revert as section 2 on the manifests; Argo CD then applies it.
- If using Argo CD's own application rollback, **disable auto-sync on the
  application first**. Otherwise auto-sync immediately re-applies `main`
  and undoes the rollback. Re-enable it after git is brought back in line
  with what is running.
- Agents may not run `argocd app rollback` or `kubectl`/`helm` writes; the
  guard blocks them.

## 11. Sources

The URLs referenced when this runbook was designed are recorded in note 3/4
of ticket ID-34. They were not available to the task that wrote this file
(agents may not read other tickets), so the citations, each with the date
read, are still to be added by the human.
