#!/usr/bin/env bash
# HUMAN ONLY. Merge a finished task into main, then clean up.
# (Agents are blocked from running this by .claude/hooks/guard_bash.py.)
#
# Usage:  scripts/finish-task.sh <task-name>
#
# Steps: re-run the checks in the task worktree, check the risk level and that
# the exact commit has a PASS review (high risk: from both Codex and Claude),
# show you what will be merged, ask for confirmation (high risk: you type the
# ticket id), merge, re-run the checks on main, push (if there's a remote,
# which is what ArgoCD watches), and remove the worktree.
#
# Emergency: --emergency "why" merges despite a missing or failing review. You
# must then type OVERRIDE, and it is logged to $FLEET_HOME/override.log
# (default ~/.fleet/override.log) and in the merge commit message.
# The checks (step 1 and 4) are never skipped.
set -euo pipefail

name=""
emergency=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --emergency)
      [[ -n "${2:-}" ]] || { echo "--emergency needs a reason in quotes."; exit 1; }
      emergency="$2"; shift 2 ;;
    -*) echo "Unknown option $1"; exit 1 ;;
    *) [[ -z "$name" ]] || { echo "Only one task name, please."; exit 1; }
       name="$1"; shift ;;
  esac
done
if [[ -z "$name" ]]; then
  echo "Usage: scripts/finish-task.sh <task-name> [--emergency \"reason\"]"
  exit 1
fi
# The ticket tool: FLEET_TICKET if set, else `ticket` on PATH.
ticket_cmd="${FLEET_TICKET:-ticket}"

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
echo "== 2/5 Risk level and reviews"
# The exact commit being reviewed, gated and merged. Merging this id (not the
# branch name) means a commit added while you read this cannot slip in.
tip="$(git rev-parse --verify "$branch^{commit}")"
gate_args=(gate --base "$base" --branch "$branch" --dir "$root" --worktree "$wt")
override_reason=""
gate_ok=1
$ticket_cmd "${gate_args[@]}" || gate_ok=0
risk="$($ticket_cmd "${gate_args[@]}" --field risk 2>/dev/null || true)"
confirm_word="$($ticket_cmd "${gate_args[@]}" --field confirm 2>/dev/null || true)"
if [[ "$gate_ok" == "0" ]]; then
  echo
  if [[ -z "$emergency" ]]; then
    echo "NOT MERGING: the risk level or the reviews above do not allow it."
    echo "Ask the agent to fix and run scripts/review.sh again (REVIEWER=both for high risk)."
    echo "Real emergency? Run again with:  --emergency \"why\"   (it is logged)"
    exit 1
  fi
  override_reason="$emergency"
  echo "!!! EMERGENCY OVERRIDE: merging without the required review."
  echo "!!! Reason: $emergency"
  echo "!!! This will be written to the override log and to the merge commit."
fi
if [[ -z "$risk" || -z "$confirm_word" ]]; then
  [[ -n "$emergency" ]] || { echo "Could not work out the risk level (is the ticket command installed?). Not merging."; exit 1; }
  risk="${risk:-unknown}"; confirm_word="${confirm_word:-$name}"
fi

echo
echo "== 3/5 What will be merged into $base ($commits commits)"
git log --oneline "$base..$tip"
git diff --stat "$base...$tip"

echo
if [[ -n "$override_reason" ]]; then
  echo "Type OVERRIDE to merge $tip WITHOUT the required review:"
  read -r answer
  [[ "$answer" == "OVERRIDE" ]] || { echo "Cancelled. Nothing changed."; exit 0; }
elif [[ "$risk" == "high" ]]; then
  echo "HIGH RISK change."
  echo "Type the ticket id ($confirm_word) to merge $branch into $base:"
  read -r answer
  [[ "$answer" == "$confirm_word" ]] || { echo "Cancelled. Nothing changed."; exit 0; }
else
  read -r -p "Merge $branch into $base? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled. Nothing changed."; exit 0; }
fi

[[ "$(git rev-parse --verify "$branch^{commit}")" == "$tip" ]] \
  || { echo "$branch changed while you were deciding. Nothing merged; run this again."; exit 1; }

merge_msg="Merge $branch"
if [[ -n "$override_reason" ]]; then
  merge_msg="$merge_msg

EMERGENCY OVERRIDE (risk $risk): $override_reason"
  log="${FLEET_HOME:-$HOME/.fleet}/override.log"
  mkdir -p "$(dirname "$log")"
  printf '%s\t%s\tbranch=%s\tcommit=%s\trisk=%s\treason=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(git config user.name || id -un)" \
    "$branch" "$tip" "$risk" "${override_reason//$'\n'/ }" >> "$log"
  echo "Override logged to $log"
fi
git merge --no-ff "$tip" -m "$merge_msg"

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
