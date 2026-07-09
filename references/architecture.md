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
    ├── memory_neuron/               # memory store — L2/L3/L4 synthesis artifacts
    │   ├── l2_graph/
    │   │   └── memory_graph.json     # Graph nodes + edges
    │   ├── l3_synthesis/
    │   │   ├── SYNTHESIS.md              # Living snapshot of current insights
    │   │   ├── current_insights.json     # Structured handoff → L4 promoter
    │   │   ├── insight_history.jsonl     # Append-only insight history
    │   │   ├── l3_edge_confidence.json   # Edge confidence scores
    │   │   ├── contradiction_log.jsonl   # Detected contradictions
    │   │   └── contradiction_queue.jsonl # Unresolved contradictions
    │   └── l4_promoted/
    │       ├── promotion_state.json      # Provisional + trusted store
    │       ├── promotion_log.jsonl       # Append-only audit trail
    │       ├── TRUSTED.md                # Human-readable trusted set mirror
    │       └── .last_consumed.json       # Dedup guard
    ├── vector_db_docs/               # long-docs store — RAG embeddings (separate from memory)
    └── data_store/
        ├── data.duckdb               # structured-data store — one table per dataset (separate from memory)
        └── catalog.json             # table schema map + intent router (columns, keywords, source_hash)
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

**Execution loop** — turn boundaries are decided by **step heaviness**. Before each step the agent judges its execution load:

- **Next step is heavy** (write+test a whole tool/file, multi-file change, research-then-build, a full write→run→debug→verify cycle) — give it its own turn: do only that step, checkpoint, stop cleanly, report `step n/m done`. Don't chain another heavy step. Next turn or the Project Advancer cron resumes from `current_step`.
- **Next step is light** (tiny edit/config/doc/one-liner) — mark `[x]`, rewrite `resume_state`, advance `current_step`, batch the next light step in the same turn.

Heaviness is the signal because build-and-test steps fragment badly if split mid-cycle; a heavy step earns its own turn regardless of how much room is left.

Resumption reads `resume_state` **only** — never conversation history. After every step the agent rewrites `resume_state` so the note stays self-sufficient. This is the reliability linchpin: a blind fresh session must be able to continue from the note alone.

**Driver — hybrid** — the user can say "continue" to advance manually. If they don't, the **Project Advancer** cron (every 4h) sweeps `status: in_progress` project notes and advances them unattended, announcing progress to chat. A safety gate keeps autonomy honest: destructive or external steps (delete data, git push, send external messages, spend money, touch credentials) are **not** executed unattended — the project pauses and announces it is awaiting approval. Read/build/local-file steps proceed on their own. Once `done_when` is met, the neuron's **Daily Note Review** cron (06:00 UTC, installed by this skill) deletes or promotes the note through the normal pipeline. It is claim-aware and also GCs `task_bound` notes whose `done_when` is met and abandoned notes past `stale_after`.

**Atomic task claiming** — the Project Advancer writes `claimed_by` + `claimed_at` to a note before executing any step. If another Advancer instance fires while the first is running (overlapping 4h cron windows), it sees the claim and skips that note. Claims older than 10 minutes are considered stale and auto-released. The claim is cleared on step completion, context checkpoint, or safety-gate pause. This prevents double-advancement when two cron instances overlap.

## RAG — long-document ingest

Long documents (contracts, books, legal text, manuals) live in a separate vector store (`kb/vector_db_docs/`), kept apart from memory so they never pollute the synthesis pipeline. Ingest is **seamless and multi-format**: share a file in chat — PDF, Word (`.docx`), `.rtf`, `.odt`, HTML, EPUB, or plain text/markdown — and `docs_ingest.py` auto-extracts it to markdown (PDF via `pdftotext`, Office/HTML via `pandoc`), saves `docs/<slug>.md`, and ingests it for `docs_search`. No manual conversion, no folder drop. Uploaded media paths (`~/.openclaw/media/inbound/<uuid>`) are accepted directly, with magic-byte format detection when the extension is missing. If a binary is unavailable, text/markdown ingest still works and the affected formats degrade gracefully.

## Structured data — exact SQL store

Structured/semi-structured datasets (CSV, JSON, Parquet) live in a **separate DuckDB store** (`kb/data_store/data.duckdb`), kept apart from both memory and the RAG vector store. The **primary path** is exact retrieval — the counterpart to semantic RAG: filter, range (`<`,`>`,`=`), sort, `COUNT`/`SUM`/`GROUP BY`, cross-table `JOIN` — the precise questions embeddings can't answer.

Two **optional aids** ride alongside the exact path (neither regresses it):

