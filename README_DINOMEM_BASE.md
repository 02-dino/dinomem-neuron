# dinomem — Base README

**This is the README for the *dinomem base* package** (the memory foundation that dinomem-neuron is built on top of).

> dinomem-neuron rides on the dinomem base. The page below is a quick summary of the base — the **full, always-current base README** lives in the public dinomem repo and updates automatically, so this never goes stale.

📖 **Full dinomem base README (live, always current) →** [github.com/02-dino/dinomem](https://github.com/02-dino/dinomem#readme)
🧬 **What neuron adds on top of base →** [dinomem-neuron README](./README.md)

---

## What the base gives your agent

dinomem gives any OpenClaw agent **persistent memory** — without you writing a single config line.

| | Feature | What it does |
|-|---------|-------------|
| 🗄️ | **Session archiving** | Conversations are archived and distilled into `memory/*.md` — durable notes, not raw transcripts |
| 🗺️ | **Navigation index** | `MEMORY.md` is a searchable map of what exists in memory, injected every turn — the agent knows what it can recall |
| 🔍 | **On-demand recall** | The agent searches its own memory when relevant (`memory_search` / `memory_get`) instead of forgetting between sessions |
| 🧠 | **Self-curating memory** | An LLM reviews and dedupes memory daily — distills what matters, merges duplicates, retires stale notes. Memory gets sharper over time instead of bloating with noise. |
| 🧭 | **Self-configuring** | Teach it a rule and it routes the behavior to where its trigger lives — a schedule → cron, an event → hook, an on-demand procedure → skill — instead of you hand-writing config. |
| 🧬 | **Git-versioned memory** | An **isolated** `.dinomem-snap.git` store (on by default) snapshots memory changes — never touches your own repo. Full history, safe rollback. |
| ⚙️ | **Zero-config install** | Installer sets sane defaults, patches only what's needed, and raises (never lowers) safety-critical timeouts |

*This table is a stable summary. For the complete, current feature set, setup, config, and troubleshooting, always use the live README linked above.*

---

## Why a link instead of a copy

The base repo is **public** and updates independently. A live link means this page can never drift out of sync — you always see the real, current base. A mirrored copy would go stale the moment base changes. Truth stays at the source.

**Full base README:** https://github.com/02-dino/dinomem#readme

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
