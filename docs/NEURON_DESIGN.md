# dinomem-neuron — Architecture

> This document explains how neuron works internally.
> It is published to support independent evaluation — you do not need to read the source code to understand the system.

---

## Overview

dinomem extracts memories from sessions and makes them searchable.
Neuron takes those memories further — it finds patterns across them, evaluates those patterns over time, and promotes durable insights into always-present behavioral knowledge.

The result is an agent that changes based on what it has learned — not because you wrote a rule, but because the system observed a pattern and validated it.

---

## The Full Pipeline

```
[dinomem base — every 15 min]
Session → Archive → extract memory → memory/*.md + MEMORY.md + vector DB

        │
        │  daily
        ▼

[L2 — Relationship Discovery]
  Reads embeddings from the local vector DB
  Computes cosine similarity between all memory nodes
  Builds a graph: nodes = memory entries, edges = similarity above threshold
  Edge weight = composite neural_score: cosine sim × rank strength × recurrence weight × L3 confidence boost
  Tracks edge history across rebuilds — edges that recur across multiple memory states are stronger (Hebbian principle)
  Feedback-loop firewall: skips L4-promoted chunks so synthesis never reads its own conclusions back

        │
        │  daily
        ▼

[L3 — Pattern Synthesis]
  Reads L2 graph clusters (cross-file, medium-similarity edges only — near-duplicates excluded)
  For each cluster: LLM synthesizes an emergent insight (skeptical prompt — returns NONE if no real pattern)
  3-stage gate before an insight is promotion-eligible:
    Stage 1 — LLM audit: confidence must exceed threshold; uncertain insights penalized
    Stage 2 — Convergence: same pattern must surface from ≥2 independent clusters
    Stage 3 — Recurrence: underlying edges must have persisted across ≥3 graph rebuilds
  Deduplicates against existing insights
  Tracks each insight across synthesis runs (recurrence = reinforcement signal)

        │
        ▼

[L3 — Contradiction Detection]
  Reads new insights from synthesis
  LLM checks each against existing promoted knowledge for conflicts
  Blocking contradictions → queued, not promoted
  Warnings → promoted but flagged

        │
        │  weekly
        ▼

[L4 — Knowledge Promotion]
  Reads insights that have survived N synthesis cycles
  Applies multi-signal evaluation:
    - Confidence score from synthesis
    - Reinforcement strength (recurrence across runs)
    - Contradiction status (blocked if unresolved)
    - Insight lifecycle (decay, recovery, exception handling)
  Category-aware TTL: preferences decay slower than predictions; lessons slower than uncertain insights
  Reinforcement multiplier: insights reinforced 10+ times get 2× TTL extension
  LLM review rescue: every 60 days, LLM judges if a decaying insight is still valid — resets clock if yes, deletes if no
  Graduation rate-limited: max 2 promotions per run, hard cap of 15 permanent rules (keeps MEMORY.md lean)
  Insights that pass all signals → promoted to permanent knowledge

        │
        ▼

[L4 — Index Generation]
  Reads topics/PERSISTENT.md (manual entries) + topics/PERSISTENT_AUTO.md
  Builds topics/INDEX.md (structured topic tree)
  Writes MEMORY.md — enriched with promoted insights at the top
  MEMORY.md now serves two roles:
    1. Search navigation aid (same as dinomem base)
    2. Always-present behavioral layer (promoted insights injected every turn)

        │
        ▼

[Root context injection — every turn]
  Promoted insights present in every agent turn
  No recall needed — behavior change is baseline, not on-demand
```

---

## What "multi-signal evaluation" means

A single observation can be noise. Neuron does not promote based on one signal alone.

Before an insight is promoted to permanent knowledge, it must pass four checks:

| Signal | What it measures | Threshold |
|--------|-----------------|-----------|
| **Confidence** | How certain the synthesis LLM is about the insight | Must exceed minimum threshold |
| **Reinforcement** | How many times the insight has recurred across separate synthesis runs | Must appear in N+ independent runs |
| **Contradiction** | Whether the insight conflicts with existing promoted knowledge | Must be unblocked |
| **Lifecycle** | Whether the insight has decayed, recovered, or accumulated exceptions | Must be in a stable state |

All four must align. An insight that scores high on confidence but appears only once stays in synthesis — it is never promoted.

---

## Insight lifecycle

Insights are not static. They move through states:

```
Candidate → Provisional → Trusted → Promoted
                ↓               ↓
             Decayed         Blocked (contradiction)
                ↓
           Re-evaluated
```

- **Candidate** — synthesized but not yet reinforced
- **Provisional** — appeared in multiple runs, passed contradiction check, not yet at promotion threshold
- **Trusted** — passed all signals, queued for next weekly promotion run
- **Promoted** — written into permanent knowledge, injected every turn
- **Decayed** — reinforcement dropped below threshold (e.g. contradicting evidence appeared)
- **Blocked** — contradiction detected, held in queue until old insight decays or is resolved

---

## Contradiction handling

When synthesis produces an insight that conflicts with existing knowledge:

1. Contradiction detection runs an LLM comparison against existing promoted knowledge
2. **Blocking contradiction** → insight is queued, not promoted
3. **Warning-level contradiction** → insight is promoted but flagged
4. Blocked insights are re-evaluated automatically when the conflicting promoted insight decays

---

## How behavior change works

Ordinary memories (from dinomem base) are recalled on demand — the agent searches for them when relevant.

Promoted insights work differently. They are written into `MEMORY.md` at the top, above the search navigation index. OpenClaw injects `MEMORY.md` into the agent's context on every turn.

This means promoted insights are **always present** — they influence every response without being explicitly recalled. The agent does not need to search for them. They are part of its baseline context.

---

## Prerequisites

- dinomem base installed and working (`memory/*.md` files exist)
- TEI embedding server running (`curl http://localhost:8080/health`)
- At least a few sessions already extracted
- Python 3.8+

---

*For access to the implementation: [@dinotlgrm](https://t.me/dinotlgrm)*
