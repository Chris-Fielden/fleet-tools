"""The Bash allow rules agents run with, in one place.

.claude/settings.json (task worktrees) and the settings written for research
folders (launch.research_folder) both come from here; a test keeps the repo
file equal to these lists. Rules follow
https://code.claude.com/docs/en/permissions and /permission-modes (re-read
2026-10-09): only exact or narrow `Bash(...)` rules, because auto mode drops
broad ones. Every rule is justified in docs/permissions-report.md.
"""

# Task worktrees. Read-only commands (ls, cat, head, tail, wc, git status/diff/log/show)
# are already allowed by Claude Code itself, so they are not listed.
ALLOW = [
    "Bash(git add -A)",
    "Bash(git commit -m *)",
    "Bash(scripts/check.sh)",
    "Bash(scripts/review.sh)",
    "Bash(ticket note *)",
    "Bash(ticket show *)",
]

DENY = [
    "Bash(git push *)",
    "Bash(git push)",
    # `git commit -m *` also matches later flags; these keep commits plain and reviewable.
    "Bash(git commit * --no-verify*)",
    "Bash(git commit * -n *)",
    "Bash(git commit * -n)",
    "Bash(git commit * --amend*)",
    # Agents never drive herdr panes.
    "Bash(herdr *)",
    "Bash(herdr)",
    # Agents never wait on a question; see CLAUDE.md section 8.
    "AskUserQuestion",
]

# Research folders (not a repo, only hooks + statusLine today): ticket access only.
RESEARCH_ALLOW = [
    "Bash(ticket note *)",
    "Bash(ticket show *)",
]


def research_permissions() -> dict:
    return {"allow": list(RESEARCH_ALLOW), "deny": list(DENY)}
