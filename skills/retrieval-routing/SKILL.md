---
name: retrieval-routing
description: Pick the right retrieval route for a query and ingest long documents / structured data / code structure correctly in dinomem-neuron. Read this when deciding between hybrid_recall (the fuzzy-recall fusion door) vs the exact/ingest/meta forks (data_query / grep / docs_ingest / data_register / code_graph / code_query / memory.diffs / retrieval_log), when a long document is shared or uploaded, when a query is a row/count/range/filter/aggregate over structured data (csv/json/parquet), or when navigating THIS repo's own source (what calls X / X calls what / class hierarchy / where a function is defined / build or refresh the code graph).
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
| **INGEST** long doc / file shared / "remember this doc" | `docs_ingest` ; csv/json/parquet dataset → `data_register` ; **code tree to navigate** (this repo's own source, calls/callers/hierarchy/where-defined) → `code_graph` (build/refresh the AST index) |
| **EXACT** code structure · what calls X · X calls what · class hierarchy · where a function/method is defined · edge path A→B (file:line) | `code_query` (over the code graph) — **NEVER** fused |
| **META** what changed in memory (adds/merges/deletes) | read `memory/.diffs/` (newest JSON) |
| **META** which retrieval tool ran · routing quality | `kb/retrieval_log/` (`_retrieval_log.py --list`/`--health`) |
| **FUZZY** fact · decision · preference · todo · meaning · passage · recent convo (<7d) · relational (why/led-to/affects/trace) | **`hybrid_recall`** (the one door) |
| **PEER** what do I know about *the person I am talking to right now* · "inget apa soal gue" · "we discussed this before" (said by a non-owner) | `peer_recall` (native tool) — **NEVER** fused; scope is server-side, takes no target |

**Using `hybrid_recall`:** `python3 tools/hybrid_recall.py "<natural-language
query>" [--intent-hint fact|recent|semantic|relational] [--k 5] [--json]`. It
fuses `docs_search` + `session_search` + `graph_search` + `memory_native`
internally. `memory_native` reads the SAME OpenClaw memory index (SQLite FTS5 +
384-dim vectors) DIRECTLY, so the memory leg no longer depends on the native
`memory_search` tool being healthy — recall survives a memory_search timeout.
You MAY still fold in native `memory_search` hits (it is a native tool-call with
no CLI, so the script can't invoke it): run `memory_search` yourself first, then
pass its hits via `--external-hits <json-file|->`. That is now additive, not
required. The tool dedups by content across legs, counts
cross-leg corroboration, and reranks by normalized-sim + evidence + recency −
redundancy. Every call logs to `kb/retrieval_log/`.

**Cross-encoder rerank (memory_native leg only).** That leg's dense scores come
back nearly FLAT — measured on a 5.3k-chunk corpus: 10 hits spanning cosine
0.856–0.864, i.e. a spread of 0.007–0.011. `_normalize_within_leg` then min-max
stretches that 0.008 of noise across a full 0–1 axis, so the ordering it handed
fusion was noise wearing a score. A cross-encoder scores (query, passage)
JOINTLY instead of comparing two independently-embedded vectors, and on the SAME
candidates measured spread 0.31–0.68. So the leg now reranks its own `score`
through `tools/_rerank.py` (TEI `/rerank`, the same container `semantic_search`
already uses) before handing candidates to fusion. Other legs are deliberately
untouched — they already report usable spread.
- **Precision, not recall.** Ground-truth probe (3 queries): correct doc at raw
  #3 → #1 after rerank ✅ ; #3 → #3 neutral ; and one where the leg never
  retrieved the right doc at all → rerank could not help. A reranker reorders
  what retrieval found; it can never surface a passage the leg missed. If recall
  is the problem, fix the leg, not the ranking.
- **Cost is superlinear in BOTH dimensions**, which is why both are capped:
  8 texts × 400 chars = 1.75–3.2 s, but 20 × 2000 = 19–24 s. Bolted on
  uncapped it would add ~20 s to a 1.2 s warm recall. Defaults: top-8,
  400-char truncation, 6 s budget.
- **FAIL-OPEN by contract.** `rerank_scores()` returns scores aligned to the
  input list, or `None`. `None` means "no opinion" and the original order is
  kept — reranker down, slow, malformed, or signal-collapsed all cost nothing.
  Disable explicitly with `DINOMEM_RERANK=0` (the leg's cache key encodes this,
  so an A/B flip can't silently replay the other mode's cached ordering).
- **MODEL — `BAAI/bge-reranker-v2-m3` (multilingual) since 2026-11-11,** shipped
  by dinomem base `docker/docker-compose.tei-rerank.yml`. It replaced
  `cross-encoder/ms-marco-MiniLM-L-6-v2`, which is ENGLISH-ONLY: controlled decoy
  test EN query → EN doc 0.4263 (correct order); ID → ID 0.1919 (still correct);
  EN query → ID corpus collapsed to 0.0002 spread, i.e. signal gone — so every
  mixed-language recall silently fail-opened. The spread guard stays either way.
  Cost: bge is ~568M params vs ~22M; on CPU it takes **minutes** to warm up, and
  during that window `/health` is down and the leg fail-opens.
- **FALLBACK LADDER:** TEI → *(opt-in)* LLM listwise rerank on the agent's
  **primary** model (`agents.defaults.model.primary` in openclaw.json) → `None`.
  Enable with `DINOMEM_RERANK_LLM_FALLBACK=1`; pin a model with
  `DINOMEM_RERANK_LLM_MODEL`. Off by default because a gateway round-trip
  measured ~35 s on a loaded box — fine as a bridge while bge warms up, wrong as
  a tax on every recall. Primary not cheap tier: ordering passages is judgement,
  the cheap tier is for throwaway generation (HyDE, extraction).
  Self-test the whole contract with `python3 tools/_rerank.py --selftest`.

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
- **`peer_recall` is the PEER door, and it is not interchangeable with `memory_search`.**
  It answers exactly one question: *what do I already know about the person in this
  conversation?* Reach for it when the user refers to earlier conversations with you
  ("inget apa soal gue", "we talked about this", "you know my preferences").
  Three properties that decide the routing:
  - **It takes no target.** Scope is derived SERVER-SIDE from `sessionKey`
    (`agent:<id>:<platform>:direct:<peerId>`). Neither the model nor the user can choose
    whose record is read, so there is no "peer" argument to pass — only an optional
    `query` substring that filters the record already scoped to that peer.
  - **Non-direct lanes get nothing, by design.** Group / subagent / cron lanes have no
    `:direct:` segment, so it returns *"no single-peer scope for this session"*. That is
    the correct answer, not a failure — do not retry it elsewhere or route around it.
  - **It is the ONLY recall door a non-owner has.** `memory_search` / `memory_get` /
    `session_search` / `docs_search` / `graph_search` are all denied to non-owners by
    dinotrust (they read the whole internal corpus: owner DMs, `_pin_` infra, every peer's
    rep). `peer_recall` is allowlisted precisely because it can only ever return the
    caller's own record. So in a non-owner lane: peer question → `peer_recall`, never
    `memory_search`.
  Owner lanes may use either; prefer `peer_recall` when the question is about the
  *interlocutor* and `memory_search`/`hybrid_recall` when it is about work, decisions, or
  the system itself. Empty result ≠ broken: a peer with no derived rep yet legitimately
  returns *"no stored record yet for peer:<platform>:<id>"*.
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

## Code structure → `code_query` (graph)

**What this is:** a ZERO-LLM structural index of THIS repo's own source (nodes =
file/function/class/method w/ `file:line`; edges = calls/imports/inherits/contains).
Spending LLM tokens to "understand" imports/calls/inheritance is waste — the AST
knows it exactly. Query a scoped subgraph (~few hundred tokens) instead of
`grep -r` + reading whole files into context (~20× the tokens, lower coverage).

**When to INGEST (build/refresh the graph):** before the FIRST code-navigation
query in a workspace · after you've edited source and want callers/callee edges
current · whenever `code_query` prints a `[stale]` flag (built_at_sha ≠ HEAD) or
returns empty. It's cheap + git-incremental (re-parses only changed files), so
refreshing is near-free.
- `python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/code_graph.py --path <root>`
  (AST, no LLM; `--full` forces a from-scratch rebuild). Default root ladder:
  `--path` arg > `DINOMEM_CODE_ROOTS` env > agent workspace > `~/.openclaw`.

**When to SEARCH (query the graph):** any "what calls X / X calls what / class
hierarchy / where is <fn> defined / how does A reach B" over source — INSTEAD of
grep+read. Reflex: a code-structure question about this repo's own code → `code_query`
FIRST, grep only as fallback.
- `python3 DINOMEM_WORKSPACE_PLACEHOLDER/tools/code_query.py explain <symbol>`
  (neighborhood: contains/calls/called-by/imports/inherits, each `file:line` +
  EXTRACTED/INFERRED provenance).
- `... code_query.py path <A> <B>` (shortest edge path A→B).
- `... code_query.py query "<nl>"` (fuzzy-find the symbol, then explain).

**`code_query` vs `hybrid_recall` (the discriminator):**
- Pure structure ("what calls `fuse`", "where is `apply_as_of` defined") → **`code_query`** directly.
- Structure **+ the WHY** ("what does `apply_as_of` do AND why does it exist") →
  **`hybrid_recall`** — its CODE leg auto-fires on function/class/calls/imports/inherits
  wording and fuses the `code_query` `file:line` hits WITH the memory/graph design
  decision in one block. Neither plain code-search (no *why*) nor plain memory (no
  *file:line*) answers that alone — that fusion is the killer feature.

**Carve-outs — when grep/read is correct and `code_query` is ceremony (don't force it):**
- **Files already open in context.** If the target file(s) are already in your
  transcript (you read them this session, or they're in the read-files list), just
  `read`/`grep`/`edit` them directly. `code_query` earns its keep when you DON'T
  know where a symbol lives or who touches it — not when you're editing 3 files you
  already have open. Re-graphing a known file is ceremony, not signal.
- **Prose / config-string lookups (no AST edge to traverse).** A match inside a
  string, comment, heredoc, JSON/YAML config value, or a cron/prompt payload has NO
  call-graph edge — grep is the right tool. (e.g. finding a phrase inside an
  `install.sh` heredoc prompt: grep, not code_query.)
- **Shell / non-AST-modelled code.** The graph is AST-based and strongest on
  Python (+ other tree-sitter langs). Bash function call-graphs are weak-to-absent;
  for a small, known set of shell scripts, grep the anchor + read is leaner and
  more reliable than a shell "call graph." Use code_query for shell ONLY to locate
  a function definition across many unknown files, never as a call-path oracle.

**Still reflex-first for the real case:** an UNKNOWN code tree + a genuine
structure question (what calls X / X calls what / where is Y defined / A→B path)
over AST-modelled source → `code_query` FIRST. The carve-outs narrow the rule,
they don't repeal it.

**Notes:** graph store = `kb/code_graph/` (a SEPARATE DB file from the memory
graph — no collision); OPTIONAL tree-sitter dep (missing → code_graph offline,
fail-soft, rest of retrieval unaffected). **Never** grep+read whole files for a
structure question over unknown AST-modelled source when the graph can answer it scoped.

**Coverage (CAPPED — do NOT index the world):** big-six + agent-config —
**python / bash / json / markdown / JS / TS** (JS/JSX/TS/TSX — with tsconfig `paths` alias + workspace-glob resolution, barrel re-export hop, module-scoped member calls, and cross-file named-call scoping (`import {foo}; foo()` → the real module def, not a global same-name); external pkgs + unscoped `this.`/`arr.` member calls dropped; `code_query explain` annotates a flattened barrel edge `(via barrel.ts)`) **plus go / rust / java / cpp / php / csharp / ruby / r / css / html / sql / yaml**. C-family + dynamic langs share one generic walker (per-lang node-type specs); css/html/sql/yaml are structural-only (selectors+@import / script|link src / table|view defs+FROM|JOIN refs / top-level keys). Same-name cross-file collision fixed (intra-file calls/inherits/references resolve same-file-first). Adding a language = extractor only; ingestion + code search are language-agnostic (traverse the graph, never re-parse), so a new lang works end-to-end automatically. 40-language parity is
Graphify's PRODUCT need, NOT ours. Go/Rust/Java/C#/C++ + niche langs
(fortran/ocaml/pascal/zig/etc) are **add-on-demand**: add a `parse_<lang>` + 2
lines (`LANG_BY_EXT` + `PARSERS`) ONLY when a real buyer file exists — the
scaffold makes it a ~2-line job, so there's no reason to pre-index languages
nobody ships. Bash/json/markdown now DO carry real edges (bash: function defs +
`source`/call edges; json: nested-key nodes + path-value `references`; markdown:
code-repo doc→code `documents` edges) — so the earlier "shell call-graph is
weak" carve-out is now narrower: shell **is** modelled (function defs + `source`
+ local-fn calls), though external-binary calls are still dropped as noise.
Extractors auto-honor the **path-scoped exemption** (memory/ · docs/ ·
data_query source dirs excluded so nothing double-ingests; env
`DINOMEM_CODE_GRAPH_EXEMPT` adds buyer RAG dirs). Behavioral logic buried in
`install.sh` heredoc prompts / cron payloads (which the AST sees as one opaque
string) is covered by a grep-backed **prompt-anchor sidecar**:
`procedures/code_anchors.py` → `code_query anchors "<query>"` returns the
`Step N:` / `SCOPE LOCK` / role-name label → `file:line`.

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
