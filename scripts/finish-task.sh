#!/usr/bin/env bash
# HUMAN ONLY. Merge a finished task into main, then clean up.
# (Agents are blocked from running this by .claude/hooks/guard_bash.py.)
#
# Usage:  scripts/finish-task.sh <task-name>
#
# Steps: re-run the checks in the task worktree, show you what will be
# merged, ask for confirmation, merge, re-run the checks on main, push
# (if there's a remote, which is what ArgoCD watches), and remove the worktree.
set -euo pipefail

name="${1:-}"
if [[ -z "$name" ]]; then
  echo "Usage: scripts/finish-task.sh <task-name>"
  exit 1
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"
wt=".worktrees/$name"
branch="task/$name"

if [[ "$(git rev-parse --absolute-git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
  echo "Run this from the main checkout, not from inside a worktree."
  exit 1
fi

base="main"
git rev-parse --verify --quiet main >/dev/null || base="master"

[[ "$(git branch --show-current)" == "$base" ]] || { echo "Switch the main checkout to $base first."; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "The main checkout has uncommitted changes. Sort those out first."; exit 1; }
[[ -d "$wt" ]] || { echo "No worktree at $wt."; exit 1; }
[[ -z "$(git -C "$wt" status --porcelain)" ]] || { echo "$wt has uncommitted changes. Ask the agent to commit them."; exit 1; }

commits="$(git rev-list --count "$base..$branch")"
[[ "$commits" != "0" ]] || { echo "$branch has no new commits. Nothing to merge."; exit 1; }

echo "== 1/5 Checks in the task worktree"
# Run from inside the worktree: check.sh checks whichever checkout it's run from.
(cd "$wt" && bash scripts/check.sh)

echo
echo "== 2/5 Review status"
sha="$(git rev-parse --short "$branch")"
review="$wt/.reviews/${branch//\//-}-$sha.md"
if [[ -f "$review" ]]; then
  grep -m1 '^Reviewer' "$review" || true
  grep -m1 '^VERDICT' "$review" || echo "(no VERDICT line found)"
  echo "Full review: $review"
else
  echo "WARNING: no review found for the latest commit ($sha)."
  echo "Ask the agent to run scripts/review.sh, or continue at your own risk."
fi

echo
echo "== 3/5 What will be merged into $base ($commits commits)"
git log --oneline "$base..$branch"
git diff --stat "$base...$branch"

echo
read -r -p "Merge $branch into $base? [y/N] " answer
[[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing changed."; exit 0; }

git merge --no-ff "$branch" -m "Merge $branch"

echo
echo "== 4/5 Checks on $base after the merge"
if ! bash scripts/check.sh; then
  echo
  echo "Checks FAILED on $base after merging. Undo with:  git reset --hard HEAD~1"
  echo "(Not pushed, so nothing was deployed.)"
  exit 1
fi

echo
echo "== 5/5 Push and clean up"
if git remote get-url origin >/dev/null 2>&1; then
  git push origin "$base"
  echo "Pushed. ArgoCD will pick up the change from $base."
else
  echo "No 'origin' remote, so nothing was pushed."
fi
git worktree remove "$wt"
git branch -d "$branch"
echo "Done: $name merged and cleaned up."
