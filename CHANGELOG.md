# Changelog

All notable changes to dinomem-neuron are documented here.

---

## 1.2.3 — 2026-06-29

### Fixed
- **`install.sh` hard-failed while wiring AGENTS.md (`line NNN: title: No such
  file or directory`, exit 1).** The AGENTS.md/TOOLS.md managed blocks were built
  as double-quoted shell strings (`BLOCK="$BEGIN ..."`) containing **unescaped**
  double-quotes mid-string — e.g. `calendar_tool.py create "<title>"`,
  `session_search.py "<query>"`, and `done_when ("pushed", "told user")`. Each
  unescaped quote closed the string early, leaving the following `<title>` /
  `<query>` placeholder in shell command position where `<` parsed as a
  redirection. Under `set -e` the install aborted before AGENTS.md was wired, so
  neuron's policy/tool blocks were never installed. (Same bug class fixed in base
  dinomem v1.2.8; neuron's variant hard-fails instead of degrading.) Fixed by
  building both blocks as quoted heredocs (`cat <<'EOF'`), which makes inner
  quotes and `<...>` placeholders literal. Generated content is byte-for-byte
  unchanged.

## 1.2.2 — 2026-06-25

### Added
- **Post-install re-surfaces the model-selection recommendation at the right
  time.** The split (cheap bulk + compaction, default+thinking for reasoning) is
  manual and only becomes *meaningful* once neuron is installed — but neuron's
  installer never mentioned it, so users who deferred it at base install forgot
  to set anything. Neuron post-install now prints a `MODEL_HINT` /
  `AGENT_ACTION` block framed as “now relevant — set it now or it won't happen,”
  covering `DINOMEM_CHEAP_MODEL` + `compaction.model` (bulk) and the
  default-model reasoning tier. Closes the recommendation-timing gap: base
  install plants it, neuron install (when the reasoning tier appears) repeats it.

## 1.2.1 — 2026-06-25

### Fixed
- **Enforce dinomem base floor at 1.2.0 (was 1.0.0).** Install order is usually
  base-first, sometimes an *old* base. Below base 1.2.0, neuron's `reasoning=True`
  calls are accepted but silently ignored (no thinking budget, no error) —
  reasoning quality degrades invisibly. The installer's existing semver gate now
  requires `1.2.0`, so an old base triggers a clear “update dinomem first” warning
  instead of quietly weakening synthesis/contradiction/promotion. README updated
  to explain the silent-degradation risk.

## 1.2.0 — 2026-06-25

### Fixed
- **Reasoning calls now actually request reasoning.** The 5 LLM call sites in the
  reasoning tier passed no `reasoning=` flag, so they defaulted to `False` and ran
  without a thinking budget — a latent mistag. Flipped to `reasoning=True`:
  - `memory_synthesis.py` ×3 (insight synthesis 600t, merge 1500t, audit 400t)
  - `contradiction_check.py` ×1 (conflict check 400t)
  - `memory_promote.py` ×1 (permanent-promotion validity verdict)
  With dinomem base v1.2.0+, `reasoning=True` routes these to the default model
  with `--thinking` (`DINOMEM_REASONING_THINKING`, default `high`) and keeps them
  off `DINOMEM_CHEAP_MODEL`.

### Added
- **README “Model selection” section** (under “How it all works”): documents that
  the neuron pipeline is the reasoning tier — always default model + thinking,
  never the cheap lever — and points to dinomem base's cheap-model knob. Notes the
  base **v1.2.0+** requirement for the routing to take effect.

## 1.1.5 — 2026-06-24

### Changed
- README “What we mean by learning”: added a closing thesis paragraph — because
  synthesis is an LLM finding patterns across memory (not fine-tuned weights), the
  patterns get deeper and promotion more accurate every time the base model improves,
  with no retraining or rewrite. Fine-tuning approaches redo work each model
  generation; neuron inherits the gain for free.

## 1.1.4 — 2026-06-24

### Changed
- README: replaced timezone-assuming “every night” / “tonight’s cron” wording with
  neutral, accurate phrasing (“on a daily cron” / “once a day” / “next daily cron”).
  The synthesis/graph-build runs on a daily schedule — “night” is relative to the
  reader’s timezone and was both ambiguous and slightly inaccurate.

## 1.1.3 — 2026-06-24

### Changed
- **README repositioned.** New title “Self-Improving Memory for AI Agents” — leads
  with neuron’s differentiator (behavior change via synthesis + promotion +
  injection, no model-weight updates) instead of the generic “Dino Agent Memory
  (Neuron Layer)”.
- **Real subtitle.** Replaced the bare install-prerequisite line (“Requires dinomem
  installed first”) with a value-prop subtitle: nightly pattern-finding → promotion to
  permanent knowledge → every-turn injection, with the January↔November example. The
  dinomem dependency is now carried as value (“learning layer on top of dinomem”).
- Brand language aligned across products: dinotrust = self-enforcing,
  dinomem = self-curating, dinomem-neuron = self-improving / learns-without-fine-tuning.

## [1.1.2] — 2026-06-23

### Fixed
- **`session_ingest` model resolution** — the last remaining hardcoded HF
  snapshot hash (`8b3219…`). Now resolves the local MiniLM snapshot dynamically,
  identical to `session_search`, so ingested session vectors always align with
  query vectors. This file was missed in 1.1.1 and was silently broken on any
  machine whose HF cache pinned a different revision (the case on the current
  host — only `1110a24…` existed, so the hardcoded `8b3219…` never loaded).
