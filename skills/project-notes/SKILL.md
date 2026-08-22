---
name: project-notes
description: Plan and execute multi-step builds as durable on-disk project notes that survive context limits and new sessions. Read this when a request trips the project gate (>3 files, >5 sequential tool-steps, research-then-build, or the user says project/build), when resuming an in_progress project note, or when you need the project-note format, heaviness/turn-boundary rules, claim fields, or self-check discipline.
---

# Project notes (dinomem-neuron)

A **project note** is a context-bounded, step-by-step executor: a durable plan on
disk that survives context limits and brand-new sessions. When work is too big
for one turn, write one of these so any later turn (or the Project Advancer cron)
can resume from the note alone.

## When to make one (the estimate gate)

Decide **at request time, before starting**:

- **Gate:** `>3 files` OR `>5 sequential tool-steps` OR research-then-build OR the
  user says "project"/"build".
- **Method:** heuristic gate as a floor, your judgment can override upward. Trip
  the gate → make a project note. Otherwise → normal one-turn work.
- **Upgrade rule:** enumerate the steps FIRST. If `>1` digital/verifiable step →
  `type: project`. Never skip enumeration. Physical/non-verifiable tasks →
  `type: task_bound` (see the memory-pinning skill), not project.

## Pre-flight clarity check (run BEFORE writing the note)

The estimate gate decides IF a project note; this decides you're building the
RIGHT one. The verify/done_when chain proves you built what you decided — it
cannot catch that you decided wrong. So before enumerating steps:

- **Enumerate load-bearing assumptions.** List the interpretations your plan
  silently depends on (which file/API/scope/target, what "done" means).
- **Fork test:** if `>1` material interpretation exists OR a load-bearing
  assumption is unverified → STOP. Surface the fork + your default pick + the
  cost-if-wrong, and get ONE confirmation before writing steps. Do NOT pick
  silently and execute.
- **Record it as ruling #0:** the confirmed interpretation goes into `rulings:`
  as the first entry (`"chose X over Y — user confirmed — cost if wrong: rebuild"`),
  so a blind resume inherits the decision, not just the steps.
- **Cheap-fix carve-out:** a single unambiguous reading, or a trivial/reversible
  task → skip; don't manufacture questions. This gate is for genuine forks only.

## Format

```
# Project <name>
type: project
status: in_progress | done
date: YYYY-MM-DD
done_when: <final artifact / all steps [x]>
stale_after: YYYY-MM-DD
current_step: <n>
claimed_by: <run_id or blank>
claimed_at: <ISO timestamp or blank>
steps:
  - [ ] 1. <step> -- done_when: <check>
  - [ ] 2. ...
resume_state: <paths/decisions/assumptions the next turn needs to resume blind>
rulings:
  # append entries here when the Advancer makes an in-flight decision:
  # - "<decision> — <why> — <cost if wrong>"
```

- **`claimed_by`** — set by Project Advancer / Improver on claim; cleared on step
  complete, checkpoint, or safety-gate pause. Stale if `claimed_at` is older than
  its window.
- **`claimed_at`** — UTC ISO8601 (trailing `Z`) of the claim write; used to detect
  stale claims. Windows: **live-session 120 min**, other claimant **30 min**. A
  future-dated stamp is invalid/stale. (Live sessions: see the note-claim skill
  for the write side.)

## Heaviness gate (how big is a step?)

- **heaviness** = execution load of a step (files touched, LOC, test/debug surface).
- **heavy:** write+test a whole tool/file · multi-file change · research-then-build
  · a full write→run→debug→verify cycle.
- **light:** tiny edit · config · doc · one-liner.
- **Rule:** a heavy step gets its **own turn**; light steps can be **batched** a few
  per turn.

## Execute

- **turn_boundary:** the heaviness of the **NEXT** step decides continue-vs-stop.
  Heavy next → stop, start it fresh next turn. Light next → do it, continue.
- **gate:** do NOT start a heavy step just to batch it — a heavy step gets its own turn.
- **heavy_step:** checkpoint (write `resume_state`), stop cleanly, tell the user
  "step n/m done"; resume next turn/cron from `current_step`.
- **light_step:** mark the step `[x]`, update `resume_state`, advance `current_step`,
  continue to the next light step in the same turn.
- **spawn (optional):** if the next step is heavy AND remaining steps `> 2` AND
  `sessions_spawn` is available → spawn a subagent with `task` = remaining steps +
  `resume_state`; it works in clean context; the parent waits for the completion event.
