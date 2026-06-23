# Changelog

All notable changes to dinomem-neuron are documented here.

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
