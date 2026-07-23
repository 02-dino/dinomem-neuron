# Changelog

All notable changes to dinomem-neuron are documented here.

---

## 1.8.3 — 2026-07-23
**Image/OCR ingest no longer cold-boots a ~600–790MB process per image — it now reuses YOUR already-configured model over direct HTTP, with automatic fallback so nothing breaks.** Every scanned page, screenshot, or shared image previously ran through `openclaw capability model run --file`, which spins up a **fresh ~600–790MB Node/OpenClaw runtime per image** just to relay one call to the remote vision model. Batched across a multi-page PDF that stacks fast and can swap-thrash a small VPS. Now the OCR path does a **direct provider-HTTP POST with an `image_url` part**, using the **same model + API key you already configured** for the memory pipeline — zero new setup, no endpoint to enable, no gateway token, no port assumptions. If that path can't resolve (a non-vision model, an anthropic-format provider, or any HTTP failure) it **automatically falls back to the original CLI subprocess**, so every install keeps working exactly as before. Pure upside for the common case, zero regression for the edge cases.

### Changed
- **`procedures/docs_ingest.py` OCR is now HTTP-primary, CLI-fallback.** New `_ocr_via_http()` sends the image as a base64 data-URI (openai-compatible) or `inline_data` (google format) to your own provider, resolved by new `_ocr_resolve_provider()` (mirrors `extract_memory.py`'s proven key/base resolvers — no hardcoded provider). `_ocr_image()` tries HTTP first, then falls back to `openclaw capability model run --file` unchanged. Result: **0 Node cold-start on the hot path** (measured: 0 spawned infer workers during a live OCR), ~600MB → ~0MB local overhead per image.
- Self-configuring: uses whatever provider/key/base you already have. Anthropic-format providers and unresolvable models transparently use the CLI fallback.

### Upgrade notes
- Extract the new ZIP and run `bash scripts/install.sh`. No config changes required, no gateway restart. The efficiency win applies automatically to any provider whose model accepts images; everything else keeps working via the fallback.

---

## 1.8.2 — 2026-07-23
**Cheap-model routing now follows YOUR OpenClaw compaction default — no more hardcoded model that could break on an unknown provider; and the note-system self-resolves completed notes more reliably.** Two robustness fixes. (1) `DINOMEM_CHEAP_MODEL` now resolves through a single anchored control point: your env override → your OpenClaw `agents.defaults.compaction.model` → legacy env → empty (degrade to your OpenClaw default). No hardcoded model id ships in the box, so nothing 404s if you run a provider we've never seen. (2) A `done_when` prose-lint in `claim_note.sh` warns (non-blocking) when a project note's `done_when` isn't machine-checkable — which is what lets the Daily Note Review / Advancer janitors auto-verify against real state (git/grep/exit 0) and self-resolve a finished note, instead of getting stuck on stale checkboxes.

### Added
- **Compaction-anchored cheap-model resolution.** `procedures/_cheap_model.py` (from the dinomem base install) resolves the cheap/no-reasoning model from your own OpenClaw config's `compaction.model` when `DINOMEM_CHEAP_MODEL` isn't set. `extract_memory.py`, `generate_topic_index.py`, and `docs_ingest.py` all route through it. Set the model once (or let it follow compaction) and every cheap-LLM cron follows.
- **`done_when` prose-lint (`scripts/claim_note.sh`).** Claiming a `type: project` note whose `done_when` has no runnable check (git / grep / exit 0 / test -f / rev-parse) now prints a non-blocking WARN telling you to make it locally checkable, so the janitors can auto-resolve it.

### Upgrade notes
- Extract the new ZIP and run `bash scripts/install.sh`. The installer auto-updates the dinomem base too. Internet must be on during install (dependency download). No gateway restart required.

---

## 1.8.1 — 2026-07-22
**Contradiction resolution is no longer first-mover-biased — a correct-but-late insight can now overturn a bad-but-early one.** Before this release, when a new insight contradicted an existing *trusted* one, the newcomer was always blocked — even if it was reinforced far more, and far more recently, than the stale incumbent it conflicted with. That baked in a subtle failure mode: whichever wrong idea got promoted *first* was effectively permanent, and the right idea that arrived later kept getting rejected for contradicting it. 1.8.1 replaces the auto-block with a **reinforcement-weighted resolution**: the challenger and the incumbent are scored by strength = *recurrence × recency*, and the challenger only overturns the incumbent if it has genuinely recurred (≥2 distinct runs) **and** is stronger by a clear margin (≥1.5×). A one-shot fluke still can never demote a trusted insight — the guardrail against noise is intact.

### Added
- **Reinforcement-weighted contradiction resolution (`contradiction_check.py`).** A blocking contradiction is now *re-resolved* instead of auto-applied. New helpers `resolve_by_reinforcement`, `_strength` (reinforce_count × recency, 30-day half-life), and `_load_promotion_state` compare challenger vs incumbent using the L4 promotion state. If the challenger wins, the incumbent is written to a new **`demotion_queue.jsonl`** and the contradiction is downgraded to `overturned` so the challenger passes; otherwise the incumbent stands and the challenger is blocked exactly as before.
- **Demotion-queue consumer (`memory_promote.py`).** The weekly promotion cycle now drains `demotion_queue.jsonl` *before* the decay pass: any queued incumbent that is still `trusted` is demoted (logged as `outweighed_by_reinforced_challenger`) and the queue is cleared. This closes the loop end-to-end — a challenger that out-weighs a bad incumbent actually removes it, rather than merely being marked.

### Safety
- **Fluke guard preserved.** Overturning requires the challenger to have recurred across ≥2 distinct synthesis runs AND to out-score the incumbent by ≥1.5×; a single-shot contradicting insight can never demote a trusted one. Recency decay (30-day half-life) is what lets a fresh, repeatedly-confirmed insight outrank a stale high-count one — same recency model neuron already uses for injection ranking.

### Upgrade notes
- `git pull` then re-run `scripts/install.sh --force`. No new cron; the demotion consumer runs inside the existing L4 promotion cycle. No gateway restart required.

---

## 1.8.0 — 2026-07-21
**Behavior Promotion — your agent now turns repeated workflows into reusable automation, not just skills.** Until now, neuron's convergence engine could only promote a repeated procedure into a **skill** (an on-demand playbook the agent reads when relevant). 1.8.0 expands that same engine to also recognize when a repeated pattern is really a **hook** (something that should fire automatically on an event, e.g. "whenever a message comes in, log the sender") or a **cron** (something that should run on a schedule, e.g. "snapshot state every night at 3am"), and route it to the right surface. Skills still auto-install as before; hooks and crons are **never auto-installed** — they're prepared as *staged proposals* with the exact command to run, and only go live after **you confirm**. New `--status` shows what's promoted/pending; `prune` cleans orphaned proposals.

### Added
- **Behavior Promotion (skill / hook / cron routing).** The convergence engine now classifies each converged pattern by *surface*: an on-demand playbook → **skill** (auto-installed, unchanged), an event-triggered action → **hook** (staged), a scheduled action → **cron** (staged). Hooks and crons are surfaced as staged proposals with the exact base-tool command for one-tap human confirmation — nothing runs unattended.
- **`--status` and `prune`.** `--status` lists promoted candidates and their state (skill / hook / cron, and for staged surfaces whether they're awaiting confirmation). `prune` removes orphaned proposal files left behind by patterns that no longer converge.

### Safety
- **Pre-stage safety validator (`promotion_safety.py`).** Before any hook/cron is even staged, it's checked against OpenClaw's own hooks/cron docs. **Hooks get hard checks** — a self-triggering loop (a hook whose action re-fires its own event) or a dead/never-firing event is a **blocker** and the proposal is rejected outright; a silent no-op push or a pipeline-stalling handler is a warning. **Crons get a light check** — a token-burn warning for over-frequent schedules; OpenClaw's native guardrails and the confirm gate cover the rest. Fail-closed for hooks: if it can't be validated safe, it isn't staged. This is the guarantee that the new automation *can't quietly break your agent*.
- **Cross-install duplicate guard (`promotion_dedup.py`).** Neuron installs *on top of* dinomem-base, so a behavior base already runs (under its own name) could be promoted again under neuron's slug — installing a **second** hook/cron that fires twice (double reply, double cron run). The dedup gate queries your **live** installed hooks/crons and skips a proposal that already exists, matching in four tiers: exact **name** → **structural signature** (same hook event / same cron schedule + same action-kind, read from the generated code so it's language-neutral) → script-gated **fuzzy** text → **multilingual semantic** (reuses neuron's existing embedding model, no new dependency) as the cross-language catch. This works whether you describe your automations in Bahasa Indonesia, English, or any other language — it matches on what the automation *does*, not the words you used. Duplicate → skipped, never staged.

### Upgrade notes
- `git pull` then re-run `scripts/install.sh --force` to pick up the new promotion modules and `--status`/`prune`. Existing skills/hooks/crons are untouched. No gateway restart required unless the installer reports cron changes.
- Nothing new fires on its own from this release: hooks and crons stay as staged proposals until you explicitly confirm each one. The only automatic behavior is the same skill auto-install neuron already did.

## 1.7.5 — 2026-07-20

**Bare-relative filesystem paths stored in memory silently break on later reads.** The memory extractor (`extract_memory.py`) copies whatever path an agent typed during a session verbatim into `memory/YYYY-MM-DD_*.md`. When an agent wrote a workspace-relative path (e.g. `workspace-foo/kb/x.json`) instead of an absolute one, a *later* session reading that note from a different working directory would resolve it wrong — on installs where the working directory equals the workspace name, the path silently doubles (`workspace-foo/workspace-foo/kb/...`) and the read fails with ENOENT. Found live on one install: 59 memory files carried bare-relative paths, 4 of them written the same day (not stale backlog — actively-generated), producing a steady drip of ENOENT read failures against files that existed at the correct absolute path all along. The extractor prompt was **silent** on path format, so nothing steered the LLM toward absolute paths at authorship time.

### Fixed
- **Extractor prompt now mandates absolute filesystem paths.** Added an explicit `FILE PATHS MUST BE ABSOLUTE` rule to both `extract_memory.py` prompt templates (session-archive extraction and per-note distillation): filesystem paths must be stored as full absolute paths (starting with `/`), never workspace-relative, with an instruction to expand a relative path to absolute before storing. Fixing this at authorship (the prompt that *generates* the note) rather than with a post-hoc path-rewriting heuristic keeps the fix portable and workspace-agnostic — no hardcoded workspace root, no false-positive risk from guessing whether a string is a path that should be absolutized. This mirrors how 1.7.4 solved the `$WS`-undefined skill-example bug: fix the source of the relative reference, don't paper over it downstream.

### Upgrade notes
- `git pull` to pick up the extractor prompt change. No install re-run or gateway restart required — the new prompt applies on the next extraction run. Existing memory notes already written with relative paths are not rewritten by this change (they are harmless on installs whose working directory differs from the workspace name; on installs where they double, re-express the affected paths as absolute or let them expire).

---

## 1.7.4 — 2026-07-14

**Cheap-model 404 silently killed the memory pipeline for any buyer who never set `DINOMEM_CHEAP_MODEL`.** `extract_memory.py`, `generate_topic_index.py`, and `docs_ingest.py` all previously fell back to a *hardcoded* model (`DINOMEM_NO_REASONING_MODEL` env, or if unset, a literal `ninerouter/gc/gemini-2.5-flash`) when no cheap-tier override was configured. That default 404s through `capability model run` (a known gemini-cli misroute) — so anyone who installed neuron and never manually set `DINOMEM_CHEAP_MODEL` (the common case; it's opt-in and undocumented as a requirement) was silently failing **every single cheap-tier LLM call, permanently**, with no self-heal. Unlike the 1.7.3 backlog-draining fix, a 404 isn't transient — it never recovers on its own. This one was quietly starving the memory pipeline (extraction, topic indexing, docs ingest) on an unknown number of live installs.

### Fixed
- **Standardized all three no-reasoning scripts on the `DINOMEM_CHEAP_MODEL` tag** (was a one-off `DINOMEM_NO_REASONING_MODEL` + hardcoded `gemini-2.5-flash` default). Fallback chain is now `DINOMEM_CHEAP_MODEL → DINOMEM_NO_REASONING_MODEL (legacy back-compat) → unset`. When both are unset, `--model` is omitted entirely from the `capability model run` call — the script simply uses OpenClaw's normal default model instead of a broken hardcoded one. Default is now safe either way: cheap-and-working when configured, normal-tier-and-working when not, never broken-and-silent.
- **Installer now propagates the right env to the right cron tier**, since crond doesn't inherit the interactive shell: `CHEAP_ENV` (from `DINOMEM_CHEAP_MODEL`) reaches the no-reasoning crons (`docs_ingest`, `generate_topic_index`, and now `auto_session_reset` — the heaviest LLM consumer, all reasoning=False); `REASON_ENV` (from `DINOMEM_REASONING_THINKING`) reaches the reasoning crons (`memory_synthesis`, `contradiction_check`). Component 1 (Verified Note Deleter) and Daily Note Review agentTurn crons are pinned to the cheap model when set. Project Advancer / Improver are intentionally left on the capable default — they write and debug real artifacts unattended and forcing them cheap would degrade that work.
- **Cross-invocation extraction lock + crash-proof contradiction-check.** `extract_memory.py` previously had no protection against two concurrent invocations (e.g. a manual backlog catch-up run overlapping the cron's own run) racing `.processed_archives.json` — reproduced live as a genuine last-write-wins dedup-log race. Added a self-contained flock lock (`/tmp/dinomem_extract_memory.lock`) inside the script itself, independent of the orchestrator's own lock (which only guards two `auto_session_reset.py` runs against each other, not bare invocations). Also fixed a reproducible crash where the contradiction-check step could select the same memory file as a merge candidate for two different insights in one run — the first iteration deletes the file, the second then crashes hard on `read_text()` against a file that's already gone, killing the entire extraction process uncaught. Now skips cleanly. Added a top-level exception handler around `main()` so any future unforeseen crash writes a `'crashed'` status instead of leaving `.extract_memory_status.json` stale, preventing the orchestrator from falling back to a blanket `FAILED` when most of the batch actually succeeded. Ported from base `dinomem` commit `54cb810`.
- **Skill script-call examples had an undefined `$WS`.** `note-claim` and `memory-pinning` skill docs referenced `$WS` in example commands with no way to resolve it outside a live install shell; `retrieval-routing` had cwd-dependent paths that silently failed when the model ran from an unexpected directory. Installer now bakes the absolute workspace path into these examples via a `DINOMEM_WORKSPACE_PLACEHOLDER` sed pass, same mechanism already used elsewhere in the installer.

### Upgrade notes
- If you rely on `DINOMEM_CHEAP_MODEL`, no action needed — same variable name, same behavior, just no longer masked by a broken hardcoded fallback.
- If you never set `DINOMEM_CHEAP_MODEL`, your cheap-tier crons were silently 404ing before this release; they will now succeed on OpenClaw's default model (slightly higher cost than the intended cheap tier, but working). Set `DINOMEM_CHEAP_MODEL` to restore the intended low-cost routing.
- `git pull` then re-run `scripts/install.sh --force` to pick up the installer's env-propagation fix; requires `openclaw gateway restart` for cron changes to take effect on already-registered jobs (or let the next scheduled cron re-registration pick it up).

---

## 1.7.3 — 2026-07-13

**Memory-extraction false "FAILED" during large/first-time backlog (misleading orchestrator status).** `extract_memory.py` processes archived sessions through an LLM in small batches; the orchestrator (`auto_session_reset.py`, shared with base) wraps it in a hard `subprocess.run(timeout=300)`. On a large backlog (fresh install with lots of pre-existing history, an outage that let archives pile up, or just a busy `/new`-heavy user), one 300s tick can't clear everything, so the subprocess gets killed mid-batch and every 15-min run logged `"Memory extraction: ⚠️ FAILED"` — even though it wasn't broken: `extract_memory.py` dedups via `.processed_archives.json` and advances the backlog a bit further on every tick (self-healing over time). That's a **misleading false alarm**, not a real failure.

### Fixed
- **`extract_memory.py` now writes a status file after every batch, not just at the end.** `logs/.extract_memory_status.json` records whether the run is still draining a backlog vs. actually failed (0 archives cleared), and is refreshed after each batch so it survives the orchestrator's SIGKILL-on-timeout — the exact scenario that produced the false alarm.
- **`auto_session_reset.py` (shared with base) reads it and reports accurately:** `"⏳ IN PROGRESS (N archive(s) remaining, self-healing — not a real failure)"` instead of a blanket `"⚠️ FAILED"` when it's just backlog draining, reserving `FAILED` for when nothing was actually cleared.
- Backward-compatible: if the status file is missing (older `extract_memory.py`, or the subprocess died before writing anything), it falls back to the old blanket `FAILED` wording — never silently hides a real failure.
- Ported in lockstep with the same fix on base `dinomem` to keep `extract_memory.py` (the neuron superset over base's copy) at parity; verified `main()` is now byte-identical between the two repos aside from neuron's `NO_REASONING_MODEL` routing elsewhere in the file.
- Repro'd end-to-end on both repos: seeded a fake 20-archive backlog, hard-killed the extraction subprocess mid-batch (`timeout --signal=KILL`), confirmed the status file stayed fresh (`remaining_backlog>0`, `backlog_draining`) and the orchestrator correctly read it as not-a-real-failure.

---

## 1.7.2 — 2026-07-12

**Installer robustness — hook liveness self-check + global-dir self-heal (fixes silent hook no-op).** `openclaw hooks enable dinomem-open-notes` only flips the config `enabled` flag; it does not confirm the gateway can actually *load* the hook. When the installer's `$WS` is not the gateway's scanned workspace, the hook copied into `$WS/hooks/` lands in an unscanned dir and **silently never fires at bootstrap** — the exact failure the open-notes hook exists to prevent — while the installer still claims success.

### Fixed
- **Self-check now asserts hook eligibility via `openclaw hooks check --json`** (`dinomem-open-notes` ∈ `eligible[]`), not a `hooks list` grep (which false-negatives: the CLI table emoji-wraps and splits the hook name across two lines).
- **Self-heal:** if the hook isn't eligible from `$WS/hooks/`, the installer copies the pack into the always-scanned global `~/.openclaw/hooks/dinomem-open-notes/`, re-enables it, and warns to restart — so the headline deterministic-open-note-injection feature actually runs regardless of workspace layout.

---

## 1.7.1 — 2026-07-12

**Installer robustness — plugin registration no longer crash-loops the gateway on workspace teardown, and self-heals a previously-broken config.** The `note-claim-refresh` plugin was installed into `$WS/plugins/` and registered in the **global** `openclaw.json` with a workspace-relative `plugins.load.paths` entry; if that workspace was ever removed/moved, the dangling path made the config schema-invalid (`plugins is .strict()`) and the gateway refused to start (crash-loop). The per-plugin config was also written at the **invalid top-level `plugins.config`** key instead of `plugins.entries.<name>.config`.

### Fixed
- **Plugin installs to the persistent global extensions dir** (`$OPENCLAW_DIR/extensions/note-claim-refresh`) instead of `$WS/plugins/`, so `load.paths` never points inside a transient workspace.
- **Per-plugin config moved to `plugins.entries.note-claim-refresh.config`** (schema-valid) with `{ enabled: true }`; the installer no longer writes a top-level `plugins.config` block.
- **Self-heal on upgrade:** the wiring step now prunes any stale workspace-relative `note-claim-refresh` `load.paths` entry, and migrates + deletes any leftover invalid top-level `plugins.config` from older installer builds. A config broken by a prior build is repaired in place on the next install — no more manual hand-fixing.
- Verified by an isolated unit test (8/8 assertions) that replays the exact crash-loop config shape and asserts a valid, deduped, self-healed result. No live-gateway install run was used (that was the original footgun).

---

## 1.7.0 — 2026-07-12

**Hooks + Skills release — the long inline AGENTS.md block is replaced by a deterministic bootstrap hook and four AgentSkills.** Neuron's injected AGENTS.md block shrinks from ~118 lines to ~18: the always-loaded rules become on-demand skills, and the two cold-start recall rules become a mechanism instead of a plea. **Seamless upgrade from any prior neuron *and* from dinomem base** — `--force` refreshes the managed block in place (no stacking), and neuron's superset hook/skill overwrite base's same-named copies cleanly.

### Added
- **`dinomem-open-notes` hook (neuron superset).** Overwrites base's copy of the same hook: on every `agent:bootstrap` it injects a blocking manifest of OPEN `_note_*.md` (as base does) **plus** a neuron-only recall line telling the model to run `session_search` when the opener names something no open note covers. This mechanizes the old `M_session_start` glob rule **and** neuron's `session_start_recall` rule — the model no longer has to *remember* to look. Toggle the recall line with `DINOMEM_SESSION_RECALL=0`; cap injected notes with `DINOMEM_OPEN_NOTES_MAX` (default 5). Always overwrites base (even without `--force`) so the upgrade never keeps base's recall-less hook.
- **Four AgentSkills** (extracted from the inline block; read on-demand):
  - `project-notes` — project-note format, estimate/heaviness gates, turn-boundary execution, self-check, `resume_state` discipline.
  - `memory-pinning` (neuron superset — overwrites base's) — `_pin_`/`_note_` format + the semantic **dedup gate** (`memory_dedup_check.py`) + auto-note triggers.
  - `retrieval-routing` — which retrieval tool per query (`docs_search`/`session_search`/`memory_search`/`data_query`/`graph_search`/grep) + long-doc ingest + structured-data (`data_query`) usage.
  - `note-claim` — the live-session write side of the note-claim protocol (`claim_note.sh` claim/refresh/release).

### Changed
- **Injected AGENTS.md block cut ~118 → ~18 lines.** The moved procedures now live in the skills above; the block keeps only short when-to-use stubs, a `cold_start_recall` note pointing at the hook, and a one-line retrieval-tool quickref.

### Fixed
- **`--force` now refreshes the managed AGENTS.md block in place (was a no-op skip).** Upgrades from an older neuron (longer inline block) actually pick up the new short block: the installer strips the old `BEGIN:dinomem-neuron..END` span and writes the current block — idempotent upsert, never stacks a second block. Targets neuron's own markers, so base's separate `BEGIN:dinomem` block is untouched.
- **`rm -rf` before every `cp -r`** for the hook and skills, preventing the copy-into-existing-dir nesting footgun on re-install.

### Upgrade notes
- Existing neuron/base users: re-run the installer with `--force` to refresh the AGENTS.md block and deploy the hook + skills, then `openclaw gateway restart`. No manual steps.
- `uninstall.sh --purge` removes the three neuron-exclusive skills; the shared-name `memory-pinning` skill and `dinomem-open-notes` hook are removed only under `--with-base` (base still relies on them otherwise).

---

## 1.6.8 — 2026-07-11

**Critical install-time fix — prevents "bot goes completely silent after install".** On OpenClaw **2026.6.x+**, setting `plugins.allow` *without* also setting `plugins.bundledDiscovery` now fails schema validation (`plugins: Invalid input`). Our installer wired the `note-claim-refresh` plugin into `plugins.allow` but never stamped `bundledDiscovery`, so on the very next `openclaw gateway restart` the config was rejected and the gateway refused to start — total silence, no responses. **Recommended for anyone who installed on OpenClaw 2026.6.x or newer.**

### Fixed
- **Installer now stamps `plugins.bundledDiscovery="compat"` whenever it touches `plugins.allow`.** `"compat"` preserves legacy bundled provider/channel discovery, so existing chat channels (Telegram/WhatsApp/etc.) keep working. This makes the wired config pass validation on new OpenClaw versions and restart cleanly.
- Existing installs that are already stuck can self-repair with `openclaw doctor --fix` (adds `bundledDiscovery`), then `openclaw gateway restart` — no reinstall needed.

> Note: `python3 procedures/memory_graph.py` printing `no usable chunks with embeddings found` on a fresh install is **expected**, not an error — the memory graph simply has nothing to build until the agent accumulates memories. It is unrelated to the silence bug above.

---

## 1.6.7 — 2026-07-11

Concurrency-safety release: the **Project Advancer** and **Project Improver** crons now claim project notes through the same atomic lock the live session uses. Closes a race where an unattended cron could silently overwrite ('clobber') the claim of a note you're actively editing in a live session — and then advance or re-nag a note out from under you. **Recommended for anyone running the Advancer/Improver crons alongside interactive work.**

### Fixed
- **Advancer/Improver claim is now atomic (was a TOCTOU race).** Previously both crons *hand-wrote* `claimed_by`/`claimed_at` with a raw file edit — outside the `flock` that `claim_note.sh` holds. A claim written by a live session between the cron's read and its unlocked write was silently overwritten. Now Step 3 runs `claim_note.sh claim <note> <claimant>` (Advancer: `advancer-<runid>`, Improver: `component2-improver`) and **skips the note on exit 3 (BUSY)** — so the cron's check-and-set goes through the *same* lock as the live session. No more clobber.
- **Claim release is now atomic too.** Both crons release via `claim_note.sh release <note>` instead of hand-stripping the frontmatter.
- **Stale progress announcements.** The Advancer now re-reads `current_step`/`status` from disk immediately before composing a progress report, so it can no longer announce a cached earlier step (e.g. a ghost 'step 1/6' after the note already reached a later step).

### Notes
- The prose 120-min live-session / 30-min other-job guard is kept as a cheap pre-filter, but it is no longer trusted to be race-safe on its own — the `claim_note.sh` flock is the authoritative gate.
- Ships in the neuron `install.sh` cron templates (`$WS`-relative), so every install's Advancer/Improver inherits the atomic claim.

---

## 1.6.6 — 2026-07-11

Memory-hygiene release: a pre-write **semantic dedup gate** for auto-pin / auto-note. Stops your agent from stacking a second pin/note on a topic an existing one already covers (and, worse, writing a contradictory duplicate). **Recommended for everyone** — the longer an agent runs, the more its `memory/` accretes near-duplicate `_pin_`/`_note_` files; this closes that hole.

### Added
- **`procedures/memory_dedup_check.py` — advisory dedup gate.** Before authoring a NEW `_pin_`/`_note_`, the agent embeds the candidate and compares it against every existing pin/note in `$OPENCLAW_WORKSPACE/memory/`. If an existing file covers the same topic, it returns `{match:true, file, suggestion}` so the agent MERGES into that file instead of creating a duplicate. Advisory only — it never blocks a memory write.
- **AGENTS.md policy wiring.** `memory_pin` and `auto_note` now run the dedup check first (`dedup_first`) and merge-vs-create based on the result. Shipped in the neuron AGENTS.md template so every install gets it.

### Notes
- **Semantic, not slug-based.** Two files with different slugs but the same topic are caught; a slug check would miss them.
- **Adaptive z-score scoring.** Absolute cosine thresholds don't generalize on e5-small (anisotropic — everything clusters in a narrow high band). The gate flags a hit only when the top match is a statistical **outlier** above the corpus's own baseline, so it self-tunes per-workspace. Override via `DINOMEM_DEDUP_Z` / `DINOMEM_DEDUP_ABS_FLOOR` if a corpus over/under-flags.
- **Backend-agnostic + fail-open.** Uses whichever embed backend the install has (`tools/_embed.py` or a TEI server); if none is available it cleanly skips (`{match:false, skipped:"no-embed-backend"}`) and never blocks a write.

---

## 1.6.5 — 2026-07-10

Installer reliability + config-safety release. Fixes the neuron→base install friction that forced manual base installs, and adds a self-healing config guard that prevents the "bot goes silent after install" failure. **Recommended for everyone — especially anyone who hit a manual base-install workaround or a dead gateway after installing.**

### Fixed
- **neuron→base handoff no longer dies on `--agree`.** The neuron installer passes `--agree` through to the base installer after the human accepts the neuron license; base previously rejected it as an unknown arg and exited, forcing the buyer to install base manually first. Base now accepts+ignores `--agree`. Full base dry-run now completes end-to-end (exit 0) with the exact flag neuron passes.
- **Cron registration is now verified, not assumed.** Every cron add reads the job back by name (`cron list --json`) after adding; if it didn't actually persist, the installer prints a `[FAIL]` + a precise manual-add fallback line instead of silently assuming success. This was the worst prior failure — crons silently not registering with no signal.
- **Existing healthy TEI on :8080 is reused, not clobbered.** If a healthy embed server already answers `/health` on 8080, the installer reuses it instead of trying to start a second container on a taken port.

### Added
- **Surgical config recovery on schema-validation failure.** If a config change isn't accepted by your installed OpenClaw version's schema, the installer no longer either (a) crashes the gateway on next restart or (b) blanket-reverts every dinomem change. It now diffs the patched config to leaf-paths, re-applies each change one at a time validating after each, and **keeps every change your OpenClaw accepts while dropping only the specific rejected field(s)** — reporting exactly what was kept/skipped. This is the direct fix for the "bot goes completely silent after install" report.
- **Post-install self-check with a "bot goes silent" guard.** The neuron install epilogue prints a PASS/WARN table that now leads with **config schema validity** (safe to restart?) and **gateway liveness** (`openclaw status` responding?), then checks all crons by name, TEI `/health`, python imports, and memorySearch wiring. Purely reporting; never exits nonzero.
- **langchain-chroma install retry** and clearer doctor guidance for verifying the install non-interactively.

---

## 1.6.4 — 2026-07-10

Reliability fix for the note-claim-refresh hook: a long project note could silently lose its live-session claim while you were still actively working it. **Recommended for everyone.**

### Fixed
- **note-claim-refresh no longer misses claims buried below the first 2 KB of a note.** The hook scans each `memory/_note_*.md` to find notes the current session holds a claim on, then keeps that claim fresh so the Advancer/Improver/Deleter crons back off. It previously read only the first **2048 bytes** of each note to locate the `claimed_by:` line — fine for short fenced notes, but `claim_note.sh` writes `claimed_by`/`claimed_at` at the **end** of a bare project-note frontmatter block (the one with a long `steps:` list and a multi-paragraph `resume_state:`), which can land well past 2 KB (observed at byte ~5000 and ~12000 on real notes). Past that cutoff the hook saw no claim, silently skipped the refresh, and the claim aged out past its 120-min window — letting a background cron reclaim a note the session was still actively editing. **No error was ever raised**, so it looked like the hook simply wasn't firing. The scan now reads to the **actual frontmatter terminator** (the closing `---` for fenced notes, or the first blank line for bare project notes), capped at 32 KB, so the claim line is always found regardless of note length.
- **Broadened refresh trigger (any tool call, throttled).** The hook now refreshes a held claim on **any** tool call while a session is active, not only on edits to the claimed note — a session can legitimately hold a claim on note X while spending an hour doing exec/git/read work that never edits X, and the old edit-only trigger let the claim age out during exactly that kind of long build. A per-agent time throttle keeps the scan to at most once per 5 minutes, so the added coverage stays negligible. Fully generalized (per-install `agentId` + dynamic workspace resolution; no hardcoded paths).

---

## 1.6.3 — 2026-07-10

Upgrade-safety hardening for the LLM crons: refreshing to a new version now updates existing cron prompts instead of leaving old installs frozen on stale prompts. **Recommended for anyone upgrading from an older install.**

### Changed
- **`daily_note_review` cron now upgrades in place (prompt refresh, no freeze).** Same class of bug fixed for the project-cron trio in 1.6.2, applied to the remaining evolving-prompt cron: the installer previously did "if a cron named X exists → skip it," so an older install kept running `Daily Note Review` with its *old* prompt forever, even after upgrading the neuron. Registration is now an **upsert** — if the job already exists, its prompt is refreshed in place via `cron edit <id> --message` and it stays enabled on its own schedule (it is self-scheduled, not gate-driven, so unlike the trio it is **not** disabled); if it does not exist, it is created. No duplicates, no stale prompt. Re-running the installer is idempotent.
- **Audit note:** the OS-crontab jobs (`memory_graph`, `memory_synthesis`, `memory_promote`, `docs_ingest`) were already upgrade-safe (they use the `upsert_cron` helper, which replaces a changed line). `Project Cron Gate` intentionally stays skip-if-exists — it is a pure command-cron with no evolving prompt, so there is nothing to refresh. After 1.6.2 + 1.6.3, **every** cron the installer registers is upgrade-clean.

---

## 1.6.2 — 2026-07-10

The project-note cron trio (Project Advancer / Improver / Verified Note Deleter) no longer costs an LLM turn on idle ticks. **Recommended for everyone — it removes ~192 mostly-no-op LLM turns/day.**

### Changed
- **Zero-LLM cron gate — the 15-min heartbeat is now free unless there is real work.** Previously each of the three jobs was an `agentTurn` cron that fired on its own schedule (Advancer every 15 min, Improver/Deleter twice an hour). An `agentTurn` spins up a full LLM session *before* its own "Step 0" pre-check script runs — so the cheap `check_*.sh` gate could only ever run *after* paying for an LLM turn. In practice ~95% of those fires had nothing to do and just spun up the model to run a bash check and reply `NO_REPLY`. Now the installer registers the three jobs **disabled** (they never self-fire) plus a single new **command-kind** cron, `Project Cron Gate` (`scripts/cron_gate.sh`, every 15 min), which runs in the Gateway process at **$0 LLM**. The gate runs the three cheap checks (`check_projects.sh`, `check_improvable.sh`, and the new `check_deletable.sh`) and only calls `openclaw cron run <id>` to force-enqueue a job's LLM turn when that job's check reports real work. Net: idle ticks cost nothing; the LLM only spins up when there is an actual project note to advance/improve/retire.
- **Graceful, loud fallback when a gateway forbids command crons.** Command-kind crons require `operator.admin`. If a buyer's gateway rejects or downgrades the command job, the installer detects it (real probe via `cron get`, not just an exit code), **re-enables the three `agentTurn` jobs on their original schedules** so behavior is fully preserved, and prints a prominent warning explaining that this fallback path costs LLM on every idle tick and how to enable the free path.

- **Upgrade-safe cron registration (no duplicates, no orphaned enabled copies).** The three job blocks previously did "if a cron with this name exists, skip it." On an upgrade from an older install that already had the self-scheduled `agentTurn` crons, that skip meant the old jobs stayed **enabled on their original 15/30-min schedules with their old prompts**, while the new gate was *also* created — so both fired (double-work) and the old prompt never got refreshed. Registration is now an **upsert**: if a job with the same name already exists, it is **disabled and its prompt refreshed in place** (and its existing id is captured so the gate drives it); only genuinely new installs create the job. Result: upgrading from any older version cleanly converts the old crons to gate-driven, disabled jobs — no duplicates, no leftover enabled copy, prompt updated. Re-running the installer is idempotent.

### Added
- **`scripts/cron_gate.sh`** — the zero-LLM dispatcher (command-cron target). Job IDs are injected per-install via `--command-env`; empty/missing IDs skip that lane. Always exits 0 (a gate that errors must not spam failure alerts) and emits `NO_REPLY` when it triggers nothing.
- **`scripts/check_deletable.sh`** — zero-LLM pre-gate for the Verified Note Deleter lane (exits 0 only when a `verified: true` note exists), mirroring `resolve_done_notes.py`'s own gate.

---

## 1.6.1 — 2026-07-10

### Fixed
- **`install.sh` — Project Advancer cron registration had a broken Python heredoc that killed the whole installer.** A literal (unescaped) newline inside the `"message"` JSON string in the Project Advancer cron payload made the embedded `python3 - <<PYEOF` block a `SyntaxError`. Under `set -euo pipefail` (active at the top of `install.sh`), that syntax error propagated as a non-zero exit and **aborted the entire install script** at that step — meaning Project Improver, Verified Note Deleter, Daily Note Review, and any later setup steps never ran on any fresh install (upgrades/re-runs where the cron already existed were unaffected, since the `exists` branch short-circuits before the broken heredoc). Live since 2026-07-02 (`fc1d1a8`). Fixed by properly `\n`-escaping the message string; verified by extracting and executing the exact heredoc standalone (parses, produces valid JSON) and running `bash -n` over the full script.
- **Project Advancer cron — real progress/approval reports could be silently swallowed on default installs.** The cron ships with `delivery.mode: "none"` by default (correct — it disables only the runner's automatic chat-fallback). But the prompt's own "announce" instructions were descriptive text only, never an explicit `message` tool call — so on a stock install, genuine state-change reports (step completed, safety-gate pause needing approval) had no path to actually reach the owner. Reworded Step 10 to explicitly call the `message` tool for real reports (matching how Project Improver's Step 11 already does it), independent of `delivery.mode`.
- **Project Advancer cron — idle/no-op runs could spam chat if `delivery.mode` is later switched to `announce`.** The "stop silently / no announce" instructions never told the agent to emit the literal `NO_REPLY` token, which is what OpenClaw's cron delivery layer actually checks to suppress a run's output — a plain sentence like "No in-progress project notes found" still gets delivered under `announce` mode. Added an explicit SILENT-STOP CONTRACT to the prompt: every no-op/skip/already-paused path now must output exactly `NO_REPLY` and nothing else.

---

## 1.6.0 — 2026-07-09

Auto-refresh for the note-claim protocol — the live session no longer has to remember `claim_note.sh refresh`. **Recommended for everyone; it closes a real race that lets a background cron overwrite a note you're mid-edit.** A new `after_tool_call` hook plugin (`plugins/note-claim-refresh/`) runs the claim refresh **as a consequence of editing a note**, so the claim the Advancer/Improver/Deleter crons read to back off can never go stale from a forgotten manual refresh.

### Added
- **`note-claim-refresh` hook plugin — automatic claim refresh on note edits.** The `note_claim_protocol` (in `AGENTS.md`) tells the live agent to run `claim_note.sh refresh` every turn it keeps editing a `memory/_note_*.md`; that is documentation-only, and a forgotten refresh lets a cron grab the note mid-edit and double-write it (proven race). This ships an `after_tool_call` plugin that fires on any `edit`/`write`/`apply_patch` to a `memory/_note_*.md` and runs `claim_note.sh refresh` fire-and-forget — **observe-only, never blocks or mutates the tool result**, and `claim_note.sh` self-guards (no-op if the note isn't yours). Workspace is resolved **dynamically at runtime** (plugin config `workspaceDir` → `$OPENCLAW_WORKSPACE` → derived from the target note's own absolute path → ctx), so it works for any install with no hardcoded path; it no-ops silently if no `scripts/claim_note.sh` is found. `install.sh` copies the plugin into `<workspace>/plugins/note-claim-refresh/` and wires `openclaw.json` (`plugins.allow` + `plugins.load.paths` + `plugins.config.note-claim-refresh.workspaceDir`), idempotently; `uninstall.sh` removes the dir and unwires the config. A gateway restart loads it (`onStartup`).

Multilingual embeddings for neuron's Python semantic tools. **Recommended for anyone whose docs/sessions/data aren't purely English.** `docs_search`, `session_search`, the structured-data `--semantic` path, and their ingest procedures now embed with `intfloat/multilingual-e5-small` (multilingual EN/ID + majors, 384-dim, 512-token context) instead of the English-centric `all-MiniLM-L6-v2`. Same 384-dim, same footprint.

### Changed
- **Default embedding model → `intfloat/multilingual-e5-small` across neuron's Python tools.** `tools/_embed.py` is now the single source of truth (model id via env `DINOMEM_EMBED_MODEL`, fallback const) and adds **e5's asymmetric prefixing centrally**: `embed_query` prepends `query: `, `embed_documents` prepends `passage: ` (LangChain Chroma routes searches vs ingest through those methods, so every tool gets correct prefixing for free). `tools/docs_search.py`, `tools/session_search.py`, `procedures/docs_ingest.py`, and `procedures/session_ingest.py` carry the matching model swap + a local `_PrefixedHFE` wrapper so ingest (passage) and query vectors align. `data_query --semantic` / `data_register` inherit it through `_embed.py`. Prefixing can be disabled for a symmetric model via `DINOMEM_EMBED_PREFIX=0`. **Requires the base repo's TEI on cpu-1.6** (cpu-1.5 can't download this model). Existing Chroma stores must be re-ingested to benefit (new dimspace semantics); fresh installs embed against e5 from the start.
- **Embedding model stamped into every Chroma store + startup mismatch warning.** Each store now records the embedding model id in its collection metadata (via `_embed.py`); on open, a mismatch between the store's recorded model and the currently-configured one is warned so a silent dimension/space mismatch (e.g. querying an old all-MiniLM store with e5 vectors) surfaces instead of returning garbage. `tools/_embed.py --selftest` exercises the resolver + query/passage prefixing end-to-end for install verification.

### Added
- **`scripts/reindex_all.sh` — sequential rebuild of every Chroma store with a real post-build query gate.** Rebuilds docs → sessions → prose-indexes **sequentially, never parallel** (VPS-RAM safe) and gates each store on an **actual search through the real module** (not a row count — a corrupt HNSW can have rows yet crash a query): non-crash = pass, empty = soft-warn, crash = hard-fail. For switching embedding models or recovering a corrupt store.
- **`scripts/migrate_embeddings.sh` — safe auto-migration when the embedding model changes.** Idempotency marker (`kb/.embed_model`, re-run is a fast no-op) + disk preflight (du vs df + headroom, aborts if short) + **backup-by-rename** (`mv` to `.bak-<ts>`, never `rm`) + rebuild delegated to `reindex_all.sh` + **restore-on-fail** (reindex fails → backup moved back, marker not written, install untouched) + resumability guard (refuses to clobber an existing interrupted-run backup). Purge of old backups is opt-in only (`--purge-backups`); `--dry-run` supported.

### Fixed
- **`scripts/claim_note.sh` now claims project notes (race-protection gap).** The note-claim helper — which the Advancer / Improver / Deleter crons read to back off a note a live session is editing — previously only handled `---`-fenced YAML frontmatter. **Project notes** (`type: project`) use a different shape: a `# Heading` first line followed by bare `key: value` frontmatter that ends at the first blank line (the same block `check_projects.sh` and `resolve_done_notes.py` read). On that shape the old script could not read the claim and, when writing, injected a stray top-level `---` block — producing a **duplicate/empty `claimed_by`** that the crons then read as *unclaimed*, so a background cron could advance a project note while a live session was mid-build. `read_claim`/`write_claim` now detect **both** frontmatter formats; project-note claims are written **in place** before the first blank line (drop prior claim lines, append the fresh claim), with **exactly one `claimed_by`** and no stray fence. Fenced-format notes are unchanged. Pass the note as a **full path** (`claim_note.sh` locates by path arg).

---

## 1.5.0

New retrieval capability: **structured data (exact SQL)**. **Recommended for anyone who works with spreadsheets, exports, or any tabular/JSON data.** Adds a third, separate store so the agent can answer the precise questions embeddings can't — *how many, which ones, under $X, on this date, sorted by, grouped by, the total* — with real `WHERE`/`ORDER BY`/`COUNT`/`SUM`/`GROUP BY`/`JOIN`, not similarity. Two optional aids ride alongside the exact path: **fuzzy value routing** (resolves typo/synonym filter values, no embeddings) and **per-prose-column semantic rank** ("find rows about X" over free-text columns like survey feedback) — both strictly opt-in, neither regresses the exact-SQL path.

### The point: separate stores, one router — memory is never polluted
dinomem-neuron now keeps **different stores for different kinds of retrieval, and picks the right one automatically from the shape of the request** — no manual selection, nothing cross-contaminates:

| Kind of question | Store | Tool | How it's chosen |
|---|---|---|---|
| row · count · range · filter · sort · aggregate (csv/json/parquet) | `kb/data_store/data.duckdb` (DuckDB, exact SQL) | `data_query` | request shape (numbers/filters/totals) |
| meaning · "explain" · "like this" · passage (long docs) | `kb/vector_db_docs/` (vector embeddings) | `docs_search` | request shape (semantic recall) |
| fact · decision · preference · todo | native memory files | `memory_search` | request shape (personal/durable facts) |

Because structured rows live in their **own DuckDB** and long documents live in their **own vector DB** — both physically separate from memory — **memory is never polluted** by table rows or document chunks, and each retrieval channel stays sharp at exactly what it's good at. The routing is declared in `AGENTS.md` (`## structured_data` + `## retrieval_router`) so the agent auto-detects intent every turn instead of guessing.

### Added
- **Structured-data store + registration (`procedures/data_register.py`).** Materializes a CSV, JSON, or Parquet file into a single DuckDB store (`kb/data_store/data.duckdb`) — **one table per dataset** (not a category column, not a database-per-category). DuckDB `DESCRIBE` infers column types; nested/semi-structured JSON is preserved as a queryable `STRUCT` (`meta.tier`, `meta.age` work directly). Every table is recorded in `catalog.json` with its schema, keywords, description, and a content hash. Supports `--list`, `--dry-run`, and `--refresh` (hash-skips unchanged sources, re-materializes changed ones, and prunes vanished sources incl. `DROP TABLE`).
- **Auto-profiled catalog (better routing + fewer bad queries).** At register/refresh, each column is profiled with pure SQL (no LLM): `distinct_count`, whether it's a low-cardinality **routing dimension** (`categorical`), and up to 12 `sample_values` for those dimensions. Unique integer keys and continuous numerics are correctly excluded from being "dimensions." **Keywords are auto-derived** from the table name + column names + word-like categorical values, so a natural-language request routes to the right table *even without hand-typed* `--keywords` (manual keywords are still merged first and preserved across refresh). The sampled values give the agent the actual value vocabulary to write correct `WHERE` clauses (it knows `region ∈ {US, EU, APAC}`) — directly reducing the bad-SQL failure mode.
- **Exact SQL retrieval (`tools/data_query.py`).** Read-only `SELECT` retrieval over the store: filter, range (`<`,`>`,`=`), sort, `COUNT`/`SUM`/`GROUP BY`, and cross-table/cross-format `JOIN`. `--list`/`--table` expose each table's columns + keywords so the agent routes to the right one; `--format text|json`; importable API (`data_query()`, `list_tables()`). `catalog.json` doubles as the intent router.
- **Two-layer read-only safety.** Queries are `SELECT`-only by construction: (1) a pre-execution guard strips comments, rejects multi-statement input (`SELECT 1; DROP TABLE …`), requires a leading `SELECT`/`WITH`, and blocks every write/DDL keyword (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`ATTACH`/`COPY`/`PRAGMA`/`INSTALL`/`LOAD`/…); (2) the DuckDB connection is opened `read_only=True`, so a write is rejected at the engine level even if the guard were ever bypassed. Bad SQL surfaces as a clean one-line `SQL error:` instead of a stack trace. The store is a retrieval surface, never a mutation path.
- **Fuzzy value routing (`data_query.py --suggest`).** Resolves an *inexact filter value* — typo, partial, wrong-case, or simple synonym/acronym — to a real stored value using DuckDB-native `jaro_winkler` + `levenshtein` + `ILIKE`-contains (plus an initialism boost), scored against the catalog's sampled column values. **No embeddings.** It *suggests* the correct `table.column = value` and an example `WHERE` clause — it never rewrites your SQL, so the agent stays in control. Directly attacks the "user typed the value slightly wrong" failure mode (`"united states"` → `region = 'US'`, `"Wiget"` → `product = 'Widget'`). Works on any Latin-script language (Indonesian included).
- **Per-prose-column semantic rank (`data_query.py --semantic`).** For a table with a free-**prose** column (survey feedback, reviews, support tickets), `data_register` auto-detects that column (text, *not* a low-cardinality dimension, avg length ≥ 25 chars) and builds a small per-column embedding index under `kb/data_store/text_index/<table>__<col>/` (reusing the same local `all-MiniLM-L6-v2`/Chroma stack as `docs_search`). `--semantic "<table>.<col>: <query>"` ranks rows by **meaning** and **joins the matched `rowid` back to the full structured row** — so "find rows about pricing complaints" flows straight into normal SQL filter/group. **Strictly opt-in per column and optional-dependency guarded**: if the embedding stack isn't installed, `--semantic` exits cleanly (code 3) and the exact-SQL path is entirely unaffected. This keeps the store *table-first with prose riding along*, not a second semantic store.
- **Behavioral routing + docs.** `AGENTS.md` gains `## structured_data` and a 5-way `## retrieval_router`; `TOOLS.md` gains `data_query`/`data_register` specs; README and `references/architecture.md` document the feature and the separate-stores model. `duckdb` is an **optional** dependency — if it's absent the feature is simply unavailable and the semantic/memory tools are completely unaffected.

### Fixed
- **Installer dependency-target correctness (buyer-install smoothness).** `install.sh` previously checked for modules with `python3 -c` but installed them with bare `pip` — on machines where `pip` and `python3` resolve to *different* interpreters, dependencies landed where the tools couldn't import them. All dependency installs now go through `"$PY" -m pip` (the same interpreter used for the import checks) with a PEP-668 `--break-system-packages` auto-retry for modern externally-managed distros. Applies to all dependencies, not just the new one.
- **`openclaw.plugin.json` version drift.** The manifest was stuck at `0.1.0` while `VERSION`/CHANGELOG had advanced; it now tracks the real version.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. No config changes required. To start using structured data: `python3 procedures/data_register.py --file <your.csv> --name <table> --keywords a,b,c`, then `python3 tools/data_query.py --sql "SELECT ..."`. Requires `duckdb` (the installer adds it; if it can't, only this feature is skipped).

---

## 1.4.0

Performance + install-correctness pass. **Recommended for everyone — especially larger memory corpora.** Makes the L2 graph build effectively instant, bounds L3 synthesis cost as memory grows, and fixes a first-install crash for the schema adapter.

### Fixed
- **Fresh installs could crash L2/L3 with `ModuleNotFoundError: _schema_adapter`.** `memory_graph.py` and `memory_synthesis.py` both import `_schema_adapter.py` (the schema-drift shim added in 1.3.5), but the installer's file-copy loop never shipped it — so a clean install left the two most important cron scripts unable to import. `graph_search.py` was missing from the same loop. Both are now copied by `install.sh`. Existing installs that already had the file are unaffected.

### Changed
- **L2 `build_graph` vectorized — ~461x faster, byte-identical output.** The former pure-Python O(n^2) double loop (cosine per pair) is replaced by a single BLAS `M @ M.T` cosine matrix + `lexsort` top-k selection. On the 3,017-chunk reference corpus this returns the exact same edge set (same pairs, same `sim`, same `rank_strength`) in ~0.57s vs ~265s. The full L2 run (including forward-refs gating below) drops from ~99s to ~1.5s of graph-build time. numpy is now a hard dependency (previously only transitive via pandas) and `install.sh` installs it explicitly.
- **`forward_refs` scan is now node-count gated (`--forward-refs-max-nodes`, default 1500).** The forward-reference report is O(terms x nodes) pure-Python fuzzy matching with no clean fast+exact form, so above the threshold it would dominate an otherwise-instant build for a secondary report. Below the threshold it runs the identical original algorithm (zero fidelity change); above it, the scan is skipped with a note and the graph is unaffected. `0` = always run, large value = never skip.
- **L3 synthesis cost is now bounded as the corpus grows.** Added `--cluster-batch N` (rotating `.cluster_cursor.json`, full sweep every `ceil(total/N)` runs) plus per-cluster dirty-skip (`.cluster_cache.json` content-hash; unchanged clusters skip the LLM, default on, cache auto-prunes to the live cluster set). `--cluster-batch 0` (default) preserves the exact legacy behavior — every cluster every run.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. No config changes required; new flags default to prior behavior.

---

## 1.3.5

Compatibility fix. **Recommended for anyone whose L2 graph / L3 synthesis crons error out.** Makes neuron tolerant of OpenClaw memory-index schema drift so `memory_graph.py` and `memory_synthesis.py` keep working across OpenClaw versions.

### Fixed
- **OpenClaw memory-index schema drift broke L2/L3.** `memory_graph.py` and `memory_synthesis.py` read the sqlite index OpenClaw's native memory search builds at `~/.openclaw/memory/<agent-id>.sqlite`. That schema is owned by OpenClaw, not dinomem, and some builds expose different table/column names (e.g. `memory_index_chunks` instead of `chunks`). The hard-coded `SELECT ... FROM chunks` then threw `no such table` / `no such column` and L2/L3 went dark while every other cron kept running. Both scripts now resolve the real table/column names at runtime via a new `_schema_adapter.py`, which introspects `sqlite_master` + `PRAGMA table_info` against a table of known aliases. Verified column-for-column identical to the previous SQL on the reference schema (files/chunks) with zero regression, and resilient to virtual FTS/vec tables. Unknown schemas now fail loud with a full schema dump for one-shot diagnosis instead of a cryptic sqlite error.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`.

---

## 1.3.4

Memory-quality + reliability pass. **Recommended for all users.** Sharpens the done-note lifecycle so retiring notes enrich normal memory instead of piling up frozen pins, adds an atomic note-claim writer to close the cron/live-session race, and biases the memory pruner to keep well-connected hub facts.

### Added
- **Done-note distiller.** When a verified `status: done` note is retired, its knowledge is now distilled into proper schema memory entries (via `extract_from_text`, seeded `verdict: valid`) BEFORE the note file is deleted — so the build stays retrievable and enters the normal retention review like everything else. Runs as a separate cheap no-reasoning subprocess; `resolve_done_notes.py` stays zero-LLM. Best-effort (a distill failure still deletes the note, same as before). Toggle with `DINOMEM_DISTILL_DONE_NOTES=0`.
- **Atomic note-claim writer (`scripts/claim_note.sh`).** Closes the one-sided claim handshake: the Advancer/Improver/Deleter crons already READ `claimed_by`/`claimed_at` and back off, but nothing WROTE a live-session claim — so a cron could grab a note mid-conversation and double-edit it. The live agent now stamps a claim before editing (atomic temp-file + `mv` + `flock`; `live-session-*` claims honored 120 min, others 30 min, future-dated treated as stale). Installer wires the `note_claim_protocol` into the managed `AGENTS.md`.
- **Memory connectivity keep-bias.** The neuron memory pruner now soft-rescues cross-file “hub” entries (facts referenced from multiple memory files) when they are only marginally-valid/uncertain — preventing well-connected context from being culled just because any single mention scored low.

### Changed
- **`promote: pin` hatch REMOVED from note retirement.** Retiring notes now ALWAYS distill → regular memory (freeze on merit), never copy the whole note into a `_pin_`. The hatch predated verdict-based retention (when distill-to-regular meant eventual decay, so pinning was the only way to keep anything); now regular memory freezes if it keeps earning `valid`, so the hatch only produced junk pins. Genuine pins (credentials, architecture, do-not-repeat findings) are authored DIRECTLY as `_pin_` files — never born as `_note_` then promoted — so removing it blocks nothing real and closes the primary junk-pin source. Deleter + Daily Note Review cron prompts updated to match.

### Upgrade
Unzip the new archive over your install and run `bash scripts/install.sh`. The installer refreshes the dinomem base first (dependency), then re-registers crons and redeploys the helper scripts idempotently — one step covers base + neuron.

---

## 1.3.3

Base-dependency upgrade. **Recommended for all users** — pulls in two dinomem base fixes that recover memory silently lost on manual `/new` / `/reset`. No neuron feature change; re-running the neuron installer refreshes the base automatically.

### Changed (via base refresh)
- **dinomem base 1.2.11 — silent permanent memory loss on manual `/new` / `/reset` FIXED.** OpenClaw core archives a manually-reset session in real time to `<session>.jsonl.reset.<ISOms>Z`, but the memory pipeline only globbed `*.archived.*.jsonl` and explicitly skipped any `.reset.` name — so **any session you `/new` or `/reset` had its transcript archived but never extracted into `memory/` (permanent loss).** Base now adopts core's reset-archives into the pipeline-visible namespace (new `adopt_core_reset_archives()` step) so `extract_memory` (and neuron `session_ingest`) pick them up. First run after upgrade **backfills** every historical stranded reset-archive.
- **dinomem base 1.2.12 — 0-delay extraction on manual reset.** New `dinomem-reset-extract` internal hook (`command:new` / `command:reset`) fires the memory pipeline the instant you `/new`, instead of waiting up to 15 min for the next cron tick.

### Upgrade
Re-run the neuron installer (`bash scripts/install.sh`). It refreshes the dinomem base first (git pull + base install), so both base fixes land automatically — no separate base step. The first base cron run after upgrade backfills any historical manual-reset sessions.

## 1.3.0

Self-improving project closer. **New feature. Recommended for all users.**

### Added
- **Self-improving done-project closer (two cooperating crons).** Completed project notes are now automatically improved and retired without cluttering the workspace:
  - **Project Improver (Component 2)** — LLM cron (`:20`/`:50`). Fires only on `status: done` project notes not yet stamped `verified: true`. Domain-agnostic and behavior-preserving: it assesses whether the project can be made cheaper / faster / clearer / more reliable, makes only that improvement (no prescribed checklist — "nothing needed" is a valid outcome), validates the artifact still satisfies its `done_when`, then stamps `verified: true`. A zero-LLM pre-gate (`scripts/check_improvable.sh`) skips the LLM entirely when there is nothing to improve. A hard safety gate blocks external/destructive steps (git push, sends, spend, credentials) unattended — it does the local work, then pauses for owner approval.
  - **Verified Note Deleter (Component 1)** — deterministic janitor cron (`:25`/`:55`), a thin wrapper over `scripts/resolve_done_notes.py`. Deletes ONLY notes carrying `verified: true`, snapshotting each first and promoting any `promote: pin` note to `_pin_`. Non-blocking `flock` so it never races the Advancer/Improver.
- **Shared claim protocol across all note jobs.** Advancer, Improver, and Deleter honor `claimed_by` / `claimed_at` (UTC ISO8601): live-session claims are respected for 120 min (2h), other-claimant claims for 30 min; stale and future-dated claims are cleared. This prevents concurrent `_note_*.md` mutation — including a cron waking mid-conversation and double-editing a note a human is actively working.

### Upgrade
Unzip the new archive over your install and run `scripts/install.sh --force`. The two new crons register idempotently.

---

## 1.2.5

Memory reliability fixes. **Recommended for all users.**

### Fixed
- **MEMORY.md bloat on heavy work-days (`generate_topic_index.py`).** The "Previous Session" keyword line was built from the slug path with no per-item cap — a day with many dated memory files produced hundreds of slugs joined into one multi-KB line that the size guard couldn't trim (it trims whole lines, not within a line). Result: MEMORY.md could balloon well past the bootstrap char cap and get silently truncated on injection. Fix A: cap the slug path (15 items / 600 chars), symmetric with the already-capped LLM/word-freq paths. Fix B: `_guard_memory_size` now also collapses any single over-long line as a safety net, not just the `## Searchable` section.
- **Silent extraction failure on multi-Node hosts / permanent loss on transient LLM failure** (shared with dinomem base). Node resolved at runtime (>= 22.19), failed extractions retry instead of being marked processed, tolerant JSON parsing of gateway stdout. Ensure dinomem base is also updated (>= 1.2.9).

### Upgrade
Unzip the new archive over your install and run `scripts/install.sh --force`. Also update dinomem base to >= 1.2.9.

---

## Unreleased

### Removed
- **Calendar integration fully retired.** `calendar_sync.py`, `calendar_tool.py`, and `calendar_oauth_setup.py` deleted; calendar cron entry removed; AGENTS.md calendar block removed; all calendar references removed from docs.

---

## 1.2.4 — 2026-06-29

### Added
- **`investigate_before_act` policy block (injected into AGENTS.md).** Codifies
  the verify-before-mutate discipline: inspect the real artifact, reproduce
  locally before fixing, re-verify with evidence after fixing, check cross-entity
  claims against the target directly (shared lineage != shared bug), and treat a
  prior "it's fixed" claim as non-evidence. Sits beside `project_note.estimate`
  as the pre-build/pre-fix gate. Base (memory-only) is intentionally left lean.

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
