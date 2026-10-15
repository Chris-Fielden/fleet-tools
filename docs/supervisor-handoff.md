# Supervisor handoff format (spec only)

Source: ID-31 DESIGN 3/3 ("handoff format"). This is a specification. Nothing
described here is built: there is no parser, ledger or reporting yet. Those
wait for the human to settle the open decision at the end of this file.

A handoff is a structured plan that a supervisor agent hands to the fleet. It
is plain text with a fixed set of sections. It does not start work by itself:
it enters the ticket store as one **proposed investigate ticket**, which a
human reads and approves like any other proposal (see `docs/ticket-spec.md`).

## Sections

Exactly these five sections, each as a top-level heading, in this order:
`GOAL`, `STEPS`, `LIMITS`, `STOP IF`, `NEVER`.

### GOAL

One short paragraph: the outcome wanted, and how a human can tell it is
reached. No instructions to skip checks, touch secrets or work outside the
repos named in STEPS.

### STEPS

An ordered list. Each step has exactly these keys:

| key | meaning |
|---|---|
| `id` | Short label, unique within the handoff (`S1`, `S2`, ...). |
| `title` | One line, what the step does. |
| `type` | A ticket type from `ticket-spec.md` (`build`, `review`, `fix`, `question`, `investigate`). |
| `repo` | The repo the step works in. |
| `branch` | The task branch the step will use (`task/<name>`). |
| `acceptance test` | The observable check that says the step is done (a command or a plain, checkable statement). |
| `depends_on` | Informational only: the `id`s this step follows. It does not schedule anything. |
| `risk guess` | `low`, `medium` or `high`. A guess only: the real level comes from `risk-rules.json` when the ticket is filed. |

Sequencing: **step N+1 is filed only after step N is done.** There is no
dependency field that the system acts on; `depends_on` documents the intent
and nothing more. Filing the next step is a separate, later action, after the
previous step's ticket is `done`.

### LIMITS

The bounds the plan must stay inside (for example the maximum number of
steps, repos allowed, time or round budget). Limits only narrow what is
allowed; they never grant anything.

### STOP IF

Conditions that halt the plan and send it back to the human (for example a
step failing review twice, a surprise high-risk file, an acceptance test that
cannot be run). When any listed condition holds, no further step is filed.

### NEVER

A **fixed list**, the same for every handoff. It is not editable per
handoff: a handoff that adds, removes or rewords entries is rejected. The
list is the MUST NEVER list from the ID-31 design (see "Lists from the
design" below).

## Validation

A handoff is rejected, whole, when:

- any of the five sections is missing, duplicated or out of order;
- any section, or any key inside a STEPS entry, is not in the lists above
  (unknown keys are a rejection, not ignored);
- a STEPS entry lacks a key, or `id`s repeat;
- NEVER differs from the fixed list.

There is no partial acceptance and no "fix up and continue". A rejection
returns a plain message naming the first problem.

## Entry point

An accepted handoff becomes one ticket: type `investigate`, status
`proposed`, body = the handoff text. Nothing is created per step at that
point. A human approves, and each step is filed later, one at a time, as
described under STEPS.

## Lists from the design

The MAY and MUST NEVER lists live in ID-31 DESIGN, which is not present in
this repository or in this task's ticket. They are deliberately **not
reproduced or paraphrased here**, so this file cannot drift from, or invent
beyond, the design.

- **MAY**: TODO, copy verbatim from ID-31 DESIGN 3/3.
- **MUST NEVER** (also the fixed NEVER section): TODO, copy verbatim from
  ID-31 DESIGN 3/3.

Until both are filled in by someone with the design in hand, the spec is not
complete.

## Open human decision (from ID-31)

The open decision recorded in ID-31 is **not decided here**. This spec is
written so that either outcome fits the format above.

- **Decision:** TODO, state the question exactly as written in ID-31.
- **Status:** open, owner: the human.
- **Blocks:** the parser, ledger and reporting builds.
