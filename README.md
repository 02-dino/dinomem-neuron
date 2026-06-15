# 🧠 dinomem-neuron — Dino Agent Memory (Neuron Layer)

> dinomem remembers what you tell it. dinomem-neuron figures out things you never said.

dinomem gives your agent memory. dinomem-neuron gives it the ability to learn from that memory.

Every night, it builds a graph of how your memories relate, clusters them, and runs synthesis — an LLM pass that finds patterns across everything you've ever told your agent. Insights are rigorously evaluated before being promoted to permanent knowledge. Your agent's behavior changes over time — not because you updated a config, but because it learned.

The result: an agent that connects a decision you made in January to a pattern from November. That notices you've been contradicting yourself. That builds a structured knowledge base from your sessions — without you writing a single line of it.

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

**Requires [dinomem](https://github.com/02-dino/dinomem) installed first.**

---

## What it adds

| | Feature | What it does |
|-|---------|-------------|
| 🔗 | **Relationship Discovery** (L2) | Finds which memories are related — even across different conversations |
| 💡 | **Pattern Synthesis** (L3) | Clusters related memories → LLM synthesizes emergent insights you never explicitly stated |
| ⚠️ | **Contradiction detection** (L3) | Flags when new insights conflict with existing ones |
| 📌 | **Knowledge Promotion** (L4) | Insights that pass multi-signal evaluation (confidence, reinforcement/recurrence, contradiction, insight lifecycle) → written into always-injected context → behavior change |
| 📚 | **RAG for long docs** | Semantic search over contracts, books, legal text — stored separately, never pollutes memory |
| 📅 | **Calendar integration** | Links `_note_` reminders to Google Calendar — auto-deleted when the event passes |

---

## Prerequisites

- [ ] [dinomem](https://github.com/02-dino/dinomem) installed and working
- [ ] TEI embedding server running (`curl http://localhost:8080/health` → `{"status":"ok"}`)
- [ ] At least a few sessions already extracted (`memory/*.md` files exist)
- [ ] Python 3.8+

> **Verify dinomem is working first.** Run `python3 <workspace>/procedures/auto_session_reset.py` and confirm `memory/*.md` files exist before installing neuron.

---

## Install

```bash
# Step 1: dinomem must be installed first
git clone https://github.com/02-dino/dinomem
bash dinomem/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent

# Step 2: install dinomem-neuron (private repo — access granted after onboarding)
git clone https://github.com/02-dino/dinomem-neuron
bash dinomem-neuron/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent

# Step 3: restart OpenClaw
openclaw gateway restart
```

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

## Using dinomem-neuron

### RAG for long documents

Store and search long documents — contracts, books, legal text, manuals — without polluting memory.

```bash
# Add a document
cp my-contract.md ~/.openclaw/workspace-myagent/docs/

# Ingest it (or wait for daily cron at 4:00 UTC)
python3 ~/.openclaw/workspace-myagent/procedures/docs_ingest.py --file docs/my-contract.md

# Search it
python3 ~/.openclaw/workspace-myagent/tools/docs_search.py "force majeure clause" --k 5

# List all indexed documents
python3 ~/.openclaw/workspace-myagent/tools/docs_search.py --list
```

Documents are chunked by heading structure — one section = one chunk. Legal clauses and book chapters stay intact, not split mid-sentence.

The agent uses `docs_search.py` automatically when you ask about content from an indexed document.

### Calendar integration

Link `_note_` reminders to Google Calendar. When the event date passes, the note is automatically deleted.

**One-time setup:**
```bash
# 1. Create OAuth2 credentials at https://console.cloud.google.com/
#    APIs & Services → Credentials → Create → OAuth client ID → Desktop app
#    Download as client_secret.json → place in <workspace>/

# 2. Authorize
python3 ~/.openclaw/workspace-myagent/scripts/calendar_oauth_setup.py
```

**Usage:**
```bash
# Create a calendar event linked to a note
python3 ~/.openclaw/workspace-myagent/tools/calendar_tool.py create \
  "Doctor appointment" --date 2026-06-25 --time 14:00 --note _note_doctor.md

# With reminder (popup 30 min before) and attendee
python3 ~/.openclaw/workspace-myagent/tools/calendar_tool.py create \
  "Team meeting" --date 2026-06-25 --time 10:00 \
  --reminder popup:30 --reminder email:60 \
  --attendee colleague@example.com

# List upcoming events
python3 ~/.openclaw/workspace-myagent/tools/calendar_tool.py list --upcoming 7
```

> **Note:** Sound and vibration are device-level settings — not controllable via the Google Calendar API. Those are set on your phone/browser.

Or just tell your agent:
> "Remember to call the dentist before Friday"

The agent detects the date, saves `_note_dentist.md`, and creates the calendar event automatically. When Friday passes, `calendar_sync.py` deletes the note.

---

## How it all works

```
[dinomem base — every 15 min]
Session → Archive → extract_memory.py → memory/*.md + MEMORY.md + vector DB
        │
        │  daily 3:00 UTC
        ▼
[L2: Relationship Discovery — memory_graph.py]
  Reads embeddings → builds similarity graph
  Output: kb/memory_neuron/l2_graph/memory_graph.json
        │
        │  daily 3:30 UTC
        ▼
[L3: Pattern Synthesis — memory_synthesis.py]
  Clusters connected nodes → LLM synthesizes emergent insights
  Output: kb/memory_neuron/l3_synthesis/SYNTHESIS.md
        │
        ▼
[L3: Contradiction detection — contradiction_check.py]
  Checks new insights against existing → flags contradictions
        │
        │  weekly Sunday 4:00 UTC
        ▼
[L4: Knowledge Promotion — memory_promote.py]
  Durable insights → topics/PERSISTENT_AUTO.md + memory/_permanent.md
        │
        ▼
[L4: generate_topic_index.py]
  Builds topics/INDEX.md → writes MEMORY.md (enriched, structured)
        │
        ▼
[Root context injection — every turn]
  topics/PERSISTENT_AUTO.md + memory/_permanent.md injected into agent context
        │
        ▼
[Behavior change]
  Promoted insights present on every turn — no recall needed
```

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
│   ├── docs_search.py          # RAG — semantic search over long docs
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
Yes — both are fully independent. RAG: just run `docs_ingest.py` + `docs_search.py`. Calendar: just run `calendar_oauth_setup.py` once, then use `calendar_tool.py`.

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

Made with 🦖 by [@02-dino](https://github.com/02-dino) | [komunitech.com](https://komunitech.com)