- **`generate_topic_index` openclaw binary resolution** — replaced a raw
  hardcoded `/home/linuxbrew/.linuxbrew/bin/openclaw` (no PATH lookup) with a
  `shutil.which("openclaw")`-first resolver + common-location fallbacks, matching
  the rest of the codebase. Previously broke topic-index generation on
  npm-global / non-linuxbrew installs.

### Audit
- Full sweep of both dinomem (public) and dinomem-neuron (private) for hardcoded
  paths/hashes (portability problem #1): **zero** hardcoded HF snapshot hashes
  remain; all openclaw-binary references are now PATH-first with fallbacks. The
  `DINOMEM_*_PLACEHOLDER` tokens are by-design (install.sh sed-replaces every
  token used) and verified complete in both repos. Public repo has no embedding
  model dependency, so it was never affected by the hash bug.

---

## [1.1.1] — 2026-06-23

### Fixed
- **`docs_search` + `docs_ingest` model resolution** — removed the hardcoded HF
  snapshot hash + `DINOMEM_HF_CACHE_PLACEHOLDER` (install-time sed). Both now
  resolve the local MiniLM snapshot dynamically (glob snapshots dir, honor
  `HF_HOME`, fall back to the model id). Ingest and search resolve the SAME
  snapshot so vectors always align. This was a silent-death bug on fresh VPS
  installs where the cached revision differed from the pinned hash.
- **`docs_search` filtered queries** — `--doc` / `--section` filters are applied
  post-fetch, so a narrow filter could return fewer than `k` results even when
  matching chunks existed deeper in the ranking. Fetch now widens progressively
  (k×4 → up to 400) when a filter is active and the set is short, until `k` is
  filled or the corpus is exhausted. No extra cost on unfiltered queries.

### Changed
- **`docs_search` HyDE** — stays default ON (better recall on formal docs) but
  optimized: successful expansions are cached on disk for 24h (repeat query =
  instant, no LLM call), timeout raised to 25s and made env-configurable
  (`DOCS_HYDE_TIMEOUT`, set `0` to disable). Fallbacks are not cached, so a
  transient gateway outage never pins an un-expanded query.

---

## [1.1.0] — 2026-06-23

### Added
- **`session_search` recency rerank** — light, score-gap-based recency tie-breaker.
  Among similarly-relevant sessions the fresher one wins (sessions are usually
  recalled to find the *most recent* discussion of a topic), while a clearly
  more-relevant older session is never buried. Blends on normalized similarity
  score gap (not rank position): small gap → recency decides, large gap →
  relevance dominates. Half-life ~3d, weight 0.25. No recency floor / no
  intraday mode (recall, not news). Over-fetch raised to k×3 for rerank headroom.

### Fixed
- `session_search`: resolve the local MiniLM snapshot dir dynamically instead of
  hardcoding a snapshot hash (hardcoded hashes break silently when the HF cache
  pins a different revision on another machine); falls back to the model id.

---

## [1.0.0] — 2026-06-18

### Added
- Pre-flight checks: Python version, workspace writable, root files size threshold (10k/8k per file)

### Fixed
- Removed `contradiction_alerts` from AGENTS.md injection — behavior is auto-block, not surfaced to user

---

## [0.9.0] — 2026-06-17

### Added
- **Session deep recall** — `session_ingest.py` + `session_search` tool + cron + AGENTS.md wiring
- Injection ranking: reinforce × recency score (replaces reinforce-only sort)
- `calendar_sync.py`: auto-create calendar events from `_note_` date field + dedup log
- `MAX_PERSISTENT_RULES` tuned 12 → 15 for 200k context window

### Fixed
- `memory_promote.py`: replace hardcoded `/root/` path with placeholder (portable across users)
- `session_ingest`: parse `entry.message.role` not `entry.role` (correct OpenClaw JSONL format)
- `session_deep_recall` trigger: tightened conditions, removed redundant fallback line

---

## [0.8.0] — 2026-06-15

### Added
- **HyDE query expansion** in `docs_search.py` — hypothetical document embedding for better long-doc recall
- **Recency section** in MEMORY.md — previous session topics injected via `generate_topic_index.py`
- Calendar integration block in AGENTS.md injection
- Concurrency lockfile in `memory_promote.py`
- `_pin_` files exempt from decay

### Fixed
- `generate_topic_index` fallback regex supports per-item filename format (`YYYY-MM-DD_type_slug.md`)
- AGENTS.md injection block: stripped examples and prose, machine-readable only
- Uninstall: remove `memory/_permanent.md` on `--purge-data`

---

## [0.7.0] — 2026-06-14

### Added
- **Hybrid decay** — `reinforce_count` multiplier + LLM review rescue before expiry
- **Category-based TTL decay** — preference/decision/lesson live longer than prediction/uncertain
- Pre-flight compatibility checks + Compatibility section in README
- AGENTS.md block: RAG routing + contradiction alerts
- Calendar tool: reminders and attendees support, 30min popup baseline always included

### Fixed
- Uninstall: now removes AGENTS.md neuron block and all neuron crons
- Contradiction resolution description in FAQ
- LICENSE: replaced username with correct author name

---

## [0.1.0] — 2026-06-13

### Added
- Initial release
