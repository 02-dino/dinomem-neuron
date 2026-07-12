---
name: note-claim
description: Claim a dinomem project/task note before editing it from a live interactive session, so the Advancer/Improver/Deleter crons back off and don't double-write. Read this before your first multi-turn or build-related edit to a memory/_note_*.md that a cron could also touch.
---

# Note claiming (dinomem-neuron)

The Advancer / Improver / Deleter crons READ a note's `claimed_by` / `claimed_at`
and back off a fresh claim (live-session window: 120 min) — but **nothing writes
that claim for a live interactive session**. You (the live agent) are the **write
side** of the protocol. Skip it and a cron can grab the same note mid-edit and
double-write or re-nag.

## When to use

Before a LIVE interactive session is about to EDIT/write a project or task
`_note_*.md` that a cron (Advancer/Improver/Deleter) could also touch.

**Skip when:** read-only note lookups · `_pin_` files · a note you create AND
finish in one edit with no cron overlap.

## How

`claim_note.sh` is the **only** writer of `claimed_by`/`claimed_at` — never
hand-write those fields with the edit tool (that bypasses the shared lock and
re-opens the clobber race).

- **Before the first edit:**
  ```bash
  bash $WS/scripts/claim_note.sh claim <note> live-session-<agent-id>
  ```
  Exit `3` = another job holds it fresh → **do not fight**; pick another note or
  tell the user. Exit `0` = you hold it atomically → proceed.

- **Each later turn that resumes the SAME note:**
  ```bash
  bash $WS/scripts/claim_note.sh refresh <note> live-session-<agent-id>
  ```

- **When done / paused / abandoning:**
  ```bash
  bash $WS/scripts/claim_note.sh release <note>
  ```

## Why it matters

The claim lock is otherwise only honored cron-vs-cron. Without the live-session
write side, the (up to 4h) Advancer can wake mid-conversation and double-edit the
same note you're working. Claiming makes the protocol enforceable instead of
documentation-only. Mandatory before multi-turn or build-related note edits.
