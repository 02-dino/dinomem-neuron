# Changelog

All notable changes to dinomem-neuron are documented here.

---

## 1.6.0 — 2026-07-09

Auto-refresh for the note-claim protocol — the live session no longer has to remember `claim_note.sh refresh`. **Recommended for everyone; it closes a real race that lets a background cron overwrite a note you're mid-edit.** A new `after_tool_call` hook plugin (`plugins/note-claim-refresh/`) runs the claim refresh **as a consequence of editing a note**, so the claim the Advancer/Improver/Deleter crons read to back off can never go stale from a forgotten manual refresh.

### Added
- **`note-claim-refresh` hook plugin — automatic claim refresh on note edits.** The `note_claim_protocol` (in `AGENTS.md`) tells the live agent to run `claim_note.sh refresh` every turn it keeps editing a `memory/_note_*.md`; that is documentation-only, and a forgotten refresh lets a cron grab the note mid-edit and double-write it (proven race). This ships an `after_tool_call` plugin that fires on any `edit`/`write`/`apply_patch` to a `memory/_note_*.md` and runs `claim_note.sh refresh` fire-and-forget — **observe-only, never blocks or mutates the tool result**, and `claim_note.sh` self-guards (no-op if the note isn't yours). Workspace is resolved **dynamically at runtime** (plugin config `workspaceDir` → `$OPENCLAW_WORKSPACE` → derived from the target note's own absolute path → ctx), so it works for any install with no hardcoded path; it no-ops silently if no `scripts/claim_note.sh` is found. `install.sh` copies the plugin into `<workspace>/plugins/note-claim-refresh/` and wires `openclaw.json` (`plugins.allow` + `plugins.load.paths` + `plugins.config.note-claim-refresh.workspaceDir`), idempotently; `uninstall.sh` removes the dir and unwires the config. A gateway restart loads it (`onStartup`).

Multilingual embeddings for neuron's Python semantic tools. **Recommended for anyone whose docs/sessions/data aren't purely English.** `docs_search`, `session_search`, the structured-data `--semantic` path, and their ingest procedures now embed with `intfloat/multilingual-e5-small` (multilingual EN/ID + majors, 384-dim, 512-token context) instead of the English-centric `all-MiniLM-L6-v2`. Same 384-dim, same footprint.

### Changed
- **Default embedding model → `intfloat/multilingual-e5-small` across neuron's Python tools.** `tools/_embed.py` is now the single source of truth (model id via env `DINOMEM_EMBED_MODEL`, fallback const) and adds **e5's asymmetric prefixing centrally**: `embed_query` prepends `query: `, `embed_documents` prepends `passage: ` (LangChain Chroma routes searches vs ingest through those methods, so every tool gets correct prefixing for free). `tools/docs_search.py`, `tools/session_search.py`, `procedures/docs_ingest.py`, and `procedures/session_ingest.py` carry the matching model swap + a local `_PrefixedHFE` wrapper so ingest (passage) and query vectors align. `data_query --semantic` / `data_register` inherit it through `_embed.py`. Prefixing can be disabled for a symmetric model via `DINOMEM_EMBED_PREFIX=0`. **Requires the base repo's TEI on cpu-1.6** (cpu-1.5 can't download this model). Existing Chroma stores must be re-ingested to benefit (new dimspace semantics); fresh installs embed against e5 from the start.
- **Embedding model stamped into every Chroma store + startup mismatch warning.** Each store now records the embedding model id in its collection metadata (via `_embed.py`); on open, a mismatch between the store's recorded model and the currently-configured one is warned so a silent dimension/space mismatch (e.g. querying an old all-MiniLM store with e5 vectors) surfaces instead of returning garbage. `tools/_embed.py --selftest` exercises the resolver + query/passage prefixing end-to-end for install verification.

