#!/usr/bin/env bash
# HUMAN ONLY. Merge a finished task into main, then clean up.
# (Agents are blocked from running this by .claude/hooks/guard_bash.py.)
#
# Usage:  scripts/finish-task.sh <task-name> [--ticket ID] [--emergency "reason"]
#
# --ticket ID (which `ship` always passes): the ticket being merged. It must be on
# this branch, its id is what you type at the HIGH RISK prompt, and any other
# unfinished tickets sharing the branch are listed first, because merging the
# branch merges their work too. Without it the highest-risk ticket on the branch is asked for.
#
# Steps: re-run the checks in the task worktree, check the risk level and that
# the exact commit has a PASS review (high risk: from both Codex and Claude),
# show you what will be merged, ask for confirmation (high risk: you type the
# ticket id), merge, re-run the checks on main, tag the ship and add a
# CHANGELOG.md entry (scripts/release-tag.sh), push (if there's a remote,
# which is what ArgoCD watches), and remove the worktree.
#
# Emergency: --emergency "why" merges despite a missing or failing review. You
# must then type OVERRIDE, and it is logged to $FLEET_HOME/override.log
# (default ~/.fleet/override.log) and in the merge commit message.
# The checks (step 1 and 4) are never skipped.
set -euo pipefail

name=""
emergency=""
ticket=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --emergency)
      [[ -n "${2:-}" ]] || { echo "--emergency needs a reason in quotes."; exit 1; }
      emergency="$2"; shift 2 ;;
    --ticket)
      [[ -n "${2:-}" ]] || { echo "--ticket needs a ticket id."; exit 1; }
      ticket="$2"; shift 2 ;;
    -*) echo "Unknown option $1"; exit 1 ;;
    *) [[ -z "$name" ]] || { echo "Only one task name, please."; exit 1; }
       name="$1"; shift ;;
  esac
done
if [[ -z "$name" ]]; then
  echo "Usage: scripts/finish-task.sh <task-name> [--ticket ID] [--emergency \"reason\"]"
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
gate_args=(gate --base "$base" --branch "$branch" --dir "$root" --worktree "$wt" --repo "$(basename "$root")")
override_reason=""
gate_ok=1
if [[ -n "$ticket" ]]; then
  gate_args+=(--ticket "$ticket")
  # A ticket that is not on this branch is never merged, emergency or not: the gate says why.
  others_err="$($ticket_cmd "${gate_args[@]}" --field others 2>&1 >/dev/null || true)"
  if [[ -n "$others_err" ]]; then
    echo "NOT MERGING: $others_err"
    exit 1
  fi
  others="$($ticket_cmd "${gate_args[@]}" --field others 2>/dev/null || true)"
  if [[ -n "$others" ]]; then
    echo "WARNING: merging $branch also merges the work of these unfinished tickets on it:"
    while IFS= read -r line; do echo "  $line"; done <<< "$others"
  fi
fi
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
git --no-pager log --oneline "$base..$tip"
git --no-pager diff --stat "$base...$tip"

echo
if [[ -n "$override_reason" ]]; then
  echo "Type OVERRIDE to merge $tip WITHOUT the required review:"
  read -r answer
  [[ "$answer" == "OVERRIDE" ]] || { echo "Cancelled. Nothing changed."; exit 0; }
elif [[ "$risk" == "high" ]]; then
  echo "HIGH RISK change."
  echo "Type the ticket id ($confirm_word) to merge $branch into $base:"
  read -r answer
  # Case and surrounding spaces do not matter: 'Bd-36' confirms BD-36. (tr, not ${x^^}: macOS bash is 3.2.)
  typed="$(printf '%s' "$answer" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
  wanted="$(printf '%s' "$confirm_word" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
  [[ "$typed" == "$wanted" ]] || { echo "Cancelled. Nothing changed."; exit 0; }
elif [[ "$risk" == "low" || "$risk" == "medium" ]]; then
  echo "Merging $branch into $base (risk $risk; no confirmation below high risk)."
else
  echo "Unexpected risk level '$risk'. Not merging."; exit 1
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
pre_merge="$(git rev-parse HEAD)"
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
echo "== 5/5 Tag, push and clean up"
# The known-good point: only reached once the checks on $base passed. An annotated tag and a
# CHANGELOG.md entry naming the ticket, committed on $base so the push carries both.
ship_title="$($ticket_cmd show "${ticket:-$name}" 2>/dev/null | awk 'NR==1{sub(/^[^ ]+[ ]+/,""); print}' || true)"
if ! bash "$root/scripts/release-tag.sh" "${ticket:-$name}" "$ship_title"; then
  echo
  echo "Tagging FAILED after merging. Not pushed, so nothing was deployed."
  echo "Undo with:  git reset --hard $pre_merge  (or fix it and run scripts/release-tag.sh by hand)"
  exit 1
fi
# Only a repo with a k8s/ folder has anything for ArgoCD to deploy; say so only then.
pushed_note() {
  if [[ -d k8s ]]; then
    echo "Pushed. ArgoCD will pick up the change from $1."
  else
    echo "Pushed."
  fi
}
if git remote get-url origin >/dev/null 2>&1; then
  git push origin "$base" --follow-tags
  pushed_note "$base"
else
  echo "No 'origin' remote, so nothing was pushed."
fi
# The ticket's agent must not keep writing into the worktree we are about to remove: stop it
# and close its tab (only that ticket's own), and save anything it left uncommitted on a
# keep/<ticket>-unmerged branch so the removal below does not refuse. If the agent cannot be
# stopped, leave the worktree and branch in place: the merge is done and pushed, only the
# clean-up is pending (stop the agent by hand, then remove them).
if [[ -n "$ticket" ]]; then
  ship_cmd="${FLEET_SHIP:-ship}"
  if ! "$ship_cmd" --quiesce "$ticket"; then
    echo
    echo "$name is merged and pushed, but the agent of $ticket could not be stopped (see above)."
    echo "Clean-up is NOT done: the worktree $wt and branch $branch were left in place."
    echo "Stop that agent, then:  git worktree remove \"$wt\" && git branch -d \"$branch\""
    exit 1
  fi
fi
git worktree remove "$wt"
git branch -d "$branch"
echo "Done: $name merged and cleaned up."
