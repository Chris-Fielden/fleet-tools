#!/usr/bin/env bash
# Get a read-only review of this task branch from a DIFFERENT model than the
# one that wrote it. Run from inside a task worktree.
#
# Usage:  scripts/review.sh
#
# Which reviewer runs:
#   REVIEWER=auto   (default) the other company's model than the author:
#                   Codex when Claude wrote the work, Claude when Codex did.
#                   Run by hand, it uses Codex if installed, otherwise Claude.
#   REVIEWER=codex  OpenAI's Codex CLI (signs in with your ChatGPT account)
#   REVIEWER=claude A separate Claude session on another model
#   REVIEWER=both   Codex AND Claude, one after the other. High-risk changes
#                   need a PASS from both before the human can merge them.
#                   Claude defaults to the stronger model "opus" here.
# Optional: REVIEW_MODEL=<name> picks the model for that reviewer (with
#   REVIEWER=both, REVIEW_MODEL sets Claude's model and REVIEW_CODEX_MODEL sets Codex's).
#   Examples:  REVIEWER=claude REVIEW_MODEL=opus scripts/review.sh
#              REVIEWER=codex scripts/review.sh
#
# Each review is printed and also saved in .reviews/ (ignored by git), with a
# "Commit:" header holding the full id of the commit reviewed. The merge script
# only accepts a review of the exact commit it is merging.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

if [[ "$(git rev-parse --absolute-git-dir)" == "$(git rev-parse --path-format=absolute --git-common-dir)" ]]; then
  echo "Run this from inside a task worktree (.worktrees/<task-name>)."
  exit 1
fi

base="main"
git rev-parse --verify --quiet main >/dev/null || base="master"
branch="$(git branch --show-current)"
sha="$(git rev-parse --short HEAD)"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Commit your changes first: the reviewer reviews commits, not loose edits."
  exit 1
fi
if [[ "$(git rev-list --count "$base..HEAD")" == "0" ]]; then
  echo "No commits on $branch yet, so there is nothing to review."
  exit 1
fi

reviewer="${REVIEWER:-auto}"
if [[ "$reviewer" == "auto" ]]; then
  # Pick a reviewer from the other company than the author when we can tell.
  if [[ -n "${CLAUDECODE:-}" ]]; then
    # Claude Code is running this, so Claude wrote the work: prefer Codex.
    if command -v codex >/dev/null 2>&1; then reviewer="codex"; else reviewer="claude"; fi
  elif [[ -n "${CODEX_SANDBOX_NETWORK_DISABLED:-}${CODEX_SANDBOX:-}" ]]; then
    # Codex is running this, so Codex wrote the work: use Claude.
    reviewer="claude"
  else
    # A person ran it from a terminal: Codex if available, otherwise Claude.
    if command -v codex >/dev/null 2>&1; then reviewer="codex"; else reviewer="claude"; fi
  fi
fi

mkdir -p .reviews
full_sha="$(git rev-parse HEAD)"
prompt="You are a code reviewer. Review the changes on branch '$branch' compared
to '$base' (use: git diff $base...$full_sha and git log $base..$full_sha). You are
read-only: do not try to edit anything.

Check for: bugs, missing error handling, security problems, secrets in the
code, Kubernetes YAML mistakes, tests that were weakened or skipped, and
anything that breaks the rules in CLAUDE.md.

The author has already run scripts/check.sh and it passed, and the human runs
it again before merging. Don't rerun the checks or tests yourself: your review
sandbox may not have the same tools installed. Judge the code by reading it.
If something about your own environment stops you checking a point, mention
it under SHOULD FIX as a note. MUST FIX is only for real problems in the
changes themselves.

Reply in this format:
VERDICT: PASS or FAIL
MUST FIX: numbered list (empty if none). Each item: file, line, problem, fix.
SHOULD FIX: numbered list of smaller suggestions.
SUMMARY: two or three sentences."

# run_review <codex|claude> <model or empty>
run_review() {
  local kind="$1" model="$2"
  local out=".reviews/${branch//\//-}-$sha-$kind.md"
  local rc=0
  # Written under another name and renamed only when complete and checked, so a
  # half-finished review can never satisfy the merge gate.
  local part="$out.partial"
  rm -f "$part"
  case "$kind" in
    codex)
      command -v codex >/dev/null 2>&1 || { echo "Codex isn't installed. Install it, or run with REVIEWER=claude."; return 1; }
      if ! codex login status >/dev/null 2>&1; then
        echo "Codex isn't signed in. Run 'codex login' once (it uses your ChatGPT account), or run with REVIEWER=claude."
        return 1
      fi
      echo "Reviewing $branch @ $sha with Codex${model:+ ($model)}..."
      local model_args=()
      [[ -n "$model" ]] && model_args=(-m "$model")
      # -s read-only: Codex may read files and run read-only commands, never edit.
      # --ephemeral: don't keep a Codex session file for this one-off review.
      # </dev/null: without it, Codex waits for extra input when no terminal is
      #   attached (for example when an agent runs this script).
      # Progress goes to stderr; only the final review goes to stdout (and the file).
      # The header is written first, then a blank line, then the review.
      # `|| rc=$?` keeps the reviewer's failure (pipefail) instead of losing it
      # when this function is called inside an `||` list.
      { echo "Reviewer: Codex${model:+ ($model)}"; echo "Commit: $full_sha"; echo; \
        codex exec -s read-only --ephemeral "${model_args[@]}" "$prompt" </dev/null; } | tee "$part" || rc=$?
      ;;
    claude)
      model="${model:-sonnet}"
      echo "Reviewing $branch @ $sha with Claude ($model)..."
      # AGENT_RULES_OFF=1: the reviewer is read-only, so the Stop gate isn't needed.
      { echo "Reviewer: Claude ($model)"; echo "Commit: $full_sha"; echo; \
        AGENT_RULES_OFF=1 claude -p "$prompt" \
          --model "$model" \
          --allowedTools "Read" "Grep" "Glob" "Bash(git diff *)" "Bash(git log *)" "Bash(git show *)" \
          --disallowedTools "Edit" "Write" "NotebookEdit"; } | tee "$part" || rc=$?
      ;;
  esac
  echo
  if [[ "$rc" != "0" ]]; then
    rm -f "$part"
    echo "The $kind review failed (exit $rc), so no review was saved."
    return 1
  fi
  # The review is only about the commit in its header: if the checkout moved
  # while the reviewer ran, it does not count.
  if [[ "$(git rev-parse HEAD)" != "$full_sha" ]]; then
    rm -f "$part"
    echo "HEAD changed during the $kind review, so it was discarded. Run it again."
    return 1
  fi
  mv -f "$part" "$out"   # only now can the merge gate see it
  echo "Saved to $out"
}

case "$reviewer" in
  codex)  run_review codex "${REVIEW_MODEL:-}" ;;
  claude) run_review claude "${REVIEW_MODEL:-}" ;;
  both)
    # Run both even if the first fails, so one outage doesn't hide the other
    # review; fail overall if either could not run.
    status=0
    run_review codex "${REVIEW_CODEX_MODEL:-}" || status=1
    run_review claude "${REVIEW_MODEL:-opus}" || status=1
    exit $status
    ;;
  *)
    echo "Unknown REVIEWER '$reviewer'. Use auto, codex, claude or both."
    exit 1
    ;;
esac
