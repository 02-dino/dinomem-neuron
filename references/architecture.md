# dinomem-neuron — Architecture

## Neuron Pipeline (extends dinomem base)

```
[dinomem base output]
memory/*.md + MEMORY.md + vector DB (TEI)
        │
        │  daily 3:00 UTC
        ▼
[memory_graph.py — L2]
  • Reads embeddings from <agent>.sqlite (OpenClaw native memory DB)
  • Computes cosine similarity between all memory nodes
  • Builds graph: nodes = memory entries, edges = similarity > threshold
  • Output: kb/memory_neuron/l2_graph/memory_graph.json
        │
        │  daily 3:30 UTC
        ▼
[memory_synthesis.py — L3]
  • Reads L2 graph clusters (connected components)
  • For each cluster: LLM synthesizes emergent insight
  • Deduplicates against existing insights
  • Output: kb/memory_neuron/l3_synthesis/insights.jsonl
        │
        ▼
[contradiction_check.py — daily 3:30 UTC, after synthesis]
  • Reads insight_history.jsonl
  • LLM checks each new insight against existing for contradictions
  • Flags contradictions to contradiction_log.jsonl
  • Queues unresolved contradictions to contradiction_queue.jsonl
        │
        │  weekly Sunday 4:00 UTC
        ▼
[memory_promote.py — L4]
  • Reads insights that have survived N synthesis cycles (durable)
  • Promotes to topics/PERSISTENT_AUTO.md + memory/_permanent.md
  • Marks promoted insights in insight_history.jsonl
        │
        ▼
[generate_topic_index.py — weekly Sunday 4:00 UTC, after promote]
  • Reads topics/PERSISTENT.md + topics/PERSISTENT_AUTO.md
  • Builds topics/INDEX.md (structured topic tree)
  • Writes MEMORY.md simultaneously — same content as INDEX.md with title header prepended
  • Note: MEMORY.md and topics/INDEX.md are NOT symlinks — both written in one atomic pass by this script
```

## Directory Structure

```
<workspace>/
├── topics/
│   ├── INDEX.md              # Auto-generated topic tree (do not edit)
│   ├── PERSISTENT.md         # Manually curated persistent topics
│   └── PERSISTENT_AUTO.md    # Auto-promoted by memory_promote.py (do not edit)
├── memory/
│   └── _permanent.md         # Promoted permanent lessons (do not edit)
└── kb/
    └── memory_neuron/
        ├── l2_graph/
        │   └── memory_graph.json     # Graph nodes + edges
        ├── l3_synthesis/
        │   ├── SYNTHESIS.md              # Living snapshot of current insights
        │   ├── current_insights.json     # Structured handoff → L4 promoter
        │   ├── insight_history.jsonl     # Append-only insight history
        │   ├── l3_edge_confidence.json   # Edge confidence scores
        │   ├── contradiction_log.jsonl   # Detected contradictions
        │   └── contradiction_queue.jsonl # Unresolved contradictions
        └── l4_promoted/
            ├── promotion_state.json      # Provisional + trusted store
            ├── promotion_log.jsonl       # Append-only audit trail
            ├── TRUSTED.md                # Human-readable trusted set mirror
            └── .last_consumed.json       # Dedup guard
```

## Cron Schedule

| Time | Script | Layer |
|------|--------|-------|
| Daily 3:00 UTC | `memory_graph.py` | L2 |
| Daily 3:30 UTC | `memory_synthesis.py` + `contradiction_check.py` | L3 |
| Weekly Sun 4:00 UTC | `memory_promote.py` + `generate_topic_index.py` | L4 |
| Every 4h | Project Advancer (OpenClaw agentTurn cron) | Project executor (auto-advance `type: project` notes) |

## Premium Note Upgrades (extends dinomem base note system)

Neuron layers two upgrades on top of the base `_note_` system (`done_when` + `stale_after`). Base dinomem knows nothing about these — it ignores fields it does not own. Neuron appends its rules in its own `<!-- BEGIN:dinomem-neuron -->` AGENTS.md block and registers the Project Advancer cron.

### Automatic note creation

Base note creation is user-driven. Neuron makes it automatic via three triggers:

