---
name: retrieval-routing
description: Pick the right retrieval route for a query and ingest long documents / structured data correctly in dinomem-neuron. Read this when deciding between hybrid_recall (the fuzzy-recall fusion door) vs the exact/ingest/meta forks (data_query / grep / docs_ingest / data_register / memory.diffs / retrieval_log), when a long document is shared or uploaded, or when a query is a row/count/range/filter/aggregate over structured data (csv/json/parquet).
---

# Retrieval routing (dinomem-neuron)

Neuron adds retrieval tools base doesn't have (`session_search`, `data_query`,
`graph_search`, `docs_search`) plus `hybrid_recall` — a fusion layer that runs the
fuzzy legs together and reranks. This skill is the router: which tool for which
query, plus how to ingest long docs and structured data.

## The router — EXACT-vs-FUZZY fork

Routing inverts around one idea: **check the EXACT / ingest / meta forks first;
everything else (semantic recall) goes through the ONE fuzzy door,
`hybrid_recall`.** You no longer hand-pick among 5 fuzzy legs — `hybrid_recall`
fires the right legs, fuses, dedups, reranks, and compiles with provenance.

| Query intent | Route |
| ------------ | ---- |
| **EXACT** row · count · range · filter · sort · aggregate · field lookup (csv/json/parquet) | `data_query` (exact SQL) — **NEVER** fused |
| **EXACT** pattern · regex · grep · exact-string over raw text/logs | `exec` (ripgrep/grep) — **NEVER** fused |
| **INGEST** long doc / file shared / "remember this doc" | `docs_ingest` ; csv/json/parquet dataset → `data_register` |
| **META** what changed in memory (adds/merges/deletes) | read `memory/.diffs/` (newest JSON) |
| **META** which retrieval tool ran · routing quality | `kb/retrieval_log/` (`_retrieval_log.py --list`/`--health`) |
| **FUZZY** fact · decision · preference · todo · meaning · passage · recent convo (<7d) · relational (why/led-to/affects/trace) | **`hybrid_recall`** (the one door) |

