# Incident: <short title>

- **Date:** YYYY-MM-DD
- **Severity:** low | medium | high
- **Owner:** <human or agent writing this up>
- **Status:** open | resolved

## When to write one

Write an incident record when any of these is true:

- A change reached `main` that broke checks, the cluster or another task.
- A secret, token or private key was exposed, or may have been.
- A hook, reviewer or other guardrail was bypassed or failed silently.
- An agent acted outside its task worktree, or on the cluster directly.
- Work was lost, or a deploy had to be rolled back.

## Timeline

All times UTC. Facts only.

| Time  | Event                              |
| ----- | ---------------------------------- |
| HH:MM | What happened, or what was noticed |

## Cause

What went wrong, and why the guardrails did not stop it.

## Fix

What was done to restore things, with commit or ticket IDs.

## Follow-up tickets

| Ticket | What it prevents or improves |
| ------ | ---------------------------- |
| BD-NNN | ...                          |
