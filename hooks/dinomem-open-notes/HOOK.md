---
name: dinomem-open-notes
description: "On every agent bootstrap, deterministically inject a blocking manifest of OPEN dinomem notes (status in_progress|pending) so the model can't miss unfinished work. NEURON SUPERSET: also reminds the model to run session_search (recent-conversation recall) when the opener names something no open note covers — replaces the old 'glob before answering' + 'session_start_recall' AGENTS.md rules with a mechanism instead of a plea."
metadata:
  { "openclaw": { "emoji": "📌", "events": ["agent:bootstrap"], "requires": { "bins": [] } } }
---
# dinomem-open-notes (neuron)

Zero-token-waste, deterministic "resume your open work" injection at bootstrap.
This is the **neuron superset** of the base `dinomem-open-notes` hook — same
mechanism, same hook name (it **overwrites** base's copy), plus a neuron-only
cold-start recall line.

## Why

dinomem's project/task notes (`memory/_note_*.md`) carry a `status:` and a
`resume_state:`. The old contract was an AGENTS.md instruction: *"on the first
turn, glob `memory/_note_*.md` and read the open ones before answering."* That is
a **plea to the model** — the dinomem docs themselves flagged skipping this glob
as the single most common silent failure (open note one `ls` away that the model
never read, so it re-nags or restarts finished work).

This hook removes the glob-skip failure class entirely: it runs the glob itself,
**every bootstrap**, and injects a short **blocking manifest** of only the OPEN
notes directly into the model's bootstrap context.

## What neuron adds over base

Neuron ships a `session_search` tool — semantic recall over recent (<7d)
conversation transcripts — that base does not have. The old neuron AGENTS.md rule
`session_start_recall` said: *"at cold start, if the opener names an
entity/feature/continuation absent from the notes, run session_search before
answering."* That was another plea. This hook folds it into the injected
manifest: when open notes exist, the manifest appends an **imperative recall
line** telling the model to `session_search` the opener's topic if no listed note
covers it. So both cold-start recall behaviors (open-note read + session recall)
are now mechanized at bootstrap instead of relying on the model remembering two
separate AGENTS.md rules.

## Design (why it's cheap AND reliable)

It is a **pointer manifest, not a full dump** — near-zero tokens:

- Lists only notes with `status: in_progress` or `status: pending` (finished /
  malformed notes are skipped).
- One line per note: **path · title · status · one-line `done_when`**.
- Sorted most-recent first (mtime), hard-capped (`DINOMEM_OPEN_NOTES_MAX`,
  default 5); overflow summarized as `+N more`.
- Appends the neuron `session_search` recall line (unless disabled).
- Rendered as an **imperative block**, not an FYI.

The deterministic win: the manifest **always appears** when open notes exist, so
"the model forgot to glob / forgot to recall" can no longer happen.

## What it does

On `agent:bootstrap`:

1. Resolves the workspace dir from the event context (`context.workspaceDir` →
   `context.cfg.workspace.dir` → `OPENCLAW_WORKSPACE` / `DINOMEM_WORKSPACE`).
   Fully portable — no hardcoded paths.
2. Globs `<workspace>/memory/_note_*.md`, parses each note's `status:`,
   `# Title`, and `done_when:`.
3. Keeps only open notes, sorts by mtime desc, caps at N, builds the manifest
   (+ the session_search recall line).
4. Pushes a single bootstrap entry (`name: "AGENTS.md"` so it survives the
   subagent/cron session filter, `content:` = the manifest) onto
   `context.bootstrapFiles`. No open notes → injects nothing.

Synchronous, fast, and never blocks — on any error it logs and returns, leaving
bootstrap untouched.

## Requirements

- No external binaries. Pure Node (`fs`), reads only `memory/_note_*.md`.
- A dinomem/neuron install in the target workspace (harmless if none: no-op).

## Config (optional)

| Env / config | Default | Effect |
| ------------ | ------- | ------ |
| `DINOMEM_OPEN_NOTES_MAX` | `5` | Max open notes injected in full; rest become `+N more`. |
| `DINOMEM_SESSION_RECALL` | `1` | Set `0`/`false`/`off` to omit the session_search recall line (e.g. if the workspace has no session_search tool). |

## Enable

```bash
openclaw hooks enable dinomem-open-notes
openclaw gateway restart
```

## Relationship to the AGENTS.md block

This hook **replaces** two rules that used to live in the injected block:
base's `M_session_start` glob rule and neuron's `session_start_recall`. After
enabling it, both are redundant in AGENTS.md (neuron's installer ships the
shortened block accordingly). The remaining recall-discipline rules (M0–M3,
`investigate_before_act`) stay inline — those are model-reasoning directives a
hook cannot mechanize.
