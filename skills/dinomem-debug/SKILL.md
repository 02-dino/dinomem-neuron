---
name: dinomem-debug
description: Systematically diagnose a dinomem/neuron pipeline failure — a cron that failed, an extraction that errored, a hook that misfired, a memory that didn't get written/recalled, a graph/synthesis run that produced nothing. Read this BEFORE guessing at a fix, so you find the root cause (which stage, which layer) instead of patching a symptom.
---
# Debugging dinomem pipeline failures

The dinomem pipeline is multi-stage (session_reset → extract → ingest → graph →
synthesis → promote) and fail-open by design — errors are logged, the pipeline
continues. That means a failure is usually SILENT: memory just doesn't show up,
an insight never promotes, a note never advances. Guessing wastes turns. Follow
the phases.

## The Iron Law

```
NO FIX WITHOUT FINDING WHICH STAGE FAILED FIRST
```

dinomem is a chain. A missing memory could originate at ANY link (session never
archived → never extracted → never embedded → never graphed). Fix at the source
link, not the symptom link.

## Phase 1 — Locate the failing stage

Read the logs BEFORE touching code. Each stage logs to its own file:

| Stage | Log | Status file / output |
| ----- | --- | -------------------- |
| orchestrator | `logs/auto_reset.log` | orchestrator summary |
| session reset | `logs/session_reset.log` | archived .jsonl |
| extraction | `logs/extract_memory.log` | `memory/YYYY-MM-DD.md` + status file |
| session ingest | (in auto_reset.log) | `kb/vector_db_sessions/` |
| graph (L2) | run output | `kb/memory_neuron/l2_graph/memory_graph.json` |
| synthesis (L3) | run output | `kb/memory_neuron/l3_synthesis/` |
| promotion (L4) | run output | `memory/_permanent.md`, `promotion_log.jsonl` |
| project crons | `logs/` (advancer/improver) | `_note_*.md` frontmatter |
| hooks | gateway journal | (event-driven) |

**Do this:**
1. `tail -50` the log for the stage nearest the symptom.
2. Check the stage's OUTPUT exists and has a recent mtime (`ls -la`). An empty or
   stale output = the stage didn't run or produced nothing.
3. Walk BACKWARD: if extraction output is missing, check session_reset archived
   the session at all. The bad value's origin is usually upstream of the symptom.

## Phase 2 — Instrument the stage boundary

dinomem stages hand off via files/DBs. Verify each boundary the same way
superpowers instruments a multi-component system — check what ENTERS and what
EXITS each link:

```
For the suspect stage:
  - INPUT present?   (does the file/archive/DB row it reads exist + non-empty?)
  - Ran at all?      (log line with a recent timestamp?)
  - OUTPUT written?  (target file/row exists + recent mtime?)
  - Exit status?     (status file, or grep the log for ERROR/⚠️/crashed)
```

Common boundary failures (check these first — they recur):
- **Lock held:** a `flock`'d stage (extract, resolve_done_notes) skipped because
  another run holds the lock. Check for a stale lock file.
- **Claim collision:** a `_note_` wasn't advanced because `claimed_by` is set +
  `claimed_at` is fresh (live-session 120m / other 30m). A stale-but-unreleased
  claim blocks the Advancer.
- **Schema drift:** graph/synthesis reads the native sqlite via `_schema_adapter`
  — a schema it doesn't recognize = SchemaUnsupported, zero nodes.
- **Placeholder unresolved:** a script still has `DINOMEM_*_PLACEHOLDER` (install
  sed didn't run) → wrong workspace path → reads/writes the wrong dir.
- **Fail-open swallow:** the stage caught an exception and continued. The error is
  in the log even though exit was 0 — grep for it, don't trust exit code alone.

## Phase 3 — One hypothesis, minimal test

State it: "Stage X failed because Y." Then test with the SMALLEST run:
- Re-run the ONE stage standalone (most take `--dry-run`):
  `python3 procedures/<stage>.py --dry-run`
- Change ONE variable. Do not fix multiple things at once — you won't know which
  worked, and dinomem's fail-open masking makes multi-fixes especially opaque.
- Didn't confirm? Form a NEW hypothesis. Don't stack fixes.

## Phase 4 — Fix at source + verify with evidence

1. Fix the ORIGIN stage, not the symptom stage.
2. Re-run that stage for real, read the actual output (not "should work"):
   - extraction → the new `memory/YYYY-MM-DD.md` has the fact
   - graph → `memory_graph.json` node count > 0
   - promotion → `_permanent.md` / `promotion_log.jsonl` shows the entry
3. Re-run the DOWNSTREAM stages that depend on it (a re-extract needs a re-ingest
   + re-graph before the memory is recallable).
4. If a memory/note edit is involved, back up first (`procedures/backup.py`) — the
   git-autosnapshot store makes every write reversible, but confirm it's active.

## If 3+ fixes fail: question the stage design

Repeated failures that each surface a new coupling (extract fixes break ingest,
ingest fixes break graph) = not a bug, an architecture smell. Stop. The stages may
be sharing state they shouldn't, or a schema contract drifted. Surface it to the
owner instead of a 4th patch.

## Red flags — STOP and return to Phase 1

- "Just re-run the cron and see" (without reading the log first)
- "Probably the embedding model" (without checking the DB row exists)
- Patching the recall side when the write side never happened
- Trusting exit 0 on a fail-open stage without grepping the log for the swallowed error
- Editing memory files by hand to "fix" what a broken stage should produce
