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
| 📅 | **Calendar integration** | Links `_note_` reminders to Google Calendar — auto-deleted when the event passes |
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

> **Verify dinomem is working first.** Run `python3 <workspace>/procedures/auto_session_reset.py` and confirm `memory/*.md` files exist before installing neuron.

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

neuron's pipeline is the **reasoning tier**. `memory_synthesis`, `contradiction_check`,
and `memory_promote` make validity/judgment calls (emergent-insight synthesis,
contradiction logic, permanent-promotion verdicts) — so they always run on your
OpenClaw **default** model and request a thinking budget
(`DINOMEM_REASONING_THINKING`, default `high`). They ignore `DINOMEM_CHEAP_MODEL`
on purpose: routing these to a cheap model risks cementing stale insights or
deleting good memory.

`generate_topic_index` is mechanical (tree assembly) and stays on the default
model without a thinking boost.

The cheap-model lever (`DINOMEM_CHEAP_MODEL`) only affects dinomem base's
no-reasoning bulk scripts (`extract_memory`, `memory_review`). See
[dinomem → Model selection](https://github.com/02-dino/dinomem#model-selection).
These flags live in `extract_memory.call_llm`, which neuron imports — so install
dinomem base **v1.2.0+** for the reasoning routing to take effect.

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

## Calendar setup

Calendar integration requires a one-time OAuth setup. Everything else — L2/L3/L4, RAG, contradiction detection — runs automatically after install.

```bash
# 1. Create OAuth2 credentials at https://console.cloud.google.com/
#    APIs & Services → Credentials → Create → OAuth client ID → Desktop app
#    Download as client_secret.json → place in <workspace>/

# 2. Authorize
python3 ~/.openclaw/workspace-myagent/scripts/calendar_oauth_setup.py
```

Once set up, just tell your agent:
> "Remember to call the dentist before Friday"

The agent detects the date, saves `_note_dentist.md`, and creates the calendar event automatically. When Friday passes, `calendar_sync.py` deletes the note.

> Sound and vibration are device-level settings — not controllable via the Google Calendar API.

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
| Daily 7:00 UTC | `calendar_sync.py` | Auto-delete `_note_` files linked to past events |
| Weekly Sun 4:00 UTC | `memory_promote.py` + `generate_topic_index.py` | L4 — Knowledge Promotion + MEMORY.md enrichment |

---

## Compatibility

| Potential clash | What happens | How to avoid |
|----------------|-------------|-------------|
| dinomem base not installed | install.sh warns and continues | Install dinomem first |
| Existing `kb/memory_neuron/` | Preserved — not overwritten | Nothing, safe |
| Existing `kb/vector_db_docs/` | Neuron RAG will write to this path | Back up first if it belongs to another system |
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
│   └── calendar_sync.py        # Calendar — auto-resolve past _note_ files
├── tools/
│   ├── docs_search.py          # RAG — semantic search over long docs (HyDE query expansion enabled by default)
│   ├── cache_utils.py          # Shared cache utility
│   └── calendar_tool.py        # Calendar — create/list/delete events
├── scripts/
│   └── calendar_oauth_setup.py # Calendar — one-time OAuth setup
├── topics/
│   ├── INDEX.md                # Auto-generated topic tree (do not edit)
│   ├── PERSISTENT.md           # Manually curated persistent topics
│   └── PERSISTENT_AUTO.md      # Auto-promoted by memory_promote.py (do not edit)
├── memory/
│   └── _permanent.md           # Promoted permanent lessons (do not edit)
├── docs/                       # Long documents for RAG (you add files here)
└── kb/
    └── memory_neuron/
        ├── l2_graph/
        ├── l3_synthesis/
        └── l4_promoted/
```

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

**Calendar events not being created**
```bash
# Check OAuth token exists
grep GOOGLE_CALENDAR_TOKEN <workspace>/.env
# If missing: re-run OAuth setup
python3 <workspace>/scripts/calendar_oauth_setup.py
```

**`_note_` files not being deleted after event passes**
```bash
tail -50 <workspace>/logs/calendar_sync.log
# Run manually
python3 <workspace>/procedures/calendar_sync.py --dry-run
```

**Cron not running**
```bash
crontab -l | grep memory_graph
bash dinomem-neuron/scripts/install.sh --workspace <workspace> --agent-id <id>
```

---

## FAQ

**How long until I see results?**
L2 graph: next morning. L3 synthesis: a few days (needs memory clusters to form). L4 promotion: 1–2 weeks (insights must pass multi-signal evaluation across synthesis runs). Calendar: immediate after OAuth setup.

**Does it modify my existing memory files?**
No. L2/L3/L4 write to `kb/memory_neuron/` only. `memory_promote.py` appends to `_permanent.md` and `PERSISTENT_AUTO.md`. `generate_topic_index.py` overwrites `MEMORY.md` and `topics/INDEX.md` — both are auto-generated, not your raw memory files.

**What happens if synthesis finds a contradiction?**
Blocking contradictions → insight blocked, queued in `contradiction_queue.jsonl`. Warnings → promoted but flagged. Both logged to `contradiction_log.jsonl`. Blocked insights are re-evaluated automatically when old insights decay. You can also trigger manually: `python3 procedures/contradiction_check.py --re-evaluate`.

**Can I use RAG or calendar without L2/L3/L4?**
Yes — both are fully independent. Just tell your agent — it handles ingestion and search automatically. Calendar requires one-time OAuth setup via `calendar_oauth_setup.py`.

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

Commercial — see LICENSE

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