### Added
- **`scripts/reindex_all.sh` — sequential rebuild of every Chroma store with a real post-build query gate.** Rebuilds docs → sessions → prose-indexes **sequentially, never parallel** (VPS-RAM safe) and gates each store on an **actual search through the real module** (not a row count — a corrupt HNSW can have rows yet crash a query): non-crash = pass, empty = soft-warn, crash = hard-fail. For switching embedding models or recovering a corrupt store.
- **`scripts/migrate_embeddings.sh` — safe auto-migration when the embedding model changes.** Idempotency marker (`kb/.embed_model`, re-run is a fast no-op) + disk preflight (du vs df + headroom, aborts if short) + **backup-by-rename** (`mv` to `.bak-<ts>`, never `rm`) + rebuild delegated to `reindex_all.sh` + **restore-on-fail** (reindex fails → backup moved back, marker not written, install untouched) + resumability guard (refuses to clobber an existing interrupted-run backup). Purge of old backups is opt-in only (`--purge-backups`); `--dry-run` supported.

### Fixed
- **`scripts/claim_note.sh` now claims project notes (race-protection gap).** The note-claim helper — which the Advancer / Improver / Deleter crons read to back off a note a live session is editing — previously only handled `---`-fenced YAML frontmatter. **Project notes** (`type: project`) use a different shape: a `# Heading` first line followed by bare `key: value` frontmatter that ends at the first blank line (the same block `check_projects.sh` and `resolve_done_notes.py` read). On that shape the old script could not read the claim and, when writing, injected a stray top-level `---` block — producing a **duplicate/empty `claimed_by`** that the crons then read as *unclaimed*, so a background cron could advance a project note while a live session was mid-build. `read_claim`/`write_claim` now detect **both** frontmatter formats; project-note claims are written **in place** before the first blank line (drop prior claim lines, append the fresh claim), with **exactly one `claimed_by`** and no stray fence. Fenced-format notes are unchanged. Pass the note as a **full path** (`claim_note.sh` locates by path arg).

---

## 1.5.0

New retrieval capability: **structured data (exact SQL)**. **Recommended for anyone who works with spreadsheets, exports, or any tabular/JSON data.** Adds a third, separate store so the agent can answer the precise questions embeddings can't — *how many, which ones, under $X, on this date, sorted by, grouped by, the total* — with real `WHERE`/`ORDER BY`/`COUNT`/`SUM`/`GROUP BY`/`JOIN`, not similarity. Two optional aids ride alongside the exact path: **fuzzy value routing** (resolves typo/synonym filter values, no embeddings) and **per-prose-column semantic rank** ("find rows about X" over free-text columns like survey feedback) — both strictly opt-in, neither regresses the exact-SQL path.

### The point: separate stores, one router — memory is never polluted
dinomem-neuron now keeps **different stores for different kinds of retrieval, and picks the right one automatically from the shape of the request** — no manual selection, nothing cross-contaminates:

| Kind of question | Store | Tool | How it's chosen |
|---|---|---|---|
| row · count · range · filter · sort · aggregate (csv/json/parquet) | `kb/data_store/data.duckdb` (DuckDB, exact SQL) | `data_query` | request shape (numbers/filters/totals) |
| meaning · "explain" · "like this" · passage (long docs) | `kb/vector_db_docs/` (vector embeddings) | `docs_search` | request shape (semantic recall) |
| fact · decision · preference · todo | native memory files | `memory_search` | request shape (personal/durable facts) |

Because structured rows live in their **own DuckDB** and long documents live in their **own vector DB** — both physically separate from memory — **memory is never polluted** by table rows or document chunks, and each retrieval channel stays sharp at exactly what it's good at. The routing is declared in `AGENTS.md` (`## structured_data` + `## retrieval_router`) so the agent auto-detects intent every turn instead of guessing.

