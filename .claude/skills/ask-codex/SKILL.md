---
name: ask-codex
description: Get a read-only second opinion from OpenAI's Codex on a design choice, a tricky bug, a failing test, or a plan before committing to it. Use when the user says "ask Codex", "check with Codex", "get a second opinion", or when you are unsure between approaches.
---

# Ask Codex for a second opinion

Codex is a coding agent from a different company (OpenAI). It catches
different mistakes than you do, so its view is most useful exactly when you
feel confident but haven't verified.

## When to use it

- The user asks you to check with Codex or get a second opinion.
- You're choosing between two designs and the choice is hard to undo.
- A bug or failing test has resisted two honest attempts to fix it.
- Before a change that touches Kubernetes manifests, secrets handling, or
  anything security-related.

Don't use it for simple questions you can answer by reading the code. Every
call uses part of the user's ChatGPT plan allowance.

## How

Run the helper from anywhere in the repo, with a self-contained question:

```bash
scripts/ask-codex.sh "In src/weather.py, fetch_forecast() retries 5 times with no backoff. Is that a problem for the NWS API, and what would you change?"
```

For long input (an error log, a diff), pipe it in:

```bash
git diff main...HEAD | scripts/ask-codex.sh
```

Write the question so it stands alone: name the files, say what you're
trying to do, and say what you already tried. Codex can read the repo, but
it doesn't see this conversation.

## After Codex answers

- Tell the user what Codex said, in a sentence or two, and whether you agree.
- If Codex found a real problem, fix it. If you disagree, say why.
- Codex is read-only here. It can't change files, and its answer isn't an
  approval to merge. The review step (`scripts/review.sh`) and the human
  still decide that.

If the script says Codex isn't installed or isn't signed in, tell the user
and carry on without it.