- **Self-commitment** — when the agent's own output implies deferred work ("next step…", "TODO", "remaining: X"), it writes a `_note_` for that commitment. Closes the gap where an agent promises something and a later session forgets.
- **Completion follow-up** — when a task finishes but leaves a remainder, the leftover becomes a `_note_`.
- **Project estimate** — when a request trips the project gate (below), the agent writes a project note before starting.

Anti-spam guardrail: only auto-notes when a concrete `done_when` is writable. Personal/ambiguous items still ask first; self-commitments and project plans do not.

### Project notes — context-bounded step executor

A `type: project` note is a durable, on-disk plan that lets the agent execute a large build **one step per turn**, surviving context limits and fresh sessions. The note is the state machine.

```
# Project <name>
type: project
status: in_progress | done
date: YYYY-MM-DD
done_when: <final artifact / all steps [x]>
stale_after: YYYY-MM-DD
current_step: <n>
claimed_by: <run_id>          # set by Project Advancer on claim; blank when idle
claimed_at: <ISO timestamp>   # time of claim; stale if >10min ago (auto-released)
steps:
  - [ ] 1. <step> -- done_when: <check>
  - [ ] 2. ...
resume_state: <paths / decisions / assumptions the next turn needs to resume blind>
```

**Estimate gate** — at request time the agent decides whether a request is a project: `>3 files OR >5 sequential tool-steps OR research-then-build OR the user says "project/build"`. Heuristic floor, LLM may override. Trip the gate → write a project note instead of diving in; else execute normally in one turn.

**Execution loop** — the agent does the current step, then reads its own context budget via `session_status` (`Context: Nk/200k (X%)`):

- **Under 60%** — mark the step `[x]`, rewrite `resume_state`, advance `current_step`, continue to the next step in the same turn.
- **Over 60%** — checkpoint (state already written), stop cleanly, report `step n/m done, context X%`. The next turn or the Project Advancer cron resumes from `current_step`.

Resumption reads `resume_state` **only** — never conversation history. After every step the agent rewrites `resume_state` so the note stays self-sufficient. This is the reliability linchpin: a blind fresh session must be able to continue from the note alone.

**Driver — hybrid** — the user can say "continue" to advance manually. If they don't, the **Project Advancer** cron (every 4h) sweeps `status: in_progress` project notes and advances them unattended, announcing progress to chat. A safety gate keeps autonomy honest: destructive or external steps (delete data, git push, send external messages, spend money, touch credentials) are **not** executed unattended — the project pauses and announces it is awaiting approval. Read/build/local-file steps proceed on their own. Once `done_when` is met, the base Daily Note Review cron deletes or promotes the note through the normal pipeline.

**Atomic task claiming** — the Project Advancer writes `claimed_by` + `claimed_at` to a note before executing any step. If another Advancer instance fires while the first is running (overlapping 4h cron windows), it sees the claim and skips that note. Claims older than 10 minutes are considered stale and auto-released. The claim is cleared on step completion, context checkpoint, or safety-gate pause. This prevents double-advancement when two cron instances overlap.

## RAG — long-document ingest

Long documents (contracts, books, legal text, manuals) live in a separate vector store (`kb/vector_db_docs/`), kept apart from memory so they never pollute the synthesis pipeline. Ingest is **seamless and multi-format**: share a file in chat — PDF, Word (`.docx`), `.rtf`, `.odt`, HTML, EPUB, or plain text/markdown — and `docs_ingest.py` auto-extracts it to markdown (PDF via `pdftotext`, Office/HTML via `pandoc`), saves `docs/<slug>.md`, and ingests it for `docs_search`. No manual conversion, no folder drop. Uploaded media paths (`~/.openclaw/media/inbound/<uuid>`) are accepted directly, with magic-byte format detection when the extension is missing. If a binary is unavailable, text/markdown ingest still works and the affected formats degrade gracefully.

## Prerequisites

- dinomem base installed (TEI running, `<agent>.sqlite` exists with embeddings)
- Python 3.8+
- OpenClaw memory DB populated (requires at least a few sessions extracted)
- `pdftotext` and `pandoc` on `PATH` for multi-format RAG ingest (installer auto-installs when possible; text/markdown ingest works without them)