- **resume:** a new session reads the note, jumps to `current_step`, and resumes
  from `resume_state` ONLY — no reliance on chat history.

**Mandatory:** after every step, rewrite `resume_state` so the note is
self-sufficient. This is the reliability linchpin — the open-notes bootstrap hook
surfaces the note, but only a concrete `resume_state` lets a blind resume work.

## Note-edit safety (don't silently lose a write)

The `edit` tool applies a multi-edit batch **atomically** — if any one `oldText`
fails to match, the WHOLE batch rolls back and nothing changes. The tool returns
an error, but a note you *believe* you updated is then silently stale (Advancer
resumes from the wrong step, or a done note never closes).

- **Trap:** chaining edits where a later `oldText` anchor overlaps/depends on text
  an earlier edit in the same batch already changed, or reusing an anchor a prior
  applied edit consumed → stale anchor → full rollback.
- **Rules:**
  - NEVER treat a note write as done until you have READ the tool's success/error
    result; an error = zero changes applied.
  - After any batch of `>1` edit to a note, grep/re-read the changed lines
    (`status`, `current_step`, `resume_state`, `claimed_at`) to CONFIRM before
    advancing or announcing.
  - Keep batch edits to DISJOINT independent regions; if edits overlap or depend on
    each other, split into sequential calls and verify between them.
  - On rollback: re-read the note FRESH (anchors may reflect a partial earlier
    success), rebuild `oldText` from current content, retry.

## Plan self-review (after writing, before executing)

After writing a project note's steps and before executing step 1, run this
checklist inline. Fix issues now — a weak plan is the #1 source of Advancer
stalls. Self-review takes one pass; no subagent needed.

1. **Placeholder scan** — no step may contain "TBD", "TODO", "similar to step
   N", "add appropriate X", or any vague action without a concrete method.
   Every step must state HOW, not just WHAT.
2. **Step completeness** — each step has a concrete action and a locally
   verifiable `done_when` (a command, a grep, a file-exists check). Narrative
   done_when (`"when it works"`) fails here.
3. **Interface consistency** — if step N produces a file/value consumed by
   step N+1, the path/name must match exactly in both steps.
4. **done_when verifiability** — the top-level `done_when:` must be runnable
   as a shell command (exit 0 = done). If it can't be checked by a script,
   rewrite it until it can.

Fix any issue inline before writing `current_step: 1`. A plan that fails
self-review is not ready to execute — fix it first.

## Self-check (before stopping at any checkpoint)

When `status: in_progress`, before stopping verify: `type: project`,
`status: in_progress`, `current_step` matches the lowest unchecked step, and
`resume_state` is non-empty + concrete. On fail: fix the note THIS turn before
stopping — a malformed note is a silent death (the Advancer skips it).

## Driver & resolve

- **driver:** hybrid — the user may say "continue"; otherwise the **Project
  Advancer** cron auto-advances `in_progress` notes unattended (announces progress,
  pauses on destructive/external steps).
- **rulings ledger:** on resume, the Advancer reads `rulings:` BEFORE `resume_state`.
  When making any in-flight decision (ambiguity, blocker workaround, scope call),
  append a ruling entry BEFORE continuing: `"<decision> — <why> — <cost if wrong>"`.
  After compaction or crash, trust `rulings:` + git log over recollection.
- **evidence gate (done_when):** the Advancer satisfies `done_when` only by running
  the verification command and reading its actual output. Self-report ("I implemented
  it") does NOT satisfy the gate. No `verified: true` stamp without fresh evidence.
- **awaiting_approval_reverify:** for a safety-gated final step (push / external),
  at cold start re-verify the artifact exists (`git -C <repo> rev-parse HEAD == @{u}`);
  if already done, close the note instead of re-asking.
- **resolve:** when `done_when` is met, the **Daily Note Review** cron (06:00 UTC,
  claim-aware) deletes/promotes it via the normal pipeline.
- **done_when verifiability:** every `done_when` must be locally checkable (file
  exists / grep / exit 0 / `git rev-parse HEAD == @{u}`). Narrative `done_when`
  never auto-closes.
- **fix_rounds escalation (Improver):** notes carry an optional `fix_rounds: N`
  field (default 0). The Improver increments it each retry cycle. At `N >= 3`:
  flag for owner review instead of silently retrying (add a comment to the note
  explaining what's stuck). At `N >= 5`: set `status: stalled` with a reason line
  — the Advancer and Improver both skip stalled notes until the owner intervenes.
