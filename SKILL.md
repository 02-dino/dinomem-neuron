---
name: dinomem-neuron
description: "Advanced memory layer for OpenClaw — adds memory graph (L2), synthesis (L3), promotion (L4), contradiction detection, RAG for long docs, and calendar integration. Requires dinomem."
author: dino
license: Commercial
---

# dinomem-neuron

Extends dinomem with emergent pattern detection, graph linking, synthesis, and promotion pipelines. Private repo — contact [@dinotlgrm](https://t.me/dinotlgrm) for access.

## Install

```bash
# dinomem must be installed first
git clone https://github.com/02-dino/dinomem-neuron
bash dinomem-neuron/scripts/install.sh --workspace <workspace> --agent-id <id>
openclaw gateway restart
```

## Verify

```bash
python3 <workspace>/procedures/memory_graph.py
crontab -l | grep memory_graph
```
