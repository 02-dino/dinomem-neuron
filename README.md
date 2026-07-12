# 🧠 dinomem-neuron — Self-Improving Memory for AI Agents

> The learning layer on top of [dinomem](https://github.com/02-dino/dinomem). On a daily cron it finds patterns across everything you've told your agent, promotes the stable ones into permanent knowledge, and injects them every turn — so your agent connects January's decision to November's pattern, without touching model weights.

Once a day, it builds a graph of how your memories relate, clusters them, and runs synthesis — an LLM pass that finds patterns across everything you've ever told your agent. Insights are rigorously evaluated before being promoted to permanent knowledge. And it's injected every turn, so those insights actually influence behavior.

The result: an agent that connects a decision you made in January to a pattern from November. That notices you've been contradicting yourself. That builds a structured knowledge base from your sessions. It changes behavior based on all of that—without you writing a single line of code.

---

## What we mean by learning

Neuron does not fine-tune model weights.

Instead it learns by:

1. Observing experiences (memory files from sessions)
2. Identifying patterns across memory clusters (synthesis)
3. Evaluating those patterns across multiple signals — confidence, reinforcement strength (recurrence across synthesis runs), contradiction checks, insight lifecycle
4. Promoting stable patterns into long-term knowledge (permanent files)
5. Changing future behavior based on that knowledge (injected every turn)

```
Experiences → Patterns → Promotion → Behavior Change
```

**This compounds with model quality.** Synthesis is an LLM finding patterns across your memory — not weights you fine-tune. Every time the base model gets smarter, the patterns get deeper and promotion gets more accurate, with no retraining and no rewrite. Fine-tuning-based approaches have to redo their work each model generation; neuron just inherits the gain for free.

---

## How behavior changes

Promoted insights are written into `topics/PERSISTENT_AUTO.md` and `memory/_permanent.md`. These feed into `MEMORY.md` — the root file that OpenClaw injects into the agent's context on every turn.

Unlike ordinary memories (recalled on demand via `memory_search`), promoted insights are **present on every turn** via `MEMORY.md`. Patterns discovered from experience become part of the agent's baseline behavior — without you writing a single config line.

---

## Example

**Raw memories (extracted over months):**
```
January:  "User prefers concentrated positions."
February: "User only holds 5 stocks max."
March:    "User dislikes broad diversification."
```

**L3 synthesis finds the pattern:**
```
"User consistently prefers concentrated portfolios."
```

**After passing multi-signal evaluation (confidence, reinforcement/recurrence, contradiction, lifecycle) — promoted:
```
topics/PERSISTENT_AUTO.md:
"User prefers concentrated investing."
```

This insight is now injected every turn. Future investing discussions automatically reflect this preference — without recall, without prompting.

---

## What it adds

| | Feature | What it does |
|-|---------|-------------|
| 🔗 | **Relationship Discovery** (L2) | Finds which memories are related — even across different conversations |
| 💡 | **Pattern Synthesis** (L3) | Clusters related memories → LLM synthesizes emergent insights you never explicitly stated |
| ⚠️ | **Contradiction detection** (L3) | Flags when new insights conflict with existing ones |
| 📌 | **Knowledge Promotion** (L4) | Insights that pass multi-signal evaluation (confidence, reinforcement/recurrence, contradiction, insight lifecycle) → written into always-injected context → behavior change |
| 🗺️ | **Enriched navigation index** | `MEMORY.md` serves dual roles: search navigation aid (like dinomem base) + always-present behavior layer. Promoted insights live here — active every turn without recall. |
| 📚 | **RAG for long docs** | Semantic search over contracts, books, legal text — stored separately, never pollutes memory |
| 📊 | **Structured data** | Exact SQL retrieval over CSV/JSON/Parquet — filter, range (`<`,`>`,`=`), sort, `COUNT`/`SUM`/`GROUP BY` — the precise questions embeddings can't answer. Plus optional **fuzzy value routing** (`--suggest`, resolves typo/synonym filter values, no embeddings) and optional **per-prose-column semantic rank** (`--semantic`, "find rows about X" over free-text columns like survey feedback). Read-only; never pollutes memory |
| 🤖 | **Automatic notes** | The agent writes `_note_` files from its own commitments ("next step", "TODO") and task follow-ups — not just when you ask |
| 🏗️ | **Project executor** | Large builds become `type: project` notes: the agent estimates token cost upfront and splits the work if it won't fit the remaining budget, then executes one step per turn (with a reactive context checkpoint as a safety net) so nothing is lost across sessions. A Project Advancer cron auto-advances unfinished projects unattended and announces progress, pausing for approval on anything destructive. |
| ♻️ | **Self-improving project closer** | When a project finishes, a Project Improver cron reviews the completed work and — if it makes sense for *that* project — makes a behavior-preserving improvement (cheaper/faster/clearer/more reliable; "nothing needed" is a valid outcome), validates it, then stamps the note `verified`. A Verified Note Deleter then retires verified notes (snapshotting first). External/destructive improvements stay gated for owner approval. |
| 🧹 | **Daily Note Review** | A daily janitor cron retires *any* resolved note — `task_bound` notes whose `done_when` is met, projects finished out-of-band, and abandoned notes past `stale_after` — so completed work doesn't pile up. Claim-aware: never touches a note another job or a live session is actively editing. |
| 🔍 | **Session deep recall** | When memory summary is thin, searches raw archived sessions (7-day window) for the exact exchange — sharper, more detailed recall for recent context |

---

## Quick Start

You have access to this repo. Here's how to get neuron running.

**Step 1 — dinomem must be installed first**
```bash
git clone https://github.com/02-dino/dinomem
bash dinomem/scripts/install.sh \
  --workspace ~/.openclaw/workspace-myagent \
  --agent-id myagent
openclaw gateway restart
```
Verify: `memory/*.md` files exist in your workspace.

**Step 2 — install dinomem-neuron**

If you have repo access:
```bash
git clone https://github.com/02-dino/dinomem-neuron
bash dinomem-neuron/scripts/install.sh \
  --workspace ~/.openclaw/workspace-myagent \
  --agent-id myagent \
  --agree
openclaw gateway restart
```

If you received a zip file:
```bash
unzip dinomem-neuron.zip
bash dinomem-neuron/scripts/install.sh \
  --workspace ~/.openclaw/workspace-myagent \
  --agent-id myagent \
  --agree
openclaw gateway restart
```

> `--agree` accepts the license non-interactively. Required when installing via AI agent or automation.

**Step 3 — verify**
```bash
# TEI embedding server running
curl http://localhost:8080/health
# → {"status":"ok"}

# Neuron crons registered
crontab -l | grep memory_graph
crontab -l | grep memory_synthesis
```

**Step 4 — run first graph build manually** (optional, so you don't wait for the next daily cron)
```bash
python3 ~/.openclaw/workspace-myagent/procedures/memory_graph.py
python3 ~/.openclaw/workspace-myagent/procedures/memory_synthesis.py
```

> Don't know your agent ID? Run `openclaw agents list` — it shows all agents and their workspace paths.

That's it. Crons handle everything from here.

---

## Prerequisites

- [ ] [dinomem](https://github.com/02-dino/dinomem) installed and working
- [ ] TEI embedding server running (`curl http://localhost:8080/health` → `{"status":"ok"}`)
- [ ] At least a few sessions already extracted (`memory/*.md` files exist)
- [ ] Python 3.8+

### Minimum spec

Neuron rides on the dinomem base (TEI already installed) and adds a daily L2 graph build + L3 synthesis. L3 calls an LLM via your agent's model — a remote API, so no local GPU/RAM burden. The only local bump over base is headroom for graph traversal + embedding churn during synthesis runs.

| Resource | Minimum (base + neuron) | Comfortable |
| -------- | ----------------------- | ----------- |
| CPU      | 2 vCPU                  | 4 vCPU      |
| RAM      | 3 GB                    | 4–8 GB      |
| Disk     | 5 GB free               | 10 GB       |

Disk barely moves (graph JSON is small; docs vector DB is ~1–2 MB per 1000 chunks). The installer preflight **warns** on below-minimum RAM/CPU and continues — no disk block here, since neuron adds no large local download.

**Agent-driven installs.** Because a stdout warning is easy for an automated caller to skim past, the preflight also emits a machine-readable `DINOMEM_NEURON_PREFLIGHT_WARN=...` line and an `[AGENT-NOTICE]` marker when spec is below recommended. An agent running the installer should **surface this to the user before proceeding** rather than silently continuing.

> **Verify dinomem is working first.** Run `python3 <workspace>/procedures/auto_session_reset.py` and confirm `memory/*.md` files exist before installing neuron.

### Token usage

Recurring cost is LLM tokens only. Figures below are **cumulative — dinomem base included** (neuron runs on top of base, not instead of it). Estimated tokens/month (input+output), grounded in the actual batch caps:

| Usage | Non-reasoning (base + neuron L4) | Reasoning (neuron L3) | Total |
| ----------------------- | -------------------------------- | --------------------- | ------- |
| Low — ~2–3 sessions/day | ~1–2M                            | ~1–2M                 | ~2–4M   |
| Moderate — ~8–12/day    | ~5–9M                            | ~4–7M                 | ~9–16M  |
| High — ~30+/day         | ~18–30M                          | ~10–15M               | ~28–45M |

**Non-reasoning** = base bulk (extraction, review) **+ neuron's L4** (`memory_promote` valid/invalidated check + `generate_topic_index`) — all route to [`DINOMEM_CHEAP_MODEL`](#model-selection) if set. **Reasoning** = neuron's **L3** synthesis + contradiction check, which run on your **default** (reasoning) model by design — the cheap lever can't touch these. Graph/L2 = **0 tokens** (pure local cosine).

_Grounding: extract ~26k tok / 3 sessions · review ~4k/day · synthesis `nodes//10` clusters/day (cap 40) × ~1.2k tok · order-of-magnitude, scales with sessions/day + corpus size._

---

## How it all works

```
[dinomem base — every 15 min]
Session → Archive → extract_memory.py → memory/*.md + MEMORY.md + vector DB
        │
        │  daily
        ▼
[L2: Relationship Discovery — memory_graph.py]
  Reads embeddings from local vector DB
  Builds a graph: nodes = memory entries, edges = similarity above threshold
  Edge weight = composite neural_score: cosine sim × rank strength × recurrence weight × L3 confidence boost
  Edges that persist across multiple graph rebuilds are stronger (Hebbian principle)
  Feedback-loop firewall: skips L4-promoted chunks so synthesis never reads its own conclusions back
        │
        │  daily
        ▼
[L3: Pattern Synthesis — memory_synthesis.py]
  Reads L2 graph clusters (cross-file, medium-similarity edges only — near-duplicates excluded)
  For each cluster: LLM synthesizes an emergent insight (skeptical prompt — returns NONE if no real pattern)
  3-stage gate before promotion-eligible:
    Stage 1 — LLM audit: confidence must exceed threshold; uncertain insights penalized
    Stage 2 — Convergence: same pattern must surface from ≥2 independent clusters
    Stage 3 — Recurrence: underlying edges must have persisted across ≥3 graph rebuilds
  Deduplicates against existing insights
        │
        ▼
[L3: Contradiction Detection — contradiction_check.py]
  LLM checks new insights against existing promoted knowledge for conflicts
  Blocking contradictions → queued, not promoted
  Warnings → promoted but flagged
        │
        │  weekly
        ▼
[L4: Knowledge Promotion — memory_promote.py]
  Reads insights that survived N synthesis cycles
  Multi-signal evaluation: confidence + reinforcement + contradiction status + lifecycle
  Category-aware TTL: preferences decay slower than predictions; lessons slower than uncertain insights
  Reinforcement multiplier: insights reinforced 10+ times get 2× TTL extension
  LLM review rescue: every 60 days, LLM judges if a decaying insight is still valid
  Graduation rate-limited: max 2 promotions per run, hard cap of 15 permanent rules
  Insights that pass all signals → promoted to permanent knowledge
        │
        ▼
[L4: Index Generation — generate_topic_index.py]
  Reads manual + auto-promoted topics
  Builds structured topic tree
  Writes MEMORY.md — enriched with promoted insights at the top
  MEMORY.md serves two roles: search navigation aid + always-present behavioral layer
        │
        ▼
[Root context injection — every turn]
  Promoted insights present in every agent turn
  No recall needed — behavior change is baseline, not on-demand
        │
        ▼
[Behavior change]
  Promoted insights present on every turn — no recall needed
```

### Model selection

neuron's **L3** is the **reasoning tier**. `memory_synthesis` and `contradiction_check`
make validity/judgment calls (emergent-insight synthesis, contradiction logic) — so
they run `reasoning=True`: your OpenClaw **default** model plus a thinking budget
(`DINOMEM_REASONING_THINKING`, default `high`). They ignore `DINOMEM_CHEAP_MODEL`
on purpose: routing these to a cheap model risks cementing stale insights or
deleting good memory.

neuron's **L4** is **non-reasoning**. `memory_promote` is a one-word
valid/invalidated check and `generate_topic_index` is mechanical tree assembly —
neither requests a thinking budget, and both honor `DINOMEM_CHEAP_MODEL` (same
bulk tier as base). They run cheap by design.

The cheap-model lever (`DINOMEM_CHEAP_MODEL`) affects all no-reasoning bulk
scripts — dinomem base's (`extract_memory`, `memory_review`) **and** neuron's L4
(`memory_promote`, `generate_topic_index`). Only L3 (`memory_synthesis`,
`contradiction_check`) ignores it. See
[dinomem → Model selection](https://github.com/02-dino/dinomem#model-selection).
These flags live in `extract_memory.call_llm`, which neuron imports — so dinomem
base **v1.2.0+ is required**. neuron's installer enforces this floor. The catch:
most people install base **first**, sometimes an older one. On base below 1.2.0
the `reasoning=True` flag is *accepted but silently ignored* (no thinking budget,
no error) — reasoning quality quietly degrades. If neuron's installer warns that
your base is too old, run `dinomem/scripts/update.sh` before using neuron.

---

## Beyond the pipeline

The L2/L3/L4 flow above is neuron's core: turning raw memory into validated, behavior-changing knowledge. Riding on that same base are five capabilities that aren't part of the synthesis engine but extend what the agent can do day to day.

### RAG for long documents

Contracts, books, legal text, manuals — anything too long to live in memory. `docs_ingest.py` chunks and embeds them into a separate vector store (`kb/vector_db_docs/`), kept apart from memory so long docs never pollute the synthesis pipeline. The agent retrieves passages via `docs_search.py`. Re-ingested daily so new/changed files stay searchable.

**Hybrid retrieval — three complementary channels, all local, no cloud call:**

1. **Semantic** — embedding similarity over the vector store, with **HyDE query expansion** on by default (the query is rewritten into document-style phrasing before embedding, pulling the vector toward how the answer is actually written).
2. **Lexical (BM25 + RRF)** — a keyword channel over the same chunks, fused with the semantic ranking via Reciprocal Rank Fusion. Semantic embeddings are fuzzy on *exact tokens* — literal citations (`Pasal 1338`), defined terms, article numbers, proper nouns — which is exactly what legal/contract queries lean on. BM25 gives those a dedicated channel so an exact-citation hit the semantic arm blurred still surfaces. Zero extra dependency (stdlib Okapi BM25 over the existing corpus), and parameter-free in practice (RRF constant defaults to the standard `60`). Disable with `DOCS_BM25=0`; tune the fusion constant with `DOCS_RRF_K`.
3. **Structural** — entity-graph augmentation appends chunks that share named entities with the top hits, surfacing related passages the query never named.

The three are additive: turning any one off leaves the others intact.

**Seamless ingest.** You don't drop files in a folder or convert anything. Share a document in chat — PDF, Word (`.docx`), `.rtf`, `.odt`, HTML, EPUB, or plain text/markdown — and the agent detects the intent, extracts it to markdown, and ingests it automatically, ready for `docs_search`. This works on any chat channel that downloads attachments to OpenClaw's shared media store (Telegram, WhatsApp, Discord, Slack, and others), not just one. PDF extraction uses `pdftotext` and Office/HTML uses `pandoc`; the installer auto-installs both when possible, and if either is unavailable, text/markdown ingest still works while the affected formats degrade gracefully with a clear message.

### Structured data

RAG answers *"what does this say?"* — structured data answers *"how many, which ones, what's the total."* Embeddings are fuzzy on exact numbers, ranges, and counts; a spreadsheet question like *"orders under \$50 in the US, sorted by date"* needs real `WHERE`/`ORDER BY`/`SUM`, not similarity. This capability is the exact-retrieval counterweight to the semantic pipeline.

`data_register.py` materializes a CSV, JSON, or Parquet file into a single DuckDB store (`kb/data_store/data.duckdb`) — **one table per dataset** (not a category column, not a DB-per-category) — and records its schema + routing keywords in `catalog.json`. Nested/semi-structured JSON is handled natively (`meta.tier`, `meta.age` are directly queryable). `data_query.py` then runs exact SQL:

```bash
# register a dataset (schema inferred, catalog updated)
python3 procedures/data_register.py --file sales.csv --name sales --desc "Q3 orders" --keywords sales,revenue

# query it — filter / range / aggregate / group-by
python3 tools/data_query.py --sql "SELECT region, SUM(price*qty) FROM sales WHERE price < 50 GROUP BY region"
```

**Read-only by construction — two layers.** Queries are SELECT-only: a keyword guard rejects any `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`ATTACH`/`COPY`/`PRAGMA`/etc. and multi-statement injection (`SELECT 1; DROP TABLE …`) before execution, and the DuckDB connection itself is opened `read_only=True` so a write is rejected at the engine level even if the guard were bypassed. The store is a retrieval surface, never a mutation path.

**Routing.** `catalog.json` doubles as the intent router — `--list` shows every table's columns + keywords so the agent picks the right one. The behavior policy (`## structured_data` / `## retrieval_router`) draws the line: *row · count · range · filter · sort · aggregate → `data_query`*, while *meaning · "like this" · passage → `docs_search`* and *fact · decision · preference → `memory_search`*. Rows are never saved to memory. `--refresh` re-scans sources (hash-skips unchanged, updates changed, prunes vanished). Requires `duckdb`; if it's not installed the feature is simply unavailable and the semantic tools are unaffected.

### Automatic note creation

Base note creation is user-driven ("remember to X"). Neuron makes it automatic on three triggers:
- **Self-commitment** — when the agent's own output implies deferred work ("next step…", "TODO", "remaining: X"), it writes a `_note_` for that commitment. Closes the gap where an agent promises something and a later session forgets.
- **Completion follow-up** — when a task finishes but leaves a remainder, the leftover becomes a `_note_`.
- **Project estimate** — when a request trips the project gate (below), the agent writes a project note before starting.

Anti-spam guardrail: only auto-notes when a concrete `done_when` is writable. Personal/ambiguous items still ask first.

### Project executor

A `type: project` note is a durable, on-disk plan that lets the agent execute a large build **one step per turn**, surviving context limits and fresh sessions — the note *is* the state machine.

```
# Project <name>
type: project
status: in_progress | done
done_when: <final artifact / all steps [x]>
current_step: <n>
claimed_by: <run_id>          # set by Advancer/Improver on claim; blank when idle
claimed_at: <ISO timestamp>   # UTC ISO8601; stale after its window (live-session 120min / other 30min), then auto-released
steps:
  - [ ] 1. <step> -- done_when: <check>
  - [ ] 2. ...
resume_state: <paths / decisions the next turn needs to resume blind>
```

- **Estimate gate** — at request time the agent classifies scope: `>3 files OR >5 sequential tool-steps OR research-then-build OR user says "project/build"`. Trip it → write a project note; else execute normally in one turn. Before starting, the agent gauges **step heaviness** — which steps are big write+test units vs light edits — and splits heavy steps into their own turns in the note upfront.
- **Execution loop** — turn boundaries are decided by **step heaviness**. Before each step the agent gauges its execution load. A heavy step (writing + testing a whole tool/file, a multi-file change, research-then-build, a full write→run→debug→verify cycle) gets its **own turn**: do only that step, checkpoint, stop cleanly, report `step n/m done` — because build-and-test work fragments badly when split across turns. Light steps (a tiny edit, config, doc, one-liner) are batched several per turn. When a heavy step is reached with >2 steps remaining, a subagent may continue the rest in clean context. Resumption reads `resume_state` **only** — never chat history — so a blind fresh session continues from the note alone.
- **Driver (hybrid)** — the user can say "continue", or the **Project Advancer** sweeps `in_progress` projects and advances them unattended. It is triggered by a single **zero-LLM command cron** — the `Project Cron Gate` (every 15 min) — that runs a cheap bash check first and only spins up the Advancer's LLM turn when an `in_progress` note actually exists. On idle ticks the LLM is never invoked at all (truly zero idle cost, not just a short no-op turn). A safety gate keeps autonomy honest: destructive or external steps (delete data, git push, external messages, spend money, credentials) are **not** run unattended — the project pauses and tags the owner for approval (platform-appropriate mention). Read/build/local-file steps proceed on their own. Announcements are state-change only — no repeat spam; malformed/stalled alerts rate-limited to once per 24h.
- **Atomic task claiming** — before executing any step, a job writes `claimed_by` + `claimed_at` (UTC ISO8601) to the note. Any other job — a second Advancer, the Improver, the Deleter, or the daily review — sees a fresh claim and skips that note. Windows: **live-session claims 120 min** (a human working in an interactive session is never fought), **other-job claims 30 min**; stale or future-dated claims are auto-cleared. This is what stops two jobs (or a cron and a live session) from double-editing the same note.
- **Project Advancer drives progress** — the **Project Advancer** (every 15 min) advances `in_progress` project steps unattended. Context overflow is handled natively by OpenClaw's safeguard compaction.

- **Self-improving closer** — when a project reaches `status: done`, a **Project Improver** cron (`:20`/`:50`) reviews the finished work. It is domain-agnostic and behavior-preserving: it asks whether *this specific* project can be made cheaper/faster/clearer/more reliable and does only that — no prescribed checklist, and "already efficient, nothing to change" is a valid, common outcome. It validates the artifact still meets its `done_when`, then stamps `verified: true` and reports what it did. The same `Project Cron Gate` runs a zero-LLM bash check and only wakes the Improver's LLM turn when a done-but-unverified project exists — so it too costs nothing on idle ticks. External/destructive improvement steps (git push, sends, spend, credentials) stay gated for owner approval. A **Verified Note Deleter** (also gate-driven) then retires `verified` notes — snapshotting each first, promoting any `promote: pin` note — via a deterministic, non-blocking, claim-aware script.

Resolution: once `done_when` is met, the neuron's **Daily Note Review** cron (06:00 UTC, installed by this skill) deletes or promotes the note — covering *all* note types, including `task_bound` notes and projects finished out-of-band, plus `stale_after` garbage collection. It is claim-aware (skips freshly-claimed notes). See [`references/architecture.md`](references/architecture.md) for the full spec.

### Skill Promotion

`skill_extract.py` runs automatically from three sources and distills reusable procedures into skill candidates under `kb/memory_neuron/skill_candidates/<slug>/`. At **convergence ≥ 3**, a candidate is promoted to `skills/<name>/SKILL.md` and becomes available as a named skill.

- **Source 1 — completed projects**: fires when a project note flips `status: done`
- **Source 2 — L3 clusters**: fires after memory synthesis; procedural clusters (size ≥ 3, action-verb pattern) become candidates
- **Source 3 — PERSISTENT.md**: fires after L4 promotion; best-practice insights ("always X", "prefer X over Y") become low-confidence candidates

- **Review mode** (`--review <slug>`) — re-extracts from the source note and overwrites an existing skill; used when a skill is flagged as stale or broken.
- **Prune mode** (`--prune`) — deletes zero-convergence candidates older than 60 days.
- Safety scan flags any step containing destructive keywords (delete, rm, truncate, etc.) with an inline ⚠️ warning in the generated SKILL.md.

### Session deep recall

When `memory_search` returns thin or uncertain results for recent context, `session_search.py` searches the raw archived session transcripts (7-day window) for the exact exchange — sharper, more detailed recall than the memory summary alone. Used only when the memory summary is insufficient, not every turn.

---

## How do I know it's working?

```bash
# Run first graph build manually
python3 ~/.openclaw/workspace-myagent/procedures/memory_graph.py
# → Wrote kb/memory_neuron/l2_graph/memory_graph.json

# Run first synthesis
python3 ~/.openclaw/workspace-myagent/procedures/memory_synthesis.py
# → Wrote SYNTHESIS.md + insight_history.jsonl

# Check crons are registered
crontab -l | grep memory_graph
crontab -l | grep memory_synthesis
crontab -l | grep memory_promote
```

After the first full week, check `topics/PERSISTENT_AUTO.md` — promoted insights will appear there.

---

### Why multi-signal evaluation?

A single observation can be noise. Neuron evaluates each insight across multiple dimensions before promotion: how confident the synthesis is, how often it recurs across synthesis runs, whether it contradicts existing knowledge, and how it behaves across its full lifecycle — including decay, recovery, and exceptions.

All signals must align. One-off or low-confidence insights stay in synthesis — never promoted.

---

## Cron schedule

| Time | Script | What runs |
|------|--------|-----------|
| Every 15 min | `auto_session_reset.py` | (dinomem base) session archive + extraction |
| Daily 3:00 UTC | `memory_graph.py` | L2 — Relationship Discovery (graph rebuild) |
| Daily 3:30 UTC | `memory_synthesis.py` + `contradiction_check.py` | L3 — Pattern Synthesis + contradiction check |
| Daily 4:00 UTC | `docs_ingest.py` | Re-ingest new/changed docs |
| Every 15 min | **Project Cron Gate** (OpenClaw **command** cron) | Zero-LLM dispatcher: runs the three bash pre-gates and force-runs Advancer/Improver/Deleter **only** when there's real work — $0 LLM on idle ticks |
| gate-driven | Project Advancer (OpenClaw cron, self-schedule disabled) | Auto-advance `type: project` notes one step/run, unattended; only fires when the gate finds an `in_progress` note; state-change-only announcements |
| gate-driven | Project Improver (OpenClaw cron, self-schedule disabled) | Self-improving closer: review a completed-but-unverified project, make a behavior-preserving improvement (or confirm none needed), validate, stamp `verified`; only fires when the gate finds a done-unverified project; external steps gated |
| gate-driven | Verified Note Deleter (OpenClaw cron, self-schedule disabled) | Deterministic janitor: delete notes tagged `verified: true` (snapshot first, `promote: pin` support, non-blocking lock, claim-aware); only fires when the gate finds a `verified` note |
| Daily 6:00 UTC | Daily Note Review (OpenClaw cron) | General note janitor: retire `task_bound` notes whose `done_when` is met, projects finished out-of-band, and abandoned notes past `stale_after`; claim-aware |
| Weekly Sun 4:00 UTC | `memory_promote.py` + `generate_topic_index.py` | L4 — Knowledge Promotion + MEMORY.md enrichment; triggers `skill_extract.py` after each run |
| On project completion / L3 / L4 | `skill_extract.py` | Skill Promotion — extracts reusable procedures from completed projects, L3 clusters, and PERSISTENT.md best practices; promotes at convergence ≥ 3 |

---

## Compatibility

| Potential clash | What happens | How to avoid |
|----------------|-------------|-------------|
| dinomem base not installed | install.sh warns and continues | Install dinomem first |
| Existing `kb/memory_neuron/` | Preserved — not overwritten | Nothing, safe |
| Existing `kb/vector_db_docs/` | Neuron RAG will write to this path | Back up first if it belongs to another system |
| Existing `kb/data_store/` | Neuron structured-data will write `data.duckdb` + `catalog.json` here | Back up first if it belongs to another system |
| Existing `rag_long_docs` in AGENTS.md | Block appended — potential duplicate | Remove duplicate manually after install |
| Existing `MEMORY.md` with content | Will be **rewritten** by `generate_topic_index.py` on first weekly run | Raw `memory/*.md` files untouched — index rebuilt from them |

---

## Install options

| Flag | Default | Description |
|------|---------|-------------|
| `--workspace DIR` | `$OPENCLAW_WORKSPACE` or `~/.openclaw/workspace` | Path to agent workspace |
| `--agent-id ID` | Detected from workspace name | OpenClaw agent ID |
| `--no-cron` | — | Skip crontab registration |
| `--force` | — | Overwrite existing scripts |

---

## What gets installed

```
<workspace>/
├── procedures/
│   ├── memory_graph.py         # L2 — daily similarity graph
│   ├── memory_synthesis.py     # L3 — emergent insight synthesis
│   ├── memory_promote.py       # L4 — durable insight promotion
│   ├── contradiction_check.py  # L3 — contradiction detection
│   ├── generate_topic_index.py # L4 — topic index + MEMORY.md enrichment
│   ├── docs_ingest.py          # RAG — ingest long docs
│   ├── data_register.py        # Structured data — register csv/json/parquet into the DuckDB store + catalog
│   ├── skill_extract.py        # Skill Promotion — extract from completed projects, L3 clusters, PERSISTENT.md best practices
│   └── run_log.py              # Per-run audit trail — append-only JSONL log for all cron scripts
├── tools/
│   ├── docs_search.py          # RAG — semantic search over long docs (HyDE query expansion enabled by default)
│   ├── data_query.py           # Structured data — read-only exact SQL over the DuckDB store (SELECT-only)
│   └── cache_utils.py          # Shared cache utility
├── scripts/
│   ├── cron_gate.sh            # zero-LLM command-cron dispatcher: runs the checks, force-runs the 3 jobs only on real work
│   ├── check_projects.sh       # zero-LLM pre-gate for Project Advancer (any in_progress project?)
│   ├── check_improvable.sh     # zero-LLM pre-gate for Project Improver (any done-unverified project?)
│   ├── check_deletable.sh      # zero-LLM pre-gate for Verified Note Deleter (any verified:true note?)
│   └── resolve_done_notes.py   # deterministic verified-note deleter (snapshot + promote + claim-aware lock)
├── topics/
│   ├── INDEX.md                # Auto-generated topic tree (do not edit)
│   ├── PERSISTENT.md           # Manually curated persistent topics
│   └── PERSISTENT_AUTO.md      # Auto-promoted by memory_promote.py (do not edit)
├── memory/
│   └── _permanent.md           # Promoted permanent lessons (do not edit)
├── docs/                       # Long documents for RAG (you add files here)
└── kb/
    ├── memory_neuron/          # memory store — L2/L3/L4 synthesis artifacts
    │   ├── l2_graph/
    │   ├── l3_synthesis/
    │   └── l4_promoted/
    ├── vector_db_docs/         # long-docs store — RAG embeddings (separate from memory)
    └── data_store/             # structured-data store — data.duckdb + catalog.json (separate from memory)
```

**Separate stores, one router.** Each retrieval kind lives in its own store and the agent auto-detects which to use from the request shape — so **memory is never polluted** by rows or long documents: structured/tabular data → `kb/data_store/data.duckdb` (exact SQL, with optional per-prose-column semantic rank riding along), long documents → `kb/vector_db_docs/` (semantic), and memory → native memory files. Row/count/range/aggregate questions route to `data_query`, passage/"like this" to `docs_search`, and facts/preferences to memory.

---

## Troubleshooting

**`memory_graph.py` fails with "DB not found"**
```bash
ls ~/.openclaw/memory/<agent-id>.sqlite
# If missing: run dinomem base first
python3 <workspace>/procedures/auto_session_reset.py
```

**`memory_synthesis.py` fails with "graph not found"**
```bash
python3 <workspace>/procedures/memory_graph.py  # run L2 first
python3 <workspace>/procedures/memory_synthesis.py
```

**Nothing in `topics/PERSISTENT_AUTO.md` after a week**
```bash
cat <workspace>/kb/memory_neuron/l3_synthesis/insight_history.jsonl | wc -l
# If 0: run synthesis manually
python3 <workspace>/procedures/memory_synthesis.py
tail -50 <workspace>/logs/memory_promote.log
```

**`docs_search.py` returns no results**
```bash
python3 <workspace>/tools/docs_search.py --list
python3 <workspace>/procedures/docs_ingest.py
tail -50 <workspace>/logs/docs_ingest.log
```

**Cron not running**
```bash
crontab -l | grep memory_graph
bash dinomem-neuron/scripts/install.sh --workspace <workspace> --agent-id <id>
```

---

## FAQ

**How long until I see results?**
L2 graph: next morning. L3 synthesis: a few days (needs memory clusters to form). L4 promotion: 1–2 weeks (insights must pass multi-signal evaluation across synthesis runs).

**Does it modify my existing memory files?**
No. L2/L3/L4 write to `kb/memory_neuron/` only. `memory_promote.py` appends to `_permanent.md` and `PERSISTENT_AUTO.md`. `generate_topic_index.py` overwrites `MEMORY.md` and `topics/INDEX.md` — both are auto-generated, not your raw memory files.

**What happens if synthesis finds a contradiction?**
Blocking contradictions → insight blocked, queued in `contradiction_queue.jsonl`. Warnings → promoted but flagged. Both logged to `contradiction_log.jsonl`. Blocked insights are re-evaluated automatically when old insights decay. You can also trigger manually: `python3 procedures/contradiction_check.py --re-evaluate`.

**Can I use RAG without L2/L3/L4?**
Yes — RAG is fully independent. Just tell your agent — it handles ingestion and search automatically.

**How much extra disk space?**
Graph JSON: small. Synthesis files: minimal text. Docs vector DB: ~1–2MB per 1000 chunks.

**Does it work with any OpenClaw agent?**
Yes — fully generic, no agent-specific logic.

---

## Update

```bash
bash dinomem-neuron/scripts/update.sh --workspace ~/.openclaw/workspace-myagent
```

## Uninstall

```bash
bash dinomem-neuron/scripts/uninstall.sh --workspace ~/.openclaw/workspace-myagent
```

This removes neuron cron jobs only. dinomem base config is untouched.

Optional flags:
- `--purge` — also remove installed scripts
- `--purge-data` — delete `kb/memory_neuron/`, `topics/`, `kb/vector_db_docs/` (memory is preserved)
- `--purge-memory` — ⚠️ permanently delete `memory/_permanent.md` (requires typing `wipe memory` to confirm)

---

## License

Commercial — see [LICENSE](LICENSE)

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
