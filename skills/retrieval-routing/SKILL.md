---
name: retrieval-routing
description: Pick the right retrieval tool for a query and ingest long documents / structured data correctly in dinomem-neuron. Read this when deciding between docs_search / session_search / memory_search / data_query / graph_search / grep, when a long document is shared or uploaded, or when a query is a row/count/range/filter/aggregate over structured data (csv/json/parquet).
---

# Retrieval routing (dinomem-neuron)

Neuron adds retrieval tools base doesn't have (`session_search`, `data_query`,
`graph_search`, `docs_search`). This skill is the router: which tool for which
query, plus how to ingest long docs and structured data.

## The router

| Query intent | Tool |
| ------------ | ---- |
| meaning · "explain" · "like this" · passage / semantic | `docs_search` |
| recent conversation detail (**<7d**) | `session_search` |
| fact · decision · preference · todo | `memory_search` |
| row · count · range · filter · sort · aggregate · field lookup (csv/json/parquet) | `data_query` (exact SQL) |
| complex relational query (depends_on / affects / trace / chain) | `graph_search` (fallback: `memory_search`) |
| exact pattern · regex · grep · exact-string over raw text/logs | `exec` (ripgrep/grep) |

Key discriminators:
- **Structured lookup ≠ semantic.** `row|count|range|filter|aggregate` over
  registered data → `data_query`, **NOT** `docs_search` and **NOT** `memory_search`
  (memory has no `<`, `>`, `=`, `COUNT`, `SUM`).
- **`session_search` embeds queries too**, so the natural-language query rule (M3)
  applies to it as well: prefer natural language, avoid technical
  identifiers / code terms / exact strings / variable names.
- `graph_search` — skip when relation_edges aren't populated yet; fall back to
  `memory_search`.

## Long documents → RAG (`docs_search`)

**When:** a long doc is shared · a doc file is uploaded (pdf/docx/rtf/odt/html/
epub/md/txt) · "save/remember this doc" · content `>2000 chars`.

**Action:**
- **Uploaded file:** `python3 procedures/docs_ingest.py --file <media path>` → confirm.
  The script self-extracts pdf/docx/etc → `docs/<slug>.md`.
- **Pasted text:** save `docs/<slug>.md` → `python3 procedures/docs_ingest.py --file docs/<slug>.md` → confirm.

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
  `python3 tools/data_query.py --sql "SELECT ... FROM <table> WHERE ..."` (SELECT-only).
- **Register:** `python3 procedures/data_register.py --file <path.csv|json|parquet> --name <table> --desc "..." [--keywords a,b,c]`.

**Notes:** store = `kb/data_store/` (one duckdb, one table per dataset — NOT a
category column, NOT a db-per-category); handles structured + semi-structured
(nested json ok); `catalog.json` = schema map + intent router; retrieval = exact
SQL, no embeddings. **Never** save rows to `memory/`.
