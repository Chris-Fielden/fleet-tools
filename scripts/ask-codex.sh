#!/usr/bin/env bash
# Ask OpenAI's Codex for a read-only second opinion. Used by Claude (through
# the ask-codex skill) or by you directly.
#
# Usage:  scripts/ask-codex.sh "Is this retry logic in weather.py safe?"
#         echo "long question or pasted error" | scripts/ask-codex.sh
#
# Codex can read files and run read-only commands in this repo, but it cannot
# change anything. Its answer is printed to the terminal.
# Optional: ASK_MODEL=<name> picks the Codex model.
set -euo pipefail

question="${1:-}"
if [[ -z "$question" && ! -t 0 ]]; then
  question="$(cat)"
fi
if [[ -z "$question" ]]; then
  echo 'Usage: scripts/ask-codex.sh "your question"'
  exit 1
fi

command -v codex >/dev/null 2>&1 || { echo "Codex isn't installed (see SETUP-GUIDE.md, Step 3b)."; exit 1; }
if ! codex login status >/dev/null 2>&1; then
  echo "Codex isn't signed in. Run 'codex login' once (it uses your ChatGPT account)."
  exit 1
fi

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

prompt="Another AI coding agent (Claude) working in this repository is asking you
for a second opinion. You are read-only: look at any files you need, but do
not try to change anything. Answer directly and concisely. If you disagree
with an assumption in the question, say so and explain why. If you are
unsure, say what you would check.

Question:
$question"

model_args=()
[[ -n "${ASK_MODEL:-}" ]] && model_args=(-m "$ASK_MODEL")

# -s read-only: can look, can't touch.  --ephemeral: no saved session.
# </dev/null: stops Codex waiting for extra input when no terminal is attached.
# Codex's progress goes to stderr; hide it so only the answer comes back.
codex exec -s read-only --ephemeral "${model_args[@]}" "$prompt" </dev/null 2>/dev/null