### Added
- **Structured-data store + registration (`procedures/data_register.py`).** Materializes a CSV, JSON, or Parquet file into a single DuckDB store (`kb/data_store/data.duckdb`) — **one table per dataset** (not a category column, not a database-per-category). DuckDB `DESCRIBE` infers column types; nested/semi-structured JSON is preserved as a queryable `STRUCT` (`meta.tier`, `meta.age` work directly). Every table is recorded in `catalog.json` with its schema, keywords, description, and a content hash. Supports `--list`, `--dry-run`, and `--refresh` (hash-skips unchanged sources, re-materializes changed ones, and prunes vanished sources incl. `DROP TABLE`).
- **Auto-profiled catalog (better routing + fewer bad queries).** At register/refresh, each column is profiled with pure SQL (no LLM): `distinct_count`, whether it's a low-cardinality **routing dimension** (`categorical`), and up to 12 `sample_values` for those dimensions. Unique integer keys and continuous numerics are correctly excluded from being "dimensions." **Keywords are auto-derived** from the table name + column names + word-like categorical values, so a natural-language request routes to the right table *even without hand-typed* `--keywords` (manual keywords are still merged first and preserved across refresh). The sampled values give the agent the actual value vocabulary to write correct `WHERE` clauses (it knows `region ∈ {US, EU, APAC}`) — directly reducing the bad-SQL failure mode.
- **Exact SQL retrieval (`tools/data_query.py`).** Read-only `SELECT` retrieval over the store: filter, range (`<`,`>`,`=`), sort, `COUNT`/`SUM`/`GROUP BY`, and cross-table/cross-format `JOIN`. `--list`/`--table` expose each table's columns + keywords so the agent routes to the right one; `--format text|json`; importable API (`data_query()`, `list_tables()`). `catalog.json` doubles as the intent router.
- **Two-layer read-only safety.** Queries are `SELECT`-only by construction: (1) a pre-execution guard strips comments, rejects multi-statement input (`SELECT 1; DROP TABLE …`), requires a leading `SELECT`/`WITH`, and blocks every write/DDL keyword (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`ATTACH`/`COPY`/`PRAGMA`/`INSTALL`/`LOAD`/…); (2) the DuckDB connection is opened `read_only=True`, so a write is rejected at the engine level even if the guard were ever bypassed. Bad SQL surfaces as a clean one-line `SQL error:` instead of a stack trace. The store is a retrieval surface, never a mutation path.
- **Fuzzy value routing (`data_query.py --suggest`).** Resolves an *inexact filter value* — typo, partial, wrong-case, or simple synonym/acronym — to a real stored value using DuckDB-native `jaro_winkler` + `levenshtein` + `ILIKE`-contains (plus an initialism boost), scored against the catalog's sampled column values. **No embeddings.** It *suggests* the correct `table.column = value` and an example `WHERE` clause — it never rewrites your SQL, so the agent stays in control. Directly attacks the "user typed the value slightly wrong" failure mode (`"united states"` → `region = 'US'`, `"Wiget"` → `product = 'Widget'`). Works on any Latin-script language (Indonesian included).
- **Per-prose-column semantic rank (`data_query.py --semantic`).** For a table with a free-**prose** column (survey feedback, reviews, support tickets), `data_register` auto-detects that column (text, *not* a low-cardinality dimension, avg length ≥ 25 chars) and builds a small per-column embedding index under `kb/data_store/text_index/<table>__<col>/` (reusing the same local `all-MiniLM-L6-v2`/Chroma stack as `docs_search`). `--semantic "<table>.<col>: <query>"` ranks rows by **meaning** and **joins the matched `rowid` back to the full structured row** — so "find rows about pricing complaints" flows straight into normal SQL filter/group. **Strictly opt-in per column and optional-dependency guarded**: if the embedding stack isn't installed, `--semantic` exits cleanly (code 3) and the exact-SQL path is entirely unaffected. This keeps the store *table-first with prose riding along*, not a second semantic store.
- **Behavioral routing + docs.** `AGENTS.md` gains `## structured_data` and a 5-way `## retrieval_router`; `TOOLS.md` gains `data_query`/`data_register` specs; README and `references/architecture.md` document the feature and the separate-stores model. `duckdb` is an **optional** dependency — if it's absent the feature is simply unavailable and the semantic/memory tools are completely unaffected.