**Using `hybrid_recall`:** `python3 tools/hybrid_recall.py "<natural-language
query>" [--intent-hint fact|recent|semantic|relational] [--k 5] [--json]`. It
fuses `docs_search` + `session_search` + `graph_search` internally. To fold in
native `memory_search` (which is a native tool-call with no CLI, so the script
can't invoke it): **run `memory_search` yourself first, then pass its hits** via
`--external-hits <json-file|->`. The tool dedups by content across legs, counts
cross-leg corroboration, and reranks by normalized-sim + evidence + recency −
redundancy. Every call logs to `kb/retrieval_log/`.

**When to still reach a single leg DIRECTLY** (skip the fusion): you already know
the exact leg and want its raw output — e.g. `graph_search --trajectory` to see a
node's score-path, `session_search --list` to enumerate indexed sessions,
`docs_search --doc <x>` to filter one document. Fusion is the default recall path;
direct-leg is the power-user escape hatch.

The discriminators below describe **what `hybrid_recall` routes to under the hood**
(so you understand its behavior) and the exact/ingest/meta forks it does NOT touch:
- **Structured lookup ≠ semantic.** `row|count|range|filter|aggregate` over
  registered data → `data_query`, **NOT** `docs_search` and **NOT** `memory_search`
  (memory has no `<`, `>`, `=`, `COUNT`, `SUM`).
- **`memory_search` is the DEFAULT recall tool; `session_search` is the escalation.**
  Reach for `session_search` ONLY when (a) `memory_search` came back thin/empty
  AND (b) the ask is a *recent-conversation detail* (“what did we say/decide
  yesterday”, verbatim exchange, a thread that may not have distilled to memory yet)
  — NOT a durable fact/decision/preference (those are `memory_search`). Recency
  boundary: `session_search` only indexes the last **<7d** of transcripts; older
  than that lives in `memory_search` only. When unsure, `memory_search` first.
- **`session_search` embeds queries too**, so the natural-language query rule (M3)
  applies to it as well: prefer natural language, avoid technical
  identifiers / code terms / exact strings / variable names.
- **`graph_search` fires on IMPLICIT relational intent, not just literal keywords.**
  Causal/dependency/impact questions route here even when the words
  “depends_on / affects / trace” are absent — e.g. “why did X break”, “what led
  to Z”, “what touches Y”, “what’s downstream of…”, “what caused…”. Use
  `--cluster-drill` to enter via topic cluster then descend, and `--trajectory`
  to see the score-path (why a node surfaced). Skip when relation_edges aren't
  populated yet; fall back to `memory_search`.
- **`memory/.diffs/` is the memory audit log** (one JSON per extraction run:
  adds/updates/deletes with before/after). Route meta-questions about *memory
  itself* here — “what changed / was merged / was deleted recently” — not to
  `memory_search` (which returns memory *content*, not its edit history). Newest
  file = latest run; `_memory_diff.py --list` / `--show <run_id>` to inspect.
- **`kb/retrieval_log/` is the retrieval audit log** (read-side twin of
  `memory/.diffs/`) — every call to `session_search` / `docs_search` /
  `graph_search` appends one line to today's JSONL: tool, query, result count,
  top scores, whether it fell back. A daily cron rolls dailies older than 30d
  into `kb/retrieval_log/rollups/YYYY-MM.json` and refreshes
  `kb/retrieval_log/health.json` (per-tool calls / fallback_rate / empty_rate /
  avg_top_score). Route meta-questions about *retrieval itself* here — “which
  tool got used”, “is graph_search actually being reached for”, “why did that
  query come back empty”, “how's routing quality lately” — via
  `python3 procedures/_retrieval_log.py --list` (recent calls, `--tool <name>`
  to filter) or `--health` (the routing-quality snapshot). NOT wired for native
  `memory_search`/`memory_get` (OpenClaw-native, outside dinomem's instrumentation
  reach) — covers the 3 neuron retrieval tools this workspace ships.

## Long documents → RAG (`docs_search`)

**When:** a long doc is shared · a doc file is uploaded (pdf/docx/rtf/odt/html/
epub/md/txt) · "save/remember this doc" · content `>2000 chars`.

**Action:**
- **Uploaded file:** `python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/docs_ingest.py --file <media path>` → confirm.
  The script self-extracts pdf/docx/etc → `docs/<slug>.md`.
- **Pasted text:** save `docs/<slug>.md` → `python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/docs_ingest.py --file docs/<slug>.md` → confirm.

**Media path:** inbound uploads land at `~/.openclaw/media/inbound/<uuid>` (from the
`<media:...>` placeholder); pass that path straight to `--file`.

**Notes:** ingest auto-extracts via pdftotext + pandoc (no manual convert);
re-ingested daily 04:00 UTC; ready for `docs_search` after. **Never** save long
docs to `memory/`. Row/count/range/aggregate over structured data → `data_query`,
not `docs_search`.

## Structured data → `data_query` (exact SQL)

**When:** row · count · range · filter · sort · aggregate · field lookup · "how
many" · "under $X" · "on <date>" · "cheapest/most" · "list … where" over
registered exact data (csv/json/parquet; any structured/semi-structured dataset).

**Action:**
- **Query:** read `kb/data_store/catalog.json` → pick table(s) by keywords+columns →
  `python3 DINOMEM_WORKSPACE_PLACEHOLDER/tools/data_query.py --sql "SELECT ... FROM <table> WHERE ..."` (SELECT-only).
- **Register:** `python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/data_register.py --file <path.csv|json|parquet> --name <table> --desc "..." [--keywords a,b,c]`.

**Notes:** store = `kb/data_store/` (one duckdb, one table per dataset — NOT a
category column, NOT a db-per-category); handles structured + semi-structured
(nested json ok); `catalog.json` = schema map + intent router; retrieval = exact
SQL, no embeddings. **Never** save rows to `memory/`.
