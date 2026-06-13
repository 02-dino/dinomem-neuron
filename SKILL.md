---
name: dinomem-neuron
description: Advanced memory layer for OpenClaw agents. Adds L2 graph linking, L3 synthesis, L4 promotion, contradiction detection, and topic indexing on top of dinomem base. Requires dinomem installed first.
version: 0.1.0
lastUpdated: 2026-06-13
metadata:
  openclaw:
    emoji: "🧠"
    requires:
      plugins: ["dinomem"]
      bins: ["python3"]
triggers:
  - "install neuron memory"
  - "set up dinomem-neuron"
  - "advanced memory layer"
  - "memory graph synthesis"
  - "topic index memory"
author: dino
license: Commercial
---

# 🧠 dinomem-neuron — Dino Agent Memory (Neuron Layer)

Advanced memory layer that extends dinomem with emergent pattern detection, graph linking, synthesis, and promotion pipelines.

**Requires [dinomem](https://github.com/02-dino/dinomem) installed first.**

## Layers

| Layer | Script | What it does |
|-------|--------|-------------|
| L2 | `memory_graph.py` | Links related memory nodes via embedding similarity |
| L3 | `memory_synthesis.py` | Fires neuron clusters → emergent insights |
| L4 | `memory_promote.py` | Promotes durable insights → `topics/PERSISTENT_AUTO.md` |
| — | `contradiction_check.py` | Detects contradictions across memory entries |
| — | `generate_topic_index.py` | Builds `topics/INDEX.md` + folds into `MEMORY.md` |

## Install

```bash
# Install dinomem first
git clone https://github.com/02-dino/dinomem
bash dinomem/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent

# Then install dinomem-neuron
git clone https://github.com/02-dino/dinomem-neuron
bash dinomem-neuron/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent
```

## Procedure

1. Ensure dinomem is installed and TEI is running
2. Run install: `bash scripts/install.sh --workspace <path> --agent-id <id>`
3. Restart OpenClaw: `openclaw gateway restart`
4. Run first graph build: `python3 <workspace>/procedures/memory_graph.py`

## References
- `references/architecture.md` — neuron layer pipeline details
