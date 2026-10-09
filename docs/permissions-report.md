# Agent permission rules (EM-22)

Source of truth: `fleet/permissions.py`. `.claude/settings.json` must equal it
(tested); research folders get `RESEARCH_ALLOW` plus the same deny list.
Docs re-read 2026-10-09: https://code.claude.com/docs/en/permissions and
/permission-modes. Only `permissions.allow` / `permissions.deny` are used.

## Threat model

An allow rule lets an agent run that command without classifier review. What
remains is the PreToolUse guard (`guard_bash.py`, regex based: it stops
mistakes, not a determined program) and the deny list. Not defended: injected
ticket or repo text steering an agent into an allowed command with hostile
arguments. So each rule is exact or narrow, tied to an observed prompt, and
listed below. Auto mode drops broad rules (`Bash(*)`, wildcarded interpreters,
package-manager run, `Agent`, `Monitor`) and keeps narrow ones, so none of those are used.

## Rules

Evidence is a ticket or note, not session-log counts (agents may not read session logs).

| Rule | Evidence | Why it is safe enough |
|---|---|---|
| `git add -A` | EM-11 spec: agents must commit before stopping (CLAUDE.md rule 2) | Exact; stages only. The guard blocks writes from the main checkout. |
| `git commit -m *` | same | `-m` first, any later flags (e.g. `--amend`, `--no-verify`, `-n`) would match, so they are in the deny list (a message containing ` -n ` is denied too); commits locally. Per the permissions doc (re-read 2026-10-09), separators split a command into subcommands that must each match, so a message with unquoted `;`, `&`, `\|` or a newline may prompt; not verified in a live session. |
| `scripts/check.sh` | EM-11 spec, CLAUDE.md rule 2 | Exact; the project's own check. |
| `scripts/review.sh` | EM-11 spec, CLAUDE.md rule 3 | Exact; the mandated reviewer call (sends the branch diff to the other model, as the rulebook requires). |
| `ticket note *` | EM-11 spec; ID-11 finding: research agents stall on it | Writes a note on any ticket ID (not tied to the agent's own). Notes with unquoted separators or substitutions may prompt (per the docs; not observed live; not fixed). |
| `ticket show *` | same | Read-only. |

Research folders get only the two `ticket` rules (for any ticket ID; tying `ticket note` to the agent's own ticket is a possible later guard change).

`git add <path>`, `git add .` and single-file pytest runs also prompt (only `git add -A` is allowed); watch that unattended builders do not stall on them. The allow-list tests show only that patterns do not trivially match human-only commands, not that the list is safe.

`scripts/review.sh` and `scripts/check.sh` are exact matches: forms with arguments or a prefix, such as `REVIEWER=both scripts/review.sh`, are not covered and may prompt or go to the auto-mode classifier.

## Removed from the old list

- `cat`, `head`, `tail`, `ls`, `wc`, `pwd`, `git status|diff|log|show`: the built-in read-only set already runs them; `git diff|log|show *` also accept `--output=FILE`.
- `python3 -m pytest *`: runs repo code and accepts `-p` plugins; `scripts/check.sh` covers it. Trade-off: a single-file pytest run now prompts.
- `scripts/ask-codex.sh *`: sends free text to an external model (network egress outside the allowlist).
- `git add *`, `git commit *`, `scripts/check.sh *`, `scripts/review.sh *`: wider than the evidence (any flags or arguments).

## Never allowed

The human-only commands (the guard's list), `ticket close|approve|sync|priority|fix-titles`,
`fleet <verbs>`, writes to `~/.fleet`, the finish script, `git push` (in the deny list),
edits to `.claude/hooks/**` and `settings.json`. The guard also refuses any command
text that mentions the finish script, even inside a note body.
