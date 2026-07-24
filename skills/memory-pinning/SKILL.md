---
name: memory-pinning
description: Create durable dinomem memory — permanent _pin_ facts and transient _note_ todos/reminders/projects — with neuron's semantic dedup gate and auto-note triggers. Read this when the user says remember/pin/note this, when you commit to deferred work, or when a task/reminder/time-bound item arises and you need the file format, slug rules, done_when discipline, and the pre-write dedup check.
---

# Memory pinning (dinomem-neuron)

dinomem persists two kinds of durable memory as files in `memory/`. This is the
**neuron** version — it adds a semantic **dedup gate** and explicit **auto-note
triggers** on top of the base memory-pinning rules, and it overwrites the base
skill of the same name.

## When to use

- User says "remember this", "pin this", "note that", "don't forget" (any phrasing).
- Your own reply commits to deferred/external work not finished this turn.
- A todo, reminder, planned task, or time-bound item arises.
- A multi-step build begins → use a **project note** (see the `project-notes` skill).

If it's a fleeting/ambiguous personal detail, **ask before pinning**.

## Two kinds

### 1. Permanent facts → `_pin_`

For durable facts, preferences, decisions — things true across sessions.

- **Trigger:** a permanent fact, or the user emphasizes importance. Uncertain? ask first.
- **File:** `memory/_pin_<slug>.md` (slug: lowercase-hyphens, max ~30).
- **Format:**
  ```
  # Title

  <content>
  ```
- **Long documents** (legal text, manuals, contracts): don't inline — ingest via
  `docs/<slug>.md` → `docs_ingest.py` (see the `retrieval-routing` skill).

### 2. Transient work → `_note_`

For todos, reminders, planned tasks, time-bound items.

- **Trigger:** a todo / reminder / planned task / time-bound item. Uncertain? ask first.
- **File:** `memory/_note_<slug>.md` (slug: lowercase-hyphens, max ~30).
- **Format:**
  ```
  # Title
  type: task_bound | time_bound
  status: pending | done
  date: YYYY-MM-DD
  done_when: <checkable — file exists / feature shipped>
  stale_after: YYYY-MM-DD
  <content>
  ```

**Schema rules:**

| Field | Meaning |
| ----- | ------- |
| `type` | `task_bound` resolves via `done_when`; `time_bound` is a date-based reminder. |
| `done_when` | Concrete artifact check; the lever the resolver cron uses to flip `status: done` and delete the note (task_bound only). |
| `stale_after` | Fallback GC for abandoned notes. Default `date + 30d` (reminders `date + 7d`); may override. |
| `status` | Flip to `done` **only** when `done_when` is verified; otherwise `pending`. |

**`done_when` MUST be locally verifiable** — the resolver runs against local state
only (no chat history, no network). Good: `file exists`, `grep`, `exit 0`; git
push → `git -C <repo> rev-parse HEAD == @{u}`. Never write narrative done_when
("pushed to repo", "told the user") — unverifiable notes never auto-close and rot.
The resolver only acts on `done_when` + `stale_after`; other fields are left untouched.

## Projects → see the `project-notes` skill

Multi-step builds (`>1` digital/verifiable step) use `type: project` notes with a
step list, `current_step`, claim fields, and `resume_state`. That format and its
execution/turn-boundary rules live in the **`project-notes`** skill — read it when
a request trips the project gate.

**⚠️ A project note is born `status: in_progress`, NOT `pending`.** Do not copy the
`pending | done` schema from the task_bound/time_bound section above — that's for
todos/reminders only. Only two project statuses are valid: `in_progress` → `done`.
A project born `pending` gets orphaned from the Project Advancer (the gate that
wakes it keys on `in_progress`), so write `in_progress` from creation.

## The dedup gate (run BEFORE writing a new file)

**When:** about to auto-write a NEW `memory/_pin_<slug>.md` or `memory/_note_<slug>.md`
(from a pin trigger OR an auto-note trigger below).

**Action:**
```bash
python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/memory_dedup_check.py --kind pin|note --title "<candidate title>" --body "<candidate body>"
```
Read the JSON on stdout:
- `match:true` → an existing file already covers this topic. **OPEN `result.file`
  and MERGE/UPDATE it** (fold the new fact in, correct any stale line) instead of
  creating a second file. `suggestion:merge` = high confidence; `suggestion:review`
  = borderline, glance first. Partial/ambiguous overlap → ask the user merge-vs-new.
- `match:false` → genuinely novel; write the new `_pin_`/`_note_`.
- `skipped:*` → dedup couldn't run (no embed backend / embed error); proceed to
  write normally. **Never block a memory write on this.**

**Scope:** fires for durable `_pin_` writes and for `_note_` writes; skip for quick
read-only lookups and for editing a file you already identified.

**Why:** proven 2026-07-11 — a new `_pin_` nearly stacked on (and contradicted) an
existing pin; embedding-based dedup catches topic overlap a slug check cannot.
Slugs differ but topics overlap, so this is **semantic**, not a slug match. The
script uses adaptive z-score scoring (absolute e5 thresholds don't generalize);
tune via `DINOMEM_DEDUP_Z` / `DINOMEM_DEDUP_ABS_FLOOR` if a corpus over/under-flags.

## Auto-note triggers

Run the dedup gate first (an existing note may already track this work — merge
into it instead of stacking a duplicate), then auto-write a `_note_` when:

- **self_commitment:** your own output implies deferred work → auto-write `_note_`.
- **external_commitment:** your output commits to a discrete external artifact
  (github issue/PR, external send) not done THIS turn → auto-write `_note_`
  immediately with a verifiable `done_when`.
- **completion_followup:** task done but a follow-up remains → auto-write `_note_`.
- **project_estimate:** request trips the project gate → auto-write a `project`
  note before starting (see `project-notes`).
- **design_decision:** a multi-turn conversation SETTLES design/architecture
  decisions that have NO build task this turn (pure thinking: "we decided X over
  Y because Z", tradeoff analysis, a port/feature plan, resolved open questions).
  The other triggers all key on *deferred work*, so settled-thinking-with-no-task
  falls through and evaporates on compaction. Auto-write `type: brainstorm`,
  `status: design` — the Ideator critiques it, and its settled sub-decisions get
  distilled into durable `memory/*.md` items on resolution. Proven gap
  (2026-07-24): an 8-turn Honcho/OpenViking port design vanished because no
  trigger fired — recoverable only by luck of transcript indexing.

**Guardrail:** only if a concrete `done_when` is writable; personal/ambiguous → ask
first. (`design_decision` notes are the ONE exception: they have no `done_when` —
their lifecycle is `design` → human `promoted`/`resolved`/`abandoned`, never
auto-reaped by the janitor; see the brainstorm-note class rules.) **Upgrade:** enumerate steps FIRST; if `>1` digital/verifiable step →
`type: project`; never skip enumeration; physical/non-verifiable tasks → `task_bound`.
