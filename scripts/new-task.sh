#!/usr/bin/env bash
# Create an isolated workspace (a git "worktree") for one task.
#
# Usage:  scripts/new-task.sh <task-name>
# Example: scripts/new-task.sh sar-weather-alert
#
# Result: a folder .worktrees/<task-name>/ on a new branch task/<task-name>,
# branched from main. Start Claude inside that folder.
set -euo pipefail

name="${1:-}"
if [[ ! "$name" =~ ^[a-z0-9][a-z0-9-]{1,40}$ ]]; then
  echo "Usage: scripts/new-task.sh <task-name>"
  echo "Use lowercase letters, numbers and dashes, e.g. sar-weather-alert"
  exit 1
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"

# Refuse to run from inside another worktree: tasks branch from main.
if [[ "$(git rev-parse --absolute-git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
  echo "Run this from the main checkout, not from inside a worktree."
  exit 1
fi

base="main"
git rev-parse --verify --quiet main >/dev/null || base="master"

if [[ -e ".worktrees/$name" ]]; then
  echo ".worktrees/$name already exists. Pick another name or finish that task first."
  exit 1
fi

# Pull the latest main first if there is a remote (skip quietly if not).
if git remote get-url origin >/dev/null 2>&1; then
  git fetch origin "$base" --quiet || true
  git merge --ff-only "origin/$base" --quiet 2>/dev/null || true
fi

git worktree add ".worktrees/$name" -b "task/$name" "$base"

echo
echo "Task workspace ready: .worktrees/$name  (branch task/$name)"
echo "Next:"
echo "  cd .worktrees/$name"
echo "  claude"
