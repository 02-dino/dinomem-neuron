# 🧠 dinomem-neuron — Before / After

> What actually changes when your agent goes from *remembering* to *learning*. No feature list — just the lived experience, before and after.

> **This is the private learning layer.** It sits on top of [dinomem base](https://github.com/02-dino/dinomem) (the public, self-curating memory system). Base must be installed first. If you only want an agent that stops re-explaining itself, base is enough — see [base's Before / After](https://github.com/02-dino/dinomem/blob/main/BEFORE_AFTER.md). Neuron is for when you want it to *connect the dots you never connected out loud.*

---

## The core fix: it figures out what you *meant*, not just what you said

### ❌ BEFORE — base memory alone

Base remembers facts perfectly. But it holds them **separately**, and only surfaces them when you ask the right question:

```
January:  "User prefers concentrated positions."
February: "User only holds 5 stocks max."
March:    "User dislikes broad diversification."
```

Three true facts. Three separate files. The agent never says the *pattern* back to you. It waits to be queried — and if you don't ask precisely, the connection is never made.

```
   Jan ●        Feb ●        Mar ●
   (three dots, never connected)
```

### ✅ AFTER — dinomem-neuron

Once a day, neuron graphs how your memories relate, clusters them, and runs an **LLM synthesis pass** that finds the emergent truth:

```
L3 synthesis output:
  insight:        "User consistently prefers concentrated investing."
  convergence:    3 independent clusters
  reinforcement:  recurred across multiple synthesis runs
  contradictions: none
  lifecycle:      stable → trusted
```

Then it's **rigorously gated** before it's trusted — a single observation is never enough:

- must **recur** across multiple synthesis rebuilds (reinforcement)
- must surface from **independent clusters** (convergence)
- must **pass a contradiction check** against existing knowledge
- must survive **multi-signal evaluation** (confidence + lifecycle + TTL)

Survive all that → **promoted into always-injected context** (`MEMORY.md`).

```
   Jan ●━━━━━━━ Feb ●━━━━━━━ Mar ●
        └──────→ "prefers concentrated investing" ──→ injected EVERY turn
```

Now **every** investing conversation reflects it — no recall, no prompting, no config line. It became baseline behavior.

---

## What that unlocks, before vs after

| | ❌ Base alone | ✅ With neuron |
|-|---------------|----------------|
| **Patterns** | facts stay separate; you connect them manually | synthesizes the emergent insight and injects it every turn |
| **Contradictions** | old + new belief both sit in memory | flags the conflict; holds the new one back until resolved |
| **Recall** | you ask, it searches | stable patterns are *already present* — no recall needed |
| **Big tasks** | one long session, easy to lose the thread | becomes a **project**: step-by-step plan it executes across sessions, advancing on its own, pausing for approval on anything risky |
| **Your documents** | not searchable | RAG over contracts, books, PDFs, scanned pages, images — OCR'd by the agent's own vision model (no GPU) |
| **Your spreadsheets** | embeddings guess | exact SQL: *how many, which ones, under $X, grouped by* — the precise answers embeddings can't give |
| **Follow-ups** | you remember to remind it | it writes its own `_note_` files from its own commitments |
| **Cleanup** | daily janitor already retires simple resolved/stale notes | *extends* it to **projects**: retires finished project notes, promotes the good ones, and a self-improving closer reviews finished work before retiring it |

---

## The whole thing, one frame

> ❌ **Base alone:** the agent remembers everything you said — but never learns what it means.
>
> ✅ **With neuron:** it connects January to November, promotes stable patterns into baseline behavior, notices when you contradict yourself, and executes big tasks on its own — all injected every turn.

**One line:** *base remembers what you said; neuron figures out what you meant — and acts on it every turn.*

---

## It compounds with model quality — for free

Synthesis is an **LLM finding patterns across your memory** — not weights you fine-tune. Every time the base model gets smarter, the patterns get deeper and promotion gets more accurate. No retraining. No rewrite.

Fine-tuning approaches have to redo their work each model generation. Neuron just inherits the gain.

*And it never fine-tunes your weights — it learns by observing experiences → identifying patterns → evaluating them → promoting the stable ones → changing behavior. Fully reversible, fully inspectable.*

---

Full architecture and a real end-to-end synthesis example: see the main README →
[How it all works](README.md#how-it-all-works) · [Example](README.md#example)

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
