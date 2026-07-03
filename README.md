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
| 🤖 | **Automatic notes** | The agent writes `_note_` files from its own commitments ("next step", "TODO") and task follow-ups — not just when you ask |
| 🏗️ | **Project executor** | Large builds become `type: project` notes: the agent estimates token cost upfront and splits the work if it won't fit the remaining budget, then executes one step per turn (with a reactive context checkpoint as a safety net) so nothing is lost across sessions. A Project Advancer cron auto-advances unfinished projects unattended and announces progress, pausing for approval on anything destructive. |
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
These flags live in `extract_memory.call_llm`, which neuron imports — so dinomem
base **v1.2.0+ is required**. neuron's installer enforces this floor. The catch:
most people install base **first**, sometimes an older one. On base below 1.2.0
the `reasoning=True` flag is *accepted but silently ignored* (no thinking budget,
no error) — reasoning quality quietly degrades. If neuron's installer warns that
your base is too old, run `dinomem/scripts/update.sh` before using neuron.

---

## Beyond the pipeline

The L2/L3/L4 flow above is neuron's core: turning raw memory into validated, behavior-changing knowledge. Riding on that same base are four capabilities that aren't part of the synthesis engine but extend what the agent can do day to day.

### RAG for long documents

Contracts, books, legal text, manuals — anything too long to live in memory. `docs_ingest.py` chunks and embeds them into a separate vector store (`kb/vector_db_docs/`), kept apart from memory so long docs never pollute the synthesis pipeline. The agent retrieves passages via `docs_search.py` (HyDE query expansion on by default). Re-ingested daily so new/changed files stay searchable.

**Seamless ingest.** You don't drop files in a folder or convert anything. Share a document in chat — PDF, Word (`.docx`), `.rtf`, `.odt`, HTML, EPUB, or plain text/markdown — and the agent detects the intent, extracts it to markdown, and ingests it automatically, ready for `docs_search`. This works on any chat channel that downloads attachments to OpenClaw's shared media store (Telegram, WhatsApp, Discord, Slack, and others), not just one. PDF extraction uses `pdftotext` and Office/HTML uses `pandoc`; the installer auto-installs both when possible, and if either is unavailable, text/markdown ingest still works while the affected formats degrade gracefully with a clear message.

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
claimed_by: <run_id>          # set by Project Advancer on claim; blank when idle
claimed_at: <ISO timestamp>   # stale if >10min ago (auto-released)
steps:
  - [ ] 1. <step> -- done_when: <check>
  - [ ] 2. ...
resume_state: <paths / decisions the next turn needs to resume blind>
```

- **Estimate gate** — at request time the agent classifies scope: `>3 files OR >5 sequential tool-steps OR research-then-build OR user says "project/build"`. Trip it → write a project note; else execute normally in one turn. Before starting, the agent estimates token cost `(files × 500) + (steps × 1000) + current_context_tokens` — if over 40% of remaining budget, the task is split into sub-tasks upfront.
- **Execution loop** — do the current step, then read context usage via `session_status`. Below 60% used: mark `[x]`, rewrite `resume_state`, advance, continue. At/over 60% used with >2 remaining steps: spawn a subagent to continue (keeps main session light). At/over 60% used otherwise: checkpoint and stop cleanly, reporting `step n/m done`. This 60%-used reactive checkpoint is the *fallback*; the primary gate is the proactive upfront estimate above (the two are consistent — ~40% headroom either way, one expressed as budget-remaining, one as context-used). Resumption reads `resume_state` **only** — never chat history — so a blind fresh session continues from the note alone.
- **Driver (hybrid)** — the user can say "continue", or the **Project Advancer** cron (every 15 min) sweeps `in_progress` projects and advances them unattended. Pre-check: a zero-LLM bash script runs first — if no `in_progress` notes exist, the LLM is never invoked (zero idle cost). A safety gate keeps autonomy honest: destructive or external steps (delete data, git push, external messages, spend money, credentials) are **not** run unattended — the project pauses and tags the owner for approval (platform-appropriate mention). Read/build/local-file steps proceed on their own. Announcements are state-change only — no repeat spam; malformed/stalled alerts rate-limited to once per 24h.
- **Atomic task claiming** — before executing any step, the Advancer writes `claimed_by` + `claimed_at` to the note. If two Advancer instances overlap, the second sees the claim and skips. Claims older than 10 minutes are stale and auto-released.
- **Two distinct crons, don't conflate them** — the **Project Advancer** (every 15 min) drives *progress*: it advances `in_progress` project steps. The **context watchdog** (`context_watchdog.py`, every 5 min, zero LLM) is a separate *overflow rescue*: it checks all active sessions via REST, and if context usage exceeds 60% it sends a checkpoint wake event to that session — catching overflow the upfront estimate missed. One moves projects forward; the other prevents any session from blowing its context mid-step.

Resolution is unchanged: once `done_when` is met, the base Daily Note Review cron deletes or promotes the note. See [`references/architecture.md`](references/architecture.md) for the full spec.

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
| Every 15 min | Project Advancer (OpenClaw cron) | Auto-advance `type: project` notes one step/run, unattended; zero LLM when idle (bash pre-check); state-change-only announcements |
| Weekly Sun 4:00 UTC | `memory_promote.py` + `generate_topic_index.py` | L4 — Knowledge Promotion + MEMORY.md enrichment; triggers `skill_extract.py` after each run |
| On project completion / L3 / L4 | `skill_extract.py` | Skill Promotion — extracts reusable procedures from completed projects, L3 clusters, and PERSISTENT.md best practices; promotes at convergence ≥ 3 |
| Every 5 min | `context_watchdog.py` | Context budget watchdog — REST-only, no LLM; sends checkpoint wake to sessions >60% context |

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
│   ├── skill_extract.py        # Skill Promotion — extract from completed projects, L3 clusters, PERSISTENT.md best practices
│   └── run_log.py              # Per-run audit trail — append-only JSONL log for all cron scripts
├── tools/
│   ├── docs_search.py          # RAG — semantic search over long docs (HyDE query expansion enabled by default)
│   └── cache_utils.py          # Shared cache utility
├── scripts/
│   └── context_watchdog.py     # Context budget watchdog — REST-only, no LLM, system cron every 5min
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
