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
    └── memory_neuron/
        ├── l2_graph/
        │   └── memory_graph.json     # Graph nodes + edges
        ├── l3_synthesis/
        │   ├── SYNTHESIS.md              # Living snapshot of current insights
        │   ├── current_insights.json     # Structured handoff → L4 promoter
        │   ├── insight_history.jsonl     # Append-only insight history
        │   ├── l3_edge_confidence.json   # Edge confidence scores
        │   ├── contradiction_log.jsonl   # Detected contradictions
        │   └── contradiction_queue.jsonl # Unresolved contradictions
        └── l4_promoted/
            ├── promotion_state.json      # Provisional + trusted store
            ├── promotion_log.jsonl       # Append-only audit trail
            ├── TRUSTED.md                # Human-readable trusted set mirror
            └── .last_consumed.json       # Dedup guard
```

## Cron Schedule

| Time | Script | Layer |
|------|--------|-------|
| Daily 3:00 UTC | `memory_graph.py` | L2 |
| Daily 3:30 UTC | `memory_synthesis.py` + `contradiction_check.py` | L3 |
| Weekly Sun 4:00 UTC | `memory_promote.py` + `generate_topic_index.py` | L4 |

## Prerequisites

- dinomem base installed (TEI running, `<agent>.sqlite` exists with embeddings)
- Python 3.8+
- OpenClaw memory DB populated (requires at least a few sessions extracted)