- **Fuzzy value routing** (`data_query.py --suggest`) — resolves an inexact filter *value* (typo / partial / simple synonym-acronym) to a real stored value using DuckDB-native `jaro_winkler`/`levenshtein`/`ILIKE`. No embeddings. It *suggests* the real `table.column = value` (+ an example `WHERE`); it never rewrites your SQL.
- **Per-prose-column semantic rank** (`data_query.py --semantic`) — for a table whose one column is free **prose** (survey feedback, reviews, tickets), `data_register` auto-detects that column (text, not a low-cardinality dimension, avg length ≥ 25 chars) and builds a small per-column embedding index (`kb/data_store/text_index/<table>__<col>/`, reusing the same `all-MiniLM-L6-v2`/Chroma stack as `docs_search`). `--semantic "<table>.<col>: <query>"` ranks rows by *meaning* and **joins the matched `rowid` back to the full structured row**, so "find rows about pricing complaints" then flows straight into normal SQL. It is **strictly opt-in per column** and **optional-dependency guarded**: if the embedding stack isn't installed, `--semantic` exits cleanly (code 3) and the exact-SQL path is entirely unaffected. This is why the store is *table-first with prose riding along*, not a second semantic store.

**Store layout.** One DuckDB file, **one table per dataset** (never a category column, never a DB-per-category). `data_register.py` materializes a source file into a table (DuckDB `DESCRIBE` infers column types; nested JSON via `read_json_auto(maximum_depth=-1)` so `meta.tier`-style fields stay queryable) and records the table in `catalog.json`. Nested/semi-structured JSON is preserved as a `STRUCT` column and queried with dotted paths.

**catalog.json** is both the schema map and the **intent router**: per table it stores `table_name`, `source_file`, `file_type`, `columns`, `row_count`, `keywords`, `description`, and a `source_hash` for change detection. `--list` surfaces columns+keywords+dimensions so the agent picks the right table.

**Auto-profiling (pure SQL, no LLM).** At register/refresh each column is profiled: `distinct_count`, a `categorical` flag (a low-cardinality *routing dimension* — ≤25 distinct text/enum/bool or small-domain int; unique-int keys and continuous numerics are excluded so an `id` never masquerades as a dimension), and up to 12 `sample_values` for those dimensions. From this, **`keywords` are auto-derived** (table name + column names + word-like categorical values; numeric/id values are excluded as routing noise), so a natural-language request routes to the correct table even without hand-typed `--keywords` — manual keywords are merged first and preserved across `--refresh`. The sampled values double as the agent's **value vocabulary for correct `WHERE` clauses** (it knows `region ∈ {US, EU, APAC}` is a 3-value dimension vs `price` continuous), directly attacking the bad-SQL failure mode.

**Read-only by construction — two layers.** `data_query.py` runs SELECT-only queries: (1) a pre-execution guard strips comments, rejects multi-statement input (`SELECT 1; DROP TABLE …`), requires a leading `SELECT`/`WITH`, and scans for any forbidden keyword (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`CREATE`/`ATTACH`/`COPY`/`PRAGMA`/`INSTALL`/`LOAD`/…); (2) the DuckDB connection is opened `read_only=True`, so a write is rejected at the engine level even if the guard were bypassed. DuckDB parse/binder/runtime errors surface as a clean one-line `SQL error:` (exit 2), never a raw traceback. `duckdb` is an optional dependency: if absent, the feature is unavailable and the semantic tools are unaffected (exit 3, feature-off-not-crash).

**Refresh.** `data_register.py --refresh` re-scans registered sources: hash-skips unchanged, updates changed (re-materialize + new schema/row_count), and prunes vanished sources (`DROP TABLE` + catalog removal).

**Routing (`## structured_data` / `## retrieval_router` in AGENTS.md).** The agent routes by request **shape**: row · count · range · filter · sort · aggregate · field-lookup → `data_query`; passage · "explain" · "like this" → `docs_search`; fact · decision · preference → `memory_search`; recent-conversation (<7d) → `session_search`; exact pattern/regex over raw text → `exec` grep. Rows are **never** written to memory. This is the separation that keeps memory clean: three stores (memory, `vector_db_docs`, `data_store`), each intent-routed, no cross-pollution.

## Prerequisites

- dinomem base installed (TEI running, `<agent>.sqlite` exists with embeddings)
- Python 3.8+
- OpenClaw memory DB populated (requires at least a few sessions extracted)
- `pdftotext` and `pandoc` on `PATH` for multi-format RAG ingest (installer auto-installs when possible; text/markdown ingest works without them)
