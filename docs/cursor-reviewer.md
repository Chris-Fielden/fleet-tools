# Cursor as the third reviewer (BD-136)

Cursor reviews a passing tip and, **only with proof**, its vote counts as the third company
(`cursor`, next to `openai` and `anthropic`). The BD-125 rule is unchanged: 2 of 3 companies must
PASS, and a high-risk change still needs a Claude opus or fable PASS.

Agents never launch Cursor and never see its API key. The review is run by the review watcher
(`fleet/cursor_review.py`), a fleet process, never by an agent and never by `scripts/review.sh`.

## One review

1. The exact commit is copied to a fresh temp folder without `.claude`, `.codex`, `.cursor`,
   `CLAUDE.md` and `AGENTS.md`; the diff against main goes in `.fleet-review/changes.diff`.
2. An empty throwaway `HOME`. A native `.cursor/hooks.json` (`fleet/cursor_hook.py`) denies shell
   and every tool except Read (Grep/Glob/LS only with every path inside the copy; a tool with no
   checkable path is denied), allows reads, and keeps only the model name the read hook saw. Hook payloads (which
   carry the account email) are never stored or logged.
3. `agent -p --trust --mode ask --model composer-2.5 --output-format json`, with `CURSOR_API_KEY`
   read from `secrets/cursor-api-key` in the fleet store and given to that one command only.
4. The fleet process saves `.reviews/<branch>-<sha>-cursor.md` with the header
   `Reviewer: Cursor (composer-2.5)`, `Commit:`, `Model:` (as seen by the hook), `Counted: yes|no`.
   Output with no `VERDICT:` line is no review. Email addresses and the key are scrubbed.

Turn it on with `"cursor_review": true` in settings; it runs on a tip that already has a PASS.

## When it counts

Only when **all** hold: the model the hook saw starts with `composer`; the probe result file
`cursor-probe-result.json` in the fleet store says probes a-f passed; and its `cli_version` equals
`agent --version` now. Otherwise the review is recorded in `.reviews/` and as a ticket note, no vote
is recorded, and one plain ping names the reason (usually: run the probe again).

## The probe (the human runs it)

`scripts/cursor-probe.sh` keeps P1-P12 and adds, per CLI version. Every Cursor command in it runs
on the key file alone (key in that one command's environment, empty HOME); the browser sign-in is
never used, and the status line in the report is information only, never proof that auth works.
The model is `PROBE_MODEL`, else composer-2.5. Each run writes a new
`probe-<version>-<run time>.md` holding each a-f reply (key and email scrubbed):

- a: the hooks block shell and write while read works
- b: the read hook records the model and allows the read
- c: ask mode refuses an edit with the hooks removed
- d: an agent environment cannot read the key file (private mode; the Read and shell guards refuse it)
- e: with the empty HOME auth still works, and a skill planted in the real `~/.claude/skills`
  never appears (the prompt never names the skill; only its token or name in the reply fails it)
- f: a canary file outside the copy cannot be found with grep, glob or search by absolute path

It writes `cursor-probe-result.json`: CLI version, pass/fail per probe, no payloads, no email, no key.

## Tests

`tests/test_cursor_review.py` and `tests/test_cursor_probe.py` run against a fake CLI
(`tests/fake_cursor.py`): no real Cursor, no network.
