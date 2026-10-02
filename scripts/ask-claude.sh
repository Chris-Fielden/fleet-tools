#!/usr/bin/env bash
# Ask Claude for a read-only second opinion. Used by Codex (see AGENTS.md)
# or by you directly.
#
# Usage:  scripts/ask-claude.sh "Is this retry logic in weather.py safe?"
#         echo "long question or pasted error" | scripts/ask-claude.sh
#
# Claude can read files and git history here, but it cannot edit anything.
# Optional: ASK_MODEL=<name> picks the Claude model (default: sonnet).
set -euo pipefail

question="${1:-}"
if [[ -z "$question" && ! -t 0 ]]; then
  question="$(cat)"
fi
if [[ -z "$question" ]]; then
  echo 'Usage: scripts/ask-claude.sh "your question"'
  exit 1
fi

command -v claude >/dev/null 2>&1 || { echo "Claude Code isn't installed (see SETUP-GUIDE.md, Step 3)."; exit 1; }

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

prompt="Another AI coding agent (OpenAI's Codex) working in this repository is
asking you for a second opinion. You are read-only: look at any files you
need, but do not try to change anything. Answer directly and concisely. If
you disagree with an assumption in the question, say so and explain why. If
you are unsure, say what you would check.

Question:
$question"

# AGENT_RULES_OFF=1: this is a read-only consult, so the Stop gate isn't needed.
AGENT_RULES_OFF=1 claude -p "$prompt" \
  --model "${ASK_MODEL:-sonnet}" \
  --allowedTools "Read" "Grep" "Glob" "Bash(git diff *)" "Bash(git log *)" "Bash(git show *)" \
  --disallowedTools "Edit" "Write" "NotebookEdit" </dev/null
