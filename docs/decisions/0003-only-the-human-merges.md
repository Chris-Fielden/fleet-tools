# 0003. Only the human merges

- **Date:** 2026-10-15
- **Status:** accepted
- **Decided by:** human

## Context

Agents write and review code, and deploys happen by merging to `main` and
letting ArgoCD apply it. Someone has to own the decision to release.

## Decision

Only the human merges to `main`, with the `ship` command. Agents commit on
their task branch, pass the checks, get the other model's review, and stop.
An agent never approves or merges its own work.

## Consequences

- Every release has a human decision behind it.
- High-risk branches need both reviewers to PASS before the human can merge.
- Agents are blocked by hooks from pushing to `main` or merging.
- Merging is a bottleneck. We accept that for the safety it buys.

## Alternatives considered

- Auto-merge on green checks and a reviewer PASS: removes the human from
  the release decision.