### Fixed
- **Installer dependency-target correctness (buyer-install smoothness).** `install.sh` previously checked for modules with `python3 -c` but installed them with bare `pip` — on machines where `pip` and `python3` resolve to *different* interpreters, dependencies landed where the tools couldn't import them. All dependency installs now go through `"$PY" -m pip` (the same interpreter used for the import checks) with a PEP-668 `--break-system-packages` auto-retry for modern externally-managed distros. Applies to all dependencies, not just the new one.
- **`openclaw.plugin.json` version drift.** The manifest was stuck at `0.1.0` while `VERSION`/CHANGELOG had advanced; it now tracks the real version.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. No config changes required. To start using structured data: `python3 procedures/data_register.py --file <your.csv> --name <table> --keywords a,b,c`, then `python3 tools/data_query.py --sql "SELECT ..."`. Requires `duckdb` (the installer adds it; if it can't, only this feature is skipped).

---

## 1.4.0

Performance + install-correctness pass. **Recommended for everyone — especially larger memory corpora.** Makes the L2 graph build effectively instant, bounds L3 synthesis cost as memory grows, and fixes a first-install crash for the schema adapter.

### Fixed
- **Fresh installs could crash L2/L3 with `ModuleNotFoundError: _schema_adapter`.** `memory_graph.py` and `memory_synthesis.py` both import `_schema_adapter.py` (the schema-drift shim added in 1.3.5), but the installer's file-copy loop never shipped it — so a clean install left the two most important cron scripts unable to import. `graph_search.py` was missing from the same loop. Both are now copied by `install.sh`. Existing installs that already had the file are unaffected.

### Changed
- **L2 `build_graph` vectorized — ~461x faster, byte-identical output.** The former pure-Python O(n^2) double loop (cosine per pair) is replaced by a single BLAS `M @ M.T` cosine matrix + `lexsort` top-k selection. On the 3,017-chunk reference corpus this returns the exact same edge set (same pairs, same `sim`, same `rank_strength`) in ~0.57s vs ~265s. The full L2 run (including forward-refs gating below) drops from ~99s to ~1.5s of graph-build time. numpy is now a hard dependency (previously only transitive via pandas) and `install.sh` installs it explicitly.
- **`forward_refs` scan is now node-count gated (`--forward-refs-max-nodes`, default 1500).** The forward-reference report is O(terms x nodes) pure-Python fuzzy matching with no clean fast+exact form, so above the threshold it would dominate an otherwise-instant build for a secondary report. Below the threshold it runs the identical original algorithm (zero fidelity change); above it, the scan is skipped with a note and the graph is unaffected. `0` = always run, large value = never skip.
- **L3 synthesis cost is now bounded as the corpus grows.** Added `--cluster-batch N` (rotating `.cluster_cursor.json`, full sweep every `ceil(total/N)` runs) plus per-cluster dirty-skip (`.cluster_cache.json` content-hash; unchanged clusters skip the LLM, default on, cache auto-prunes to the live cluster set). `--cluster-batch 0` (default) preserves the exact legacy behavior — every cluster every run.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. No config changes required; new flags default to prior behavior.

---

## 1.3.5

Compatibility fix. **Recommended for anyone whose L2 graph / L3 synthesis crons error out.** Makes neuron tolerant of OpenClaw memory-index schema drift so `memory_graph.py` and `memory_synthesis.py` keep working across OpenClaw versions.

### Fixed
- **OpenClaw memory-index schema drift broke L2/L3.** `memory_graph.py` and `memory_synthesis.py` read the sqlite index OpenClaw's native memory search builds at `~/.openclaw/memory/<agent-id>.sqlite`. That schema is owned by OpenClaw, not dinomem, and some builds expose different table/column names (e.g. `memory_index_chunks` instead of `chunks`). The hard-coded `SELECT ... FROM chunks` then threw `no such table` / `no such column` and L2/L3 went dark while every other cron kept running. Both scripts now resolve the real table/column names at runtime via a new `_schema_adapter.py`, which introspects `sqlite_master` + `PRAGMA table_info` against a table of known aliases. Verified column-for-column identical to the previous SQL on the reference schema (files/chunks) with zero regression, and resilient to virtual FTS/vec tables. Unknown schemas now fail loud with a full schema dump for one-shot diagnosis instead of a cryptic sqlite error.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`.

---

## 1.3.4

Memory-quality + reliability pass. **Recommended for all users.** Sharpens the done-note lifecycle so retiring notes enrich normal memory instead of piling up frozen pins, adds an atomic note-claim writer to close the cron/live-session race, and biases the memory pruner to keep well-connected hub facts.

### Added
- **Done-note distiller.** When a verified `status: done` note is retired, its knowledge is now distilled into proper schema memory entries (via `extract_from_text`, seeded `verdict: valid`) BEFORE the note file is deleted — so the build stays retrievable and enters the normal retention review like everything else. Runs as a separate cheap no-reasoning subprocess; `resolve_done_notes.py` stays zero-LLM. Best-effort (a distill failure still deletes the note, same as before). Toggle with `DINOMEM_DISTILL_DONE_NOTES=0`.
- **Atomic note-claim writer (`scripts/claim_note.sh`).** Closes the one-sided claim handshake: the Advancer/Improver/Deleter crons already READ `claimed_by`/`claimed_at` and back off, but nothing WROTE a live-session claim — so a cron could grab a note mid-conversation and double-edit it. The live agent now stamps a claim before editing (atomic temp-file + `mv` + `flock`; `live-session-*` claims honored 120 min, others 30 min, future-dated treated as stale). Installer wires the `note_claim_protocol` into the managed `AGENTS.md`.
- **Memory connectivity keep-bias.** The neuron memory pruner now soft-rescues cross-file “hub” entries (facts referenced from multiple memory files) when they are only marginally-valid/uncertain — preventing well-connected context from being culled just because any single mention scored low.

### Changed
- **`promote: pin` hatch REMOVED from note retirement.** Retiring notes now ALWAYS distill → regular memory (freeze on merit), never copy the whole note into a `_pin_`. The hatch predated verdict-based retention (when distill-to-regular meant eventual decay, so pinning was the only way to keep anything); now regular memory freezes if it keeps earning `valid`, so the hatch only produced junk pins. Genuine pins (credentials, architecture, do-not-repeat findings) are authored DIRECTLY as `_pin_` files — never born as `_note_` then promoted — so removing it blocks nothing real and closes the primary junk-pin source. Deleter + Daily Note Review cron prompts updated to match.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. The installer refreshes the dinomem base first (dependency), then re-registers crons and redeploys the helper scripts idempotently — one step covers base + neuron.

---

## 1.3.3

Base-dependency upgrade. **Recommended for all users** — pulls in two dinomem base fixes that recover memory silently lost on manual `/new` / `/reset`. No neuron feature change; re-running the neuron installer refreshes the base automatically.

### Changed (via base refresh)
- **dinomem base 1.2.11 — silent permanent memory loss on manual `/new` / `/reset` FIXED.** OpenClaw core archives a manually-reset session in real time to `<session>.jsonl.reset.<ISOms>Z`, but the memory pipeline only globbed `*.archived.*.jsonl` and explicitly skipped any `.reset.` name — so **any session you `/new` or `/reset` had its transcript archived but never extracted into `memory/` (permanent loss).** Base now adopts core's reset-archives into the pipeline-visible namespace (new `adopt_core_reset_archives()` step) so `extract_memory` (and neuron `session_ingest`) pick them up. First run after upgrade **backfills** every historical stranded reset-archive.
- **dinomem base 1.2.12 — 0-delay extraction on manual reset.** New `dinomem-reset-extract` internal hook (`command:new` / `command:reset`) fires the memory pipeline the instant you `/new`, instead of waiting up to 15 min for the next cron tick.

### Upgrade
Re-run the neuron installer (`bash scripts/install.sh`). It refreshes the dinomem base first (git pull + base install), so both base fixes land automatically — no separate base step. The first base cron run after upgrade backfills any historical manual-reset sessions.

## 1.3.0

Self-improving project closer. **New feature. Recommended for all users.**

### Added
- **Self-improving done-project closer (two cooperating crons).** Completed project notes are now automatically improved and retired without cluttering the workspace:
  - **Project Improver (Component 2)** — LLM cron (`:20`/`:50`). Fires only on `status: done` project notes not yet stamped `verified: true`. Domain-agnostic and behavior-preserving: it assesses whether the project can be made cheaper / faster / clearer / more reliable, makes only that improvement (no prescribed checklist — "nothing needed" is a valid outcome), validates the artifact still satisfies its `done_when`, then stamps `verified: true`. A zero-LLM pre-gate (`scripts/check_improvable.sh`) skips the LLM entirely when there is nothing to improve. A hard safety gate blocks external/destructive steps (git push, sends, spend, credentials) unattended — it does the local work, then pauses for owner approval.
  - **Verified Note Deleter (Component 1)** — deterministic janitor cron (`:25`/`:55`), a thin wrapper over `scripts/resolve_done_notes.py`. Deletes ONLY notes carrying `verified: true`, snapshotting each first and promoting any `promote: pin` note to `_pin_`. Non-blocking `flock` so it never races the Advancer/Improver.
- **Shared claim protocol across all note jobs.** Advancer, Improver, and Deleter honor `claimed_by` / `claimed_at` (UTC ISO8601): live-session claims are respected for 120 min (2h), other-claimant claims for 30 min; stale and future-dated claims are cleared. This prevents concurrent `_note_*.md` mutation — including a cron waking mid-conversation and double-editing a note a human is actively working.

### Upgrade
Unzip the new archive over your install and run `scripts/install.sh --force`. The two new crons register idempotently.

---

## 1.2.5

Memory reliability fixes. **Recommended for all users.**

### Fixed
- **MEMORY.md bloat on heavy work-days (`generate_topic_index.py`).** The "Previous Session" keyword line was built from the slug path with no per-item cap — a day with many dated memory files produced hundreds of slugs joined into one multi-KB line that the size guard couldn't trim (it trims whole lines, not within a line). Result: MEMORY.md could balloon well past the bootstrap char cap and get silently truncated on injection. Fix A: cap the slug path (15 items / 600 chars), symmetric with the already-capped LLM/word-freq paths. Fix B: `_guard_memory_size` now also collapses any single over-long line as a safety net, not just the `## Searchable` section.
- **Silent extraction failure on multi-Node hosts / permanent loss on transient LLM failure** (shared with dinomem base). Node resolved at runtime (>= 22.19), failed extractions retry instead of being marked processed, tolerant JSON parsing of gateway stdout. Ensure dinomem base is also updated (>= 1.2.9).

### Upgrade
Unzip the new archive over your install and run `scripts/install.sh --force`. Also update dinomem base to >= 1.2.9.

---

## Unreleased

### Removed
- **Calendar integration fully retired.** `calendar_sync.py`, `calendar_tool.py`, and `calendar_oauth_setup.py` deleted; calendar cron entry removed; AGENTS.md calendar block removed; all calendar references removed from docs.

---

## 1.2.4 — 2026-06-29

### Added
- **`investigate_before_act` policy block (injected into AGENTS.md).** Codifies
  the verify-before-mutate discipline: inspect the real artifact, reproduce
  locally before fixing, re-verify with evidence after fixing, check cross-entity
  claims against the target directly (shared lineage != shared bug), and treat a
  prior "it's fixed" claim as non-evidence. Sits beside `project_note.estimate`
  as the pre-build/pre-fix gate. Base (memory-only) is intentionally left lean.

## 1.2.3 — 2026-06-29

### Fixed
- **`install.sh` hard-failed while wiring AGENTS.md (`line NNN: title: No such
  file or directory`, exit 1).** The AGENTS.md/TOOLS.md managed blocks were built
  as double-quoted shell strings (`BLOCK="$BEGIN ..."`) containing **unescaped**
  double-quotes mid-string — e.g. `calendar_tool.py create "<title>"`,
  `session_search.py "<query>"`, and `done_when ("pushed", "told user")`. Each
  unescaped quote closed the string early, leaving the following `<title>` /
  `<query>` placeholder in shell command position where `<` parsed as a
  redirection. Under `set -e` the install aborted before AGENTS.md was wired, so
  neuron's policy/tool blocks were never installed. (Same bug class fixed in base
  dinomem v1.2.8; neuron's variant hard-fails instead of degrading.) Fixed by
  building both blocks as quoted heredocs (`cat <<'EOF'`), which makes inner
  quotes and `<...>` placeholders literal. Generated content is byte-for-byte
  unchanged.

## 1.2.2 — 2026-06-25

### Added
- **Post-install re-surfaces the model-selection recommendation at the right
  time.** The split (cheap bulk + compaction, default+thinking for reasoning) is
  manual and only becomes *meaningful* once neuron is installed — but neuron's
  installer never mentioned it, so users who deferred it at base install forgot
  to set anything. Neuron post-install now prints a `MODEL_HINT` /
  `AGENT_ACTION` block framed as “now relevant — set it now or it won't happen,”
  covering `DINOMEM_CHEAP_MODEL` + `compaction.model` (bulk) and the
  default-model reasoning tier. Closes the recommendation-timing gap: base
  install plants it, neuron install (when the reasoning tier appears) repeats it.

## 1.2.1 — 2026-06-25

### Fixed
- **Enforce dinomem base floor at 1.2.0 (was 1.0.0).** Install order is usually
  base-first, sometimes an *old* base. Below base 1.2.0, neuron's `reasoning=True`
  calls are accepted but silently ignored (no thinking budget, no error) —
  reasoning quality degrades invisibly. The installer's existing semver gate now
  requires `1.2.0`, so an old base triggers a clear “update dinomem first” warning
  instead of quietly weakening synthesis/contradiction/promotion. README updated
  to explain the silent-degradation risk.

## 1.2.0 — 2026-06-25

### Fixed
- **Reasoning calls now actually request reasoning.** The 5 LLM call sites in the
  reasoning tier passed no `reasoning=` flag, so they defaulted to `False` and ran
  without a thinking budget — a latent mistag. Flipped to `reasoning=True`:
  - `memory_synthesis.py` ×3 (insight synthesis 600t, merge 1500t, audit 400t)
  - `contradiction_check.py` ×1 (conflict check 400t)
  - `memory_promote.py` ×1 (permanent-promotion validity verdict)
  With dinomem base v1.2.0+, `reasoning=True` routes these to the default model
  with `--thinking` (`DINOMEM_REASONING_THINKING`, default `high`) and keeps them
  off `DINOMEM_CHEAP_MODEL`.

### Added
- **README “Model selection” section** (under “How it all works”): documents that
  the neuron pipeline is the reasoning tier — always default model + thinking,
  never the cheap lever — and points to dinomem base's cheap-model knob. Notes the
  base **v1.2.0+** requirement for the routing to take effect.

## 1.1.5 — 2026-06-24

### Changed
- README “What we mean by learning”: added a closing thesis paragraph — because
  synthesis is an LLM finding patterns across memory (not fine-tuned weights), the
  patterns get deeper and promotion more accurate every time the base model improves,
  with no retraining or rewrite. Fine-tuning approaches redo work each model
  generation; neuron inherits the gain for free.

## 1.1.4 — 2026-06-24

### Changed
- README: replaced timezone-assuming “every night” / “tonight’s cron” wording with
  neutral, accurate phrasing (“on a daily cron” / “once a day” / “next daily cron”).
  The synthesis/graph-build runs on a daily schedule — “night” is relative to the
  reader’s timezone and was both ambiguous and slightly inaccurate.

## 1.1.3 — 2026-06-24

### Changed
- **README repositioned.** New title “Self-Improving Memory for AI Agents” — leads
  with neuron’s differentiator (behavior change via synthesis + promotion +
  injection, no model-weight updates) instead of the generic “Dino Agent Memory
  (Neuron Layer)”.
- **Real subtitle.** Replaced the bare install-prerequisite line (“Requires dinomem
  installed first”) with a value-prop subtitle: nightly pattern-finding → promotion to
  permanent knowledge → every-turn injection, with the January↔November example. The
  dinomem dependency is now carried as value (“learning layer on top of dinomem”).
- Brand language aligned across products: dinotrust = self-enforcing,
  dinomem = self-curating, dinomem-neuron = self-improving / learns-without-fine-tuning.

## [1.1.2] — 2026-06-23

### Fixed
- **`session_ingest` model resolution** — the last remaining hardcoded HF
  snapshot hash (`8b3219…`). Now resolves the local MiniLM snapshot dynamically,
  identical to `session_search`, so ingested session vectors always align with
  query vectors. This file was missed in 1.1.1 and was silently broken on any
  machine whose HF cache pinned a different revision (the case on the current
  host — only `1110a24…` existed, so the hardcoded `8b3219…` never loaded).
- **`generate_topic_index` openclaw binary resolution** — replaced a raw
  hardcoded `/home/linuxbrew/.linuxbrew/bin/openclaw` (no PATH lookup) with a
  `shutil.which("openclaw")`-first resolver + common-location fallbacks, matching
  the rest of the codebase. Previously broke topic-index generation on
  npm-global / non-linuxbrew installs.

### Audit
- Full sweep of both dinomem (public) and dinomem-neuron (private) for hardcoded
  paths/hashes (portability problem #1): **zero** hardcoded HF snapshot hashes
  remain; all openclaw-binary references are now PATH-first with fallbacks. The
  `DINOMEM_*_PLACEHOLDER` tokens are by-design (install.sh sed-replaces every
  token used) and verified complete in both repos. Public repo has no embedding
  model dependency, so it was never affected by the hash bug.

---

## [1.1.1] — 2026-06-23

### Fixed
- **`docs_search` + `docs_ingest` model resolution** — removed the hardcoded HF
  snapshot hash + `DINOMEM_HF_CACHE_PLACEHOLDER` (install-time sed). Both now
  resolve the local MiniLM snapshot dynamically (glob snapshots dir, honor
  `HF_HOME`, fall back to the model id). Ingest and search resolve the SAME
  snapshot so vectors always align. This was a silent-death bug on fresh VPS
  installs where the cached revision differed from the pinned hash.
- **`docs_search` filtered queries** — `--doc` / `--section` filters are applied
  post-fetch, so a narrow filter could return fewer than `k` results even when
  matching chunks existed deeper in the ranking. Fetch now widens progressively
  (k×4 → up to 400) when a filter is active and the set is short, until `k` is
  filled or the corpus is exhausted. No extra cost on unfiltered queries.

### Changed
- **`docs_search` HyDE** — stays default ON (better recall on formal docs) but
  optimized: successful expansions are cached on disk for 24h (repeat query =
  instant, no LLM call), timeout raised to 25s and made env-configurable
  (`DOCS_HYDE_TIMEOUT`, set `0` to disable). Fallbacks are not cached, so a
  transient gateway outage never pins an un-expanded query.

---

## [1.1.0] — 2026-06-23

### Added
- **`session_search` recency rerank** — light, score-gap-based recency tie-breaker.
  Among similarly-relevant sessions the fresher one wins (sessions are usually
  recalled to find the *most recent* discussion of a topic), while a clearly
  more-relevant older session is never buried. Blends on normalized similarity
  score gap (not rank position): small gap → recency decides, large gap →
  relevance dominates. Half-life ~3d, weight 0.25. No recency floor / no
  intraday mode (recall, not news). Over-fetch raised to k×3 for rerank headroom.

### Fixed
- `session_search`: resolve the local MiniLM snapshot dir dynamically instead of
  hardcoding a snapshot hash (hardcoded hashes break silently when the HF cache
  pins a different revision on another machine); falls back to the model id.

---

## [1.0.0] — 2026-06-18

### Added
- Pre-flight checks: Python version, workspace writable, root files size threshold (10k/8k per file)

### Fixed
- Removed `contradiction_alerts` from AGENTS.md injection — behavior is auto-block, not surfaced to user

---

## [0.9.0] — 2026-06-17

### Added
- **Session deep recall** — `session_ingest.py` + `session_search` tool + cron + AGENTS.md wiring
- Injection ranking: reinforce × recency score (replaces reinforce-only sort)
- `calendar_sync.py`: auto-create calendar events from `_note_` date field + dedup log
- `MAX_PERSISTENT_RULES` tuned 12 → 15 for 200k context window

### Fixed
- `memory_promote.py`: replace hardcoded `/root/` path with placeholder (portable across users)
- `session_ingest`: parse `entry.message.role` not `entry.role` (correct OpenClaw JSONL format)
- `session_deep_recall` trigger: tightened conditions, removed redundant fallback line

---

## [0.8.0] — 2026-06-15

### Added
- **HyDE query expansion** in `docs_search.py` — hypothetical document embedding for better long-doc recall
- **Recency section** in MEMORY.md — previous session topics injected via `generate_topic_index.py`
- Calendar integration block in AGENTS.md injection
- Concurrency lockfile in `memory_promote.py`
- `_pin_` files exempt from decay

### Fixed
- `generate_topic_index` fallback regex supports per-item filename format (`YYYY-MM-DD_type_slug.md`)
- AGENTS.md injection block: stripped examples and prose, machine-readable only
- Uninstall: remove `memory/_permanent.md` on `--purge-data`

---

## [0.7.0] — 2026-06-14

### Added
- **Hybrid decay** — `reinforce_count` multiplier + LLM review rescue before expiry
- **Category-based TTL decay** — preference/decision/lesson live longer than prediction/uncertain
- Pre-flight compatibility checks + Compatibility section in README
- AGENTS.md block: RAG routing + contradiction alerts
- Calendar tool: reminders and attendees support, 30min popup baseline always included

### Fixed
- Uninstall: now removes AGENTS.md neuron block and all neuron crons
- Contradiction resolution description in FAQ
- LICENSE: replaced username with correct author name

---

## [0.1.0] — 2026-06-13

### Added
- Initial release
