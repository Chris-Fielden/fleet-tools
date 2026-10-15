# 0002. Claude and Codex never talk directly

- **Date:** 2026-10-15
- **Status:** accepted
- **Decided by:** human

## Context

Claude and Codex both work in this repository and review each other. If they
could message each other freely, one could steer or persuade the other, and
the human would lose sight of what was said.

## Decision

Agents do not send messages to each other. Cross-model contact goes only
through the read-only wrappers (the review script, `ask-codex` and
`ask-claude`), and the asking agent tells the human what the other said.

## Consequences

- Every cross-model exchange is a bounded, read-only consult with a visible
  record.
- A reviewer can't be talked into a PASS by the author.
- Coordination between tasks goes through tickets and the human.

## Alternatives considered

- Direct agent-to-agent chat: faster, but unreviewable and open to collusion
  or prompt injection passed between agents.
