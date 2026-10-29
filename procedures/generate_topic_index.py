#!/usr/bin/env python3
"""
Topic Index Generator — Derives topics/INDEX.md from memory/*.md files

Reads all markdown files in the workspace memory/ directory, groups them
into topics, and generates a compact topic index. The index is the single
source of truth for what the agent "knows" — if it's not in the index,
it's not in memory either.

Called by:
  - cleanup_old_data.py (after removing old files)

Structure:
  ## Persistent — Behavioral Rules (NOT in memory, NOT searchable)
  ## Searchable — Active (retrieved via memory_search / memory_get)
  ## Archived — Deleted from memory (NOT searchable, for continuity only)

Usage:
  python3 procedures/generate_topic_index.py [--dry-run]
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

# Paths
WORKSPACE_ROOT = Path(__file__).parent.parent
TOPICS_DIR = WORKSPACE_ROOT / "topics"
MEMORY_DIR = WORKSPACE_ROOT / "memory"
PERSISTENT_FILE = TOPICS_DIR / "PERSISTENT.md"
INDEX_FILE = TOPICS_DIR / "INDEX.md"
MEMORY_FILE = WORKSPACE_ROOT / "MEMORY.md"
ARCHIVE_TRACKER = TOPICS_DIR / ".archive_tracker.json"
HASH_FILE = TOPICS_DIR / ".last_index_hash.json"
LOG_FILE = WORKSPACE_ROOT / "logs" / "topic_index.log"

# Config
MAX_ARCHIVE_ENTRIES = 15
ARCHIVE_MAX_AGE_DAYS = 30
ACTIVE_CUTOFF_DAYS = 60
LLM_MAX_TOKENS = 4000


def _resolve_max_index_chars(default_cap: int = 20000, floor: int = 18000) -> int:
    """Char-level ceiling for the MEMORY.md this generator writes, tracking the
    LIVE bootstrap per-file cap instead of a hardcode. This generator only capped
    by ENTRY COUNT (50 max) — verbose entries could still blow past the bootstrap
    char cap and get silently truncated on injection. Read agents.defaults.
    bootstrapMaxChars from openclaw.json (same config-read idiom as the base
    memory_cleanup.py / extract_memory.py routing) and trim at 90% of it, with a
    18000 floor so we never trim more aggressively than the base default behavior.
    """
    try:
        cfg_path = OPENCLAW_ROOT / "openclaw.json"
        if not cfg_path.exists():
            return floor
        with open(cfg_path, "r", encoding="utf-8") as f:
            defaults = json.load(f).get("agents", {}).get("defaults", {})
        cap = defaults.get("bootstrapMaxChars", default_cap)
        if not isinstance(cap, (int, float)) or cap <= 0:
            cap = default_cap
        return max(floor, int(cap * 0.9))
    except Exception:
        return floor


def _guard_memory_size(memory_content: str) -> str:
    """If the assembled MEMORY.md exceeds the live char cap, trim oldest entries
    from the Searchable section only (never persistent rules, open projects, or
    recency) so the index stays under the bootstrap injection limit. Trims the
    LAST searchable lines (oldest, since entries are appended chronologically),
    matching base memory_cleanup.py's trim-oldest behavior. No-op when under cap.
    """
    limit = _resolve_max_index_chars()
    if len(memory_content) <= limit:
        return memory_content
    # Safety net (Fix B): before trimming Searchable, collapse any single
    # over-long line anywhere in the content. The Previous Session keyword blob
    # lives ABOVE ## Searchable, so line-trimming Searchable alone can't shrink
    # it. Any line longer than _MAX_SINGLE_LINE_CHARS is truncated in place.
    _MAX_SINGLE_LINE_CHARS = 800
    if any(len(ln) > _MAX_SINGLE_LINE_CHARS for ln in memory_content.splitlines()):
        _fixed = []
        for ln in memory_content.splitlines():
            if len(ln) > _MAX_SINGLE_LINE_CHARS:
                ln = ln[:_MAX_SINGLE_LINE_CHARS].rsplit(' ', 1)[0] + ' …'
            _fixed.append(ln)
        memory_content = "\n".join(_fixed) + ("\n" if memory_content.endswith("\n") else "")
        log(f"⚠️  MEMORY.md had an over-long line (> {_MAX_SINGLE_LINE_CHARS} chars) — collapsed in place.")
        if len(memory_content) <= limit:
            return memory_content
    marker = "## Searchable"
    idx = memory_content.find(marker)
    if idx == -1:
        # No searchable section to trim — leave content, just warn in log.
        log(f"⚠️  MEMORY.md is {len(memory_content)} chars (> {limit} cap) but has no Searchable section to trim.")
        return memory_content
    head = memory_content[:idx]
    tail = memory_content[idx:]
    tail_lines = tail.splitlines(keepends=True)
    # Keep trimming oldest (last) searchable entry lines until under cap or only the header remains.
    removed = 0
    while len(head) + len("".join(tail_lines)) > limit and len(tail_lines) > 1:
        tail_lines.pop()
        removed += 1
    result = head + "".join(tail_lines).rstrip() + "\n"
    log(f"⚠️  MEMORY.md exceeded {limit} char cap — trimmed {removed} oldest searchable line(s) to {len(result)} chars.")
    return result

# Sessions dir for recency section
OPENCLAW_ROOT = WORKSPACE_ROOT.parent
AGENT_ID = WORKSPACE_ROOT.name.replace('workspace-', '')
SESSIONS_DIR = OPENCLAW_ROOT / 'agents' / AGENT_ID / 'sessions'


def log(message):
    """Write to log file with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}"
    print(log_message)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_message + "\n")


def _load_cheap_model_module():
    """Import procedures/_cheap_model.py by path. None if unavailable.

    By path (not `import`) because procedures/ is not a package on an installed
    workspace. Shared by the two readers below so there is ONE loader.
    """
    try:
        import importlib.util as _ilu
        _hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cheap_model.py")
        _spec = _ilu.spec_from_file_location("_cheap_model", _hp)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod
    except Exception:
        return None


def _openclaw_config_path() -> Path:
    """Path of the openclaw.json THIS agent runs on.

    BUG FIXED 2026-10-29: this read ~/.openclaw/openclaw.json unconditionally and
    ignored OPENCLAW_CONFIG_PATH. Every dinomem cron is launched with
    OPENCLAW_CONFIG_PATH=/root/.openclaw-<agent>/openclaw.json, so on a
    multi-instance host this read the WRONG instance -- one whose providers carry
    no apiKey. get_openclaw_config() then returned (model, None, None), the
    direct-API fallback was skipped, and the run silently degraded to keyword
    extraction. Measured on a 12-agent box: the read file exposed providers
    ['tei-embed'] while the agent's real config had the LLM keys.

    Delegates to _cheap_model.config_path() -- the resolver that already fixed
    this exact class of bug on 2026-10-04 -- so there is ONE precedence ladder,
    not a second copy that can drift. Inline fallback keeps this working if the
    helper is ever missing.
    """
    _cm = _load_cheap_model_module()
    if _cm is not None and hasattr(_cm, "config_path"):
        try:
            return Path(_cm.config_path())
        except Exception:
            pass
    for _var in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG"):
        _env = os.environ.get(_var, "").strip()
        if _env:
            return Path(_env)
    return Path.home() / ".openclaw" / "openclaw.json"


def get_openclaw_config():
    """Get model and API config from OpenClaw."""
    openclaw_config = _openclaw_config_path()
    if not openclaw_config.exists():
        return None, None, None

    try:
        with open(openclaw_config, 'r') as f:
            config = json.load(f)

        providers = config.get('models', {}).get('providers', {})

        # Prefer the CHEAP model: this is a no-reasoning summarization task, and
        # the cheap tier is what the gateway path already uses. Falling straight
        # to primary made the fallback try an expensive reasoning model it often
        # had no key for.
        model = NO_REASONING_MODEL or (
            config.get('agents', {}).get('defaults', {}).get('model', {}).get('primary')
        )

        api_key = None
        api_base = None

        # Resolve the provider GENERICALLY from the "<provider>/<model>" prefix.
        #
        # BUG FIXED 2026-10-29: this used a hardcoded if/elif over kimi/google/
        # openrouter, and its last branch claimed ANY id containing "/" for
        # openrouter. So "anthropic/claude-sonnet-5" resolved to the openrouter
        # provider, which does not exist on this host -> apiKey None -> the
        # direct-API fallback was skipped entirely. Measured: the agent's real
        # providers were ['tei-embed','google','ninerouter-<agent>'], none of
        # which the old ladder could ever reach. A provider id is data, not a
        # fixed list, so match it as data and keep the legacy aliases only as a
        # fallback for configs that predate explicit prefixes.
        if model:
            prefix = model.split('/', 1)[0] if '/' in model else ''
            provider = providers.get(prefix) if prefix else None
            if not provider:
                for _alias, _ids in (
                    (('kimi',), ('kimi-coding', 'kimi-code')),
                    (('gemini', 'google'), ('google',)),
                    (('openrouter',), ('openrouter',)),
                ):
                    if any(a in model for a in _alias):
                        for _pid in _ids:
                            if providers.get(_pid):
                                provider = providers[_pid]
                                break
                    if provider:
                        break
            if provider:
                api_key = provider.get('apiKey')
                api_base = provider.get('baseUrl')

        # A provider with a key but no baseUrl cannot be called over plain HTTP
        # here; report it as unusable rather than returning a half-config the
        # caller would fail on later.
        if not api_base:
            api_key = None

        return model, api_key, api_base

    except Exception as e:
        log(f"Error reading OpenClaw config: {e}")
        return None, None, None


# Topic-index generation is a no-reasoning summarization task — run it on the
# cheap tier via the standard DINOMEM_CHEAP_MODEL tag (matches the rest of
# dinomem); legacy DINOMEM_NO_REASONING_MODEL kept as back-compat fallback.
# Unset = OpenClaw default (default-safe — no hardcoded gemini that 404s on
# buyers without that model).
def _resolve_no_reasoning_model():
    """Cheap model via the single compaction anchor (procedures/_cheap_model.py):
    DINOMEM_CHEAP_MODEL env -> agents.defaults.compaction.model -> legacy env.
    Falls back to raw env if the helper is unavailable (portable ship-safe)."""
    try:
        import importlib.util as _ilu
        _hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cheap_model.py")
        _spec = _ilu.spec_from_file_location("_cheap_model", _hp)
        _cm = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_cm)
        return _cm.cheap_model() or None
    except Exception:
        return (
            os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
            or os.environ.get("DINOMEM_NO_REASONING_MODEL", "").strip()
            or None
        )

NO_REASONING_MODEL = _resolve_no_reasoning_model()

def call_llm_gateway(prompt, max_tokens=2000):
    """Call LLM via OpenClaw gateway (cheap no-reasoning model)."""
    import subprocess, shutil

    # Resolve the openclaw binary portably: PATH first, then common install
    # locations. Hardcoding the linuxbrew path breaks on npm-global installs and
    # other distros where openclaw lives elsewhere.
    openclaw_bin = shutil.which("openclaw")
    if not openclaw_bin:
        for _c in ("/home/linuxbrew/.linuxbrew/bin/openclaw",
                   "/usr/local/bin/openclaw", "/usr/bin/openclaw"):
            if Path(_c).exists():
                openclaw_bin = _c
                break
    openclaw_bin = openclaw_bin or "openclaw"

    try:
        _cmd = [openclaw_bin, "capability", "model", "run",
                "--prompt", prompt,
                "--gateway",
                "--json"]
        if NO_REASONING_MODEL:
            _cmd += ["--model", NO_REASONING_MODEL]
        result = subprocess.run(
            _cmd,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            output = json.loads(result.stdout)
            if output.get("ok") and output.get("outputs"):
                text = output["outputs"][0].get("text", "")
                if text:
                    return True, text
    except Exception as e:
        log(f"Gateway error: {e}")

    return False, ""


def call_llm_direct(model, api_key, api_base, prompt, max_tokens=2000):
    """Direct API call fallback."""
    import urllib.request

    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1
        }
        endpoint = f"{api_base.rstrip('/')}/chat/completions"

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(data).encode('utf-8'),
            headers=headers,
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            content = result['choices'][0]['message']['content']
            return True, content

    except Exception as e:
        log(f"Direct API error: {e}")
        return False, ""


def generate_topics_with_llm(documents_summary: str) -> str:
    """Use LLM to generate a machine-facing memory index."""
    prompt = f"""You are generating a MACHINE INDEX for an AI agent's memory system.
This index is auto-injected into the agent's context at EVERY session start.
Its sole purpose: help the agent QUICKLY DECIDE which memory_search queries to run.

The agent scans this index in <1 second. It does NOT read prose. It pattern-matches keywords.

LEANNESS IS THE GOAL. This whole index reloads into context on EVERY turn, so its total length IS
its cost — shorter is strictly better. The 50-entry cap is a hard ceiling, NOT a target: produce as
FEW entries as the memory genuinely warrants. Fewer high-signal lines beat more marginal ones every
single turn. Never pad toward 50. If only 25 entries clear the transferability bar, output 25.

OUTPUT FORMAT (one entry per line, flat — no nesting, no bullets):
[DOMAIN] [AGE] TOPIC_NAME | tag1, tag2, tag3, tag4 | brief-description

DOMAIN must be one of: WORKFLOW | DOMAIN | PROJECT | PREFERENCE | REFERENCE
  (DOMAIN = this agent's own subject matter, whatever that is — sales pipeline,
   support cases, events, markets, content. Infer it from the memory itself; do
   NOT assume any particular industry.)
AGE: use "[Xd]" for days ago, "[persist]" for behavioral rules
TOPIC_NAME: 3-5 words, specific — not vague
TAGS: 4-6 comma-separated keywords including proper nouns, identifiers, project names, exact terms the user might use
BRIEF-DESCRIPTION: max 10 words, only if the tags don't make it obvious

RULES:
1. FLAT LIST — one entry per line. NO indentation, NO subtopics, NO hierarchy
2. KEYWORD-DENSE — every word in tags should be something a user might search for
3. IDENTIFIERS FIRST — if a topic names a specific entity (a person, client, product,
   project, account, ticker, case id), include that identifier verbatim as a tag
4. NO FLUFF — no "discussions about", no "various topics", no "general insights"
5. DISPLAY ORDER — newest first (largest Xd first), then [persist]. This is ORDERING only,
   NOT how you choose what to drop — see SELECTION below.
6. MAX 50 entries (this is a CEILING, not a quota — only fill a slot if the entry is genuinely
   transferable; do not pad to 50). When more than 50 exist, drop by SELECTION — never drop purely by age.
7. GROUP BY DOMAIN only if it helps scanning; otherwise sort by recency
8. For domain content: include the specific identifiers, triggers, patterns and
   decision signals that this agent's work actually turns on

SELECTION — which entries earn one of the 50 slots. Importance = TRANSFERABILITY (is there a
reusable lesson here?), NOT outcome (win/lose) and NOT recency:
- KEEP (high value): reusable lessons + structural knowledge — patterns/mechanisms that apply
  beyond a single asset, validated frameworks, behavioral rules, user preferences, AND mistakes
  that abstract into a lesson. A wrong or losing thesis that taught something ranks HIGH:
  win/lose is irrelevant, the transferable lesson IS the value.
- DROP FIRST (low value): one-off context with no reusable takeaway — single-event datapoints,
  transient prices/metrics tied to a date, expired or unresolved one-time predictions, and pure
  recitation that adds no new fact.
- TIEBREAK ONLY: if two entries are equally transferable, keep the more recent one.
- NEVER drop a [persist] behavioral rule or user preference — those are always pinned.

DOCUMENTS:
{documents_summary}

OUTPUT ONLY the flat index entries, one per line, no preamble, no markdown headers."""

    # Try gateway first
    success, result = call_llm_gateway(prompt, max_tokens=LLM_MAX_TOKENS)
    if success:
        return result

    # Fallback to direct API
    model, api_key, api_base = get_openclaw_config()
    if model and api_key and api_base:
        success, result = call_llm_direct(model, api_key, api_base, prompt, max_tokens=LLM_MAX_TOKENS)
        if success:
            return result

    log("⚠️ LLM failed, using fallback topic extraction")
    return _fallback_topic_extraction(documents_summary)


def _fallback_topic_extraction(documents_summary: str) -> str:
    """Regex-based fallback that produces machine-facing flat index entries."""
    lines = documents_summary.strip().split('\n')
    topics = []
    seen = set()

    for line in lines:
        line = line.strip()
        if not line or line.startswith('-'):
            continue

        # Extract date from filename for age
        age = "recent"
        # DOMAIN = subject-matter catch-all (see the classifier note below). An
        # unmatched line is domain content by default, not a stray REFERENCE.
        domain = "DOMAIN"
        filename = ""
        if 'File:' in line:
            parts = line.split('File:')
            if len(parts) > 1:
                filename = parts[1].strip().split()[0]
                name = filename.replace('memory_', '').replace('.md', '')
                try:
                    file_date = datetime.strptime(name, '%Y-%m-%d')
                    days_ago = (datetime.now() - file_date).days
                    age = f"{days_ago}d"
                except ValueError:
                    age = "persist"

                # Determine domain. This is the NON-LLM fallback path (used when
                # the LLM call fails), so it can only pattern-match.
                #
                # TWO RULES, both learned the hard way on 2026-10-29:
                #
                # 1. STAY VERTICAL-NEUTRAL. A crypto/market word list here
                #    silently mislabels every non-market agent on the box:
                #    'price'/'trade'/'consensus' tagged 50/50 of a consulting
                #    agent's rows as MARKET.
                #
                # 2. NEVER MATCH THE WHOLE SUMMARY LINE. Each line is
                #    "File: x.md | Source: daily | Age: .. | Size: .. | Preview: ..",
                #    so generic words collide with the METADATA, not the content:
                #    'source' hit 461/853 lines and 'user' 171, which collapsed an
                #    entire index into one bucket. Match the FILENAME and the
                #    PREVIEW only.
                #
                # The filename is the strongest signal available: dinomem encodes
                # the memory TYPE as <date>_<type>_<slug>.md, which is structural
                # and domain-independent -- far safer than guessing from prose.
                preview = line.split('Preview', 1)[1].lower() if 'Preview' in line else ''
                fname_l = filename.lower()
                if '_preference_' in fname_l:
                    domain = "PREFERENCE"
                elif '_source_score_' in fname_l:
                    domain = "REFERENCE"
                elif 'project' in fname_l:
                    domain = "PROJECT"
                elif any(w in preview for w in ['workflow', 'sop', 'procedure', 'checklist']):
                    domain = "WORKFLOW"
                elif any(w in preview for w in ['prefers ', 'preference', 'communication style', 'tone of voice']):
                    domain = "PREFERENCE"
                # else -> DOMAIN (initialised above): the subject-matter catch-all,
                # so an unmatched line lands in a correct generic bucket rather
                # than a borrowed one.

                # Extract tagged content for keywords
                tags = []
                for tag in ['[factual]', '[pattern]', '[prediction]', '[uncertain]', '[preference]', '[decision]', '[source_scores]']:
                    idx = line.find(tag)
                    if idx != -1:
                        after_tag = line[idx + len(tag):].strip()
                        after_tag = after_tag.split('|')[0].split('...')[0].strip()
                        after_tag = after_tag.strip('[]').strip()
                        if after_tag and len(after_tag) > 5:
                            # Extract key noun phrases (naive: first 4-5 words)
                            words = after_tag.split()[:6]
                            tags.append('-'.join(words).lower())

                # Build topic name from filename or first tag
                topic_name = tags[0].replace('-', ' ').title() if tags else f"Session {name}"
                tag_str = ', '.join(tags[:4]) if tags else name

                topic_key = f"{domain}:{topic_name[:30]}"
                if topic_key not in seen:
                    seen.add(topic_key)
                    topics.append(f"[{domain}] [{age}] {topic_name} | {tag_str} | {name}")

    if not topics:
        topics.append("[REFERENCE] [persist] General Knowledge | memory-system, agent-config | Various reference topics")

    # Sort by recency (newest first), then persistent
    def sort_key(t):
        age_part = t.split(']')[1].split(']')[0].strip() if ']' in t else "999d"
        if age_part == "persist":
            return (0, t)  # persistent first
        try:
            days = int(age_part.replace('d', ''))
            return (-days, t)  # newest first (largest negative)
        except ValueError:
            return (-999, t)

    topics.sort(key=sort_key)
    return '\n'.join(topics[:50])


def load_persistent_topics() -> str:
    """Load manually curated persistent topics."""
    if PERSISTENT_FILE.exists():
        return PERSISTENT_FILE.read_text(encoding='utf-8').strip()

    # Default persistent topics
    default = """## Persistent (never removed)
[PREFERENCE] [persist] Owner Communication Style | indonesian, concise, max-2000-chars | Language and length preferences
[WORKFLOW] [persist] No TA Rule | no-indicators, no-sma, no-ema, no-rsi, no-macd, no-fibonacci | Strip all technical indicators from responses
[PREFERENCE] [persist] Response Style | data-driven, strongest-number-first, evidence-required | Every claim needs a data point attached
"""
    PERSISTENT_FILE.write_text(default, encoding='utf-8')
    return default.strip()


def load_archive_tracker() -> List[Dict]:
    """Load archive tracker with timestamps."""
    if ARCHIVE_TRACKER.exists():
        try:
            data = json.loads(ARCHIVE_TRACKER.read_text(encoding='utf-8'))
            return data.get('archived', [])
        except Exception:
            pass
    return []


def save_archive_tracker(archived: List[Dict]):
    """Save archive tracker."""
    ARCHIVE_TRACKER.write_text(json.dumps({'archived': archived}, indent=2), encoding='utf-8')


def prune_archive(archived: List[Dict]) -> List[Dict]:
    """Remove old archive entries."""
    cutoff = datetime.now() - timedelta(days=ARCHIVE_MAX_AGE_DAYS)
    filtered = []

    for entry in archived:
        removed_date = datetime.fromisoformat(entry.get('removed', '2000-01-01'))
        if removed_date > cutoff:
            filtered.append(entry)

    # Also cap at max entries, remove oldest
    if len(filtered) > MAX_ARCHIVE_ENTRIES:
        filtered = sorted(filtered, key=lambda x: x.get('removed', ''), reverse=True)
        filtered = filtered[:MAX_ARCHIVE_ENTRIES]

    return filtered


# Hard ceiling on the summary we hand to the LLM.
#
# WHY A CEILING AT ALL (measured 2026-10-29, not theoretical): the prompt goes to
# `openclaw infer model run --prompt <text>` as ONE argv entry, and Linux caps a
# single argv string at MAX_ARG_STRLEN = 32 pages = 131072 bytes. That is a
# SEPARATE limit from total ARG_MAX (2 MB on the reference box), so no box tuning
# avoids it. A real workspace produced a 510,672 char summary -- 3.9x over -- so
# every run died with errno 7 and silently fell back to keyword extraction:
# 14 of 16 runs on one agent, 16 of 20 on another, going back weeks. The
# "fallback" was effectively the PRIMARY path.
#
# Checked upstream too: OpenClaw 2026.7.1 AND the latest 2026.10.8 both declare
# requiredOption("--prompt <text>") with no stdin / --prompt-file alternative, so
# upgrading does NOT fix this. Bounding the input is the half we control.
#
# 100_000 leaves ~31 KB of headroom under the ceiling for the surrounding
# instruction text, far more than that text can plausibly grow. Trimming also
# cuts cost directly: 510 KB is ~127k tokens per run, per agent, every week.
MAX_SUMMARY_CHARS = 100_000


def _budget_summary(parts: list, limit: int = MAX_SUMMARY_CHARS) -> list:
    """Trim summary lines to `limit` chars, dropping the OLDEST first.

    Order matters: `parts` arrives sorted by filename, and dinomem names memory
    <date>_<type>_<slug>.md, so filename order is chronological. Keeping the
    TAIL keeps the most recent memory, which is what the index is for. Returns
    the kept lines in their original order.
    """
    kept: list = []
    used = 0
    for line in reversed(parts):
        cost = len(line) + 1  # +1 for the newline the join adds
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept


def build_documents_summary() -> str:
    """Build a text summary of all documents in the memory/ directory."""
    try:
        if not MEMORY_DIR.exists():
            log(f"⚠️  Memory directory not found: {MEMORY_DIR}")
            return ""

        md_files = sorted(MEMORY_DIR.glob("*.md"))
        if not md_files:
            log("No .md files found in memory/")
            return ""

        summary_parts = []
        for md_file in md_files:
            # Skip hidden files
            if md_file.name.startswith('.'):
                continue

            try:
                content = md_file.read_text(encoding='utf-8').strip()
                if not content:
                    continue

                # Parse file date for recency-weighted preview
                file_date = None
                age_days = 999
                try:
                    # Extract YYYY-MM-DD from filename
                    name_part = md_file.stem  # e.g. "2026-05-29"
                    file_date = datetime.strptime(name_part, '%Y-%m-%d')
                    age_days = (datetime.now() - file_date).days
                except (ValueError, TypeError):
                    pass

                # Recency-weighted preview: newer files get more signal
                if age_days <= 30:
                    preview_chars = 800
                elif age_days <= 90:
                    preview_chars = 500
                else:
                    preview_chars = 300

                preview = content[:preview_chars].replace('\n', ' ')
                if len(content) > preview_chars:
                    preview += "..."

                # Determine source from filename
                # memory/YYYY-MM-DD.md → source = daily
                # memory/MEMORY.md → source = global
                if md_file.name == "MEMORY.md":
                    source = "global"
                else:
                    source = "daily"

                # Estimate chunk count (OpenClaw chunks ~400 tokens)
                # Rough estimate: 1 chunk per 3000 chars
                est_chunks = max(1, len(content) // 3000)

                # Extract key tags for better topic generation
                # Find first [factual], [preference], [decision] lines
                tag_lines = []
                for tag in ['[factual]', '[pattern]', '[prediction]', '[uncertain]', '[preference]', '[decision]', '[source_scores]']:
                    idx = content.find(tag)
                    if idx != -1:
                        end = content.find('\n', idx)
                        if end == -1:
                            end = idx + 200
                        tag_lines.append(content[idx:end].strip())
                
                tags_preview = ' | '.join(tag_lines[:3]) if tag_lines else preview[:200]

                summary_parts.append(
                    f"File: {md_file.name} | Source: {source} | Age: {age_days}d | "
                    f"Size: {len(content)} chars | Chunks: ~{est_chunks} | Preview({preview_chars}): {preview[:250]}... | "
                    f"Tags: {tags_preview[:200]}..."
                )
            except Exception as e:
                log(f"Error reading {md_file.name}: {e}")
                continue

        total_files = len(summary_parts)
        summary_parts = _budget_summary(summary_parts)
        if len(summary_parts) < total_files:
            log(
                f"Built summary from {len(summary_parts)} of {total_files} memory "
                f"file(s) — trimmed to the {MAX_SUMMARY_CHARS} char prompt budget "
                f"(oldest dropped first)"
            )
        else:
            log(f"Built summary from {total_files} memory file(s)")
        return '\n'.join(summary_parts)

    except Exception as e:
        log(f"Error building document summary: {e}")
        import traceback
        log(traceback.format_exc())
        return ""


# Bump when the GENERATED OUTPUT's taxonomy/classifier changes, so an upgraded
# installer forces one regeneration even on a box whose memory/*.md are byte
# identical. WHY THIS IS LOAD-BEARING: the skip-if-unchanged gate below hashes
# memory CONTENT only, so without this key an existing install that upgrades the
# generator keeps its stale index FOREVER -- the inputs never changed, so the
# generator never reruns, so the old labels never get fixed.
#   v2 = 2026-10-29 de-verticalization (MARKET -> DOMAIN, crypto branch removed)
#   v3 = 2026-10-29 classifier fix. v2 matched the WHOLE summary line, which
#        includes metadata ("| Source: daily | Age: ..."), so the generic words
#        'source' and 'user' hit 461/853 and 171/853 lines on a real workspace
#        and collapsed an entire index into one bucket. v3 matches the filename
#        and preview only. Anyone who installed v2 needs this regeneration.
INDEX_SCHEMA_VERSION = "3"


def _compute_file_hashes() -> dict:
    """Compute SHA-256 hashes of all memory/*.md files for change detection.

    Also carries INDEX_SCHEMA_VERSION so a generator upgrade invalidates the
    cache the same way a memory edit does (see the constant's note above).
    """
    import hashlib
    hashes = {"__schema__": INDEX_SCHEMA_VERSION}
    if not MEMORY_DIR.exists():
        return hashes
    for md_file in sorted(MEMORY_DIR.glob("*.md")):
        if md_file.name.startswith('.'):
            continue
        try:
            content = md_file.read_bytes()
            hashes[str(md_file.relative_to(WORKSPACE_ROOT))] = hashlib.sha256(content).hexdigest()[:16]
        except Exception:
            continue
    return hashes


def _load_previous_hashes() -> dict:
    """Load stored hashes from last successful run."""
    if not HASH_FILE.exists():
        return {}
    try:
        with open(HASH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hashes(hashes: dict):
    """Save current hashes for next run's comparison."""
    try:
        HASH_FILE.write_text(json.dumps(hashes, indent=2, sort_keys=True), encoding='utf-8')
    except Exception as e:
        log(f"⚠️  Error saving hash file: {e}")


def generate_index(dry_run=False) -> bool:
    """Main function: generate topics/INDEX.md from memory/*.md files."""
    log("=" * 50)
    log("Topic Index Generation")

    # Ensure directories exist
    TOPICS_DIR.mkdir(exist_ok=True)
    MEMORY_DIR.mkdir(exist_ok=True)

    # Skip regeneration if no files changed since last run
    if not dry_run:
        current_hashes = _compute_file_hashes()
        previous_hashes = _load_previous_hashes()
        if current_hashes == previous_hashes:
            log("📋 No changes detected in memory/*.md — skipping regeneration")
            return True
        _save_hashes(current_hashes)

    # Build document summary from memory/*.md files
    log("Building document summary from memory/ directory...")
    summary = build_documents_summary()

    if not summary:
        log("No documents found in memory/")
        # Still write index with just persistent topics
        persistent = load_persistent_topics()
        open_proj = get_open_projects_section()
        open_proj_block = f'{open_proj}\n\n' if open_proj else ''
        recency_section = get_previous_session_topics()
        recency_block = f'\n{recency_section}\n' if recency_section else ''
        index_content = f"{open_proj_block}{persistent}\n{recency_block}\n## Searchable — Active (retrieved via memory_search / memory_get)\n_No documents in memory._\n\n## Archived — Deleted from memory (not searchable)\n_None._\n"
        if not dry_run:
            INDEX_FILE.write_text(index_content, encoding='utf-8')
            memory_content = _guard_memory_size(f"MEMORY.md\n\n{index_content}")
            MEMORY_FILE.write_text(memory_content, encoding='utf-8')
        log("Wrote minimal index (persistent topics only)")
        return True

    # Generate topics with LLM
    doc_count = len(summary.split('\n')) if summary else 0
    log(f"Generating topics from {doc_count} document summaries...")
    topics_text = generate_topics_with_llm(summary)

    # Load persistent topics
    persistent = load_persistent_topics()

    # Open projects (in_progress notes) — injected at top so cold-start sees them
    open_proj = get_open_projects_section()
    open_proj_block = f'{open_proj}\n\n' if open_proj else ''

    # Get recent flush context (structured) + previous session keyword hint
    flush_context = get_recent_flush_context()
    recency_section = get_previous_session_topics()
    recency_block = ''
    if flush_context:
        recency_block += f'\n{flush_context}\n'
    if recency_section:
        recency_block += f'\n{recency_section}\n'

    # Assemble final index
    index_content = f"""{open_proj_block}{persistent}
{recency_block}
## Searchable — Active (retrieved via memory_search / memory_get)
{topics_text}

---
_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Source: memory/*.md_
"""

    if dry_run:
        log("DRY RUN — would write:")
        log(index_content[:500] + "...")
        return True

    # Write index
    INDEX_FILE.write_text(index_content, encoding='utf-8')
    log(f"✅ Wrote {INDEX_FILE}")

    # Write MEMORY.md (auto-injected root file) — same content with title
    memory_content = _guard_memory_size(f"MEMORY.md\n\n{index_content}")
    MEMORY_FILE.write_text(memory_content, encoding='utf-8')
    log(f"✅ Wrote {MEMORY_FILE} (auto-injected)")

    # Estimate token count
    token_estimate = len(index_content.split()) * 1.3  # rough estimate
    log(f"📊 Estimated size: ~{int(token_estimate)} tokens")

    return True


def get_open_projects_section() -> Optional[str]:
    """
    Scan memory/_note_*.md for status: in_progress notes (project executor +
    paused multi-step tasks) and build an 'Open Projects' block injected at the
    TOP of MEMORY.md, so a cold-start session sees unfinished work in its
    auto-injected context instead of relying on a voluntary glob.

    Emits nothing when there are no in_progress notes (no-op for pure task_bound
    setups). This is the mechanism behind fix #1: convert 'agent must remember to
    look' into 'it is already on screen'.
    """
    try:
        if not MEMORY_DIR.exists():
            return None
        rows = []
        for note in sorted(MEMORY_DIR.glob('_note_*.md')):
            try:
                text = note.read_text(encoding='utf-8')
            except Exception:
                continue
            m = re.search(r'^status:\s*(.+)$', text, re.MULTILINE)
            if not m or m.group(1).strip() != 'in_progress':
                continue
            t = re.search(r'^#\s*(?:Project:\s*)?(.+)$', text, re.MULTILINE)
            name = t.group(1).strip() if t else note.stem.replace('_note_', '')
            s = re.search(r'^current_step:\s*(.+)$', text, re.MULTILINE)
            steps_total = len(re.findall(r'^\s*-\s*\[[ x]\]\s', text, re.MULTILINE))
            step_str = ''
            if s:
                step_str = f' — step {s.group(1).strip()}' + (f'/{steps_total}' if steps_total else '')
            rows.append(f'- **{name}**{step_str} — resume from its `resume_state` (file: `{note.name}`)')
        if not rows:
            return None
        body = '\n'.join(rows)
        return ('## Open Projects (resume these — read the note before answering)\n'
                + body)
    except Exception as e:
        log(f"⚠️  get_open_projects_section error: {e}")
        return None

def get_recent_flush_context() -> Optional[str]:
    """
    Read the daily memory flush files (today + yesterday) and extract structured
    recent context: repos/paths, key decisions, recent commits, open items.
    Returns a formatted section string injected into MEMORY.md so cold-start
    sessions see actionable recent context without needing a memory_search.
    """
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        lines_out = []

        for date_str in [today, yesterday]:
            flush_file = MEMORY_DIR / f'{date_str}.md'
            if not flush_file.exists():
                continue
            text = flush_file.read_text(encoding='utf-8')
            if not text.strip():
                continue

            # Extract key decisions (lines starting with - or bullet under Key decisions)
            decisions = []
            in_decisions = False
            for line in text.splitlines():
                if re.match(r'^#{1,3}\s+Key decisions', line, re.IGNORECASE):
                    in_decisions = True
                    continue
                if in_decisions:
                    if re.match(r'^#{1,3}\s+', line):  # next section
                        in_decisions = False
                        continue
                    m = re.match(r'^[-*]\s+(.+)', line)
                    if m:
                        decisions.append(m.group(1).strip())

            # Extract repo/path mentions (lines with github/, /root/, committed, pushed)
            paths = []
            # Also collect named repos/entities from section headers and bullet context
            named_repos = []
            for line in text.splitlines():
                # Section headers naming repos/projects (e.g. '### Features built and committed to dinomem-neuron')
                m = re.match(r'^#{1,4}\s+.*(committed to|pushed to|built in|changes in|repo:|project:)\s*([\w\-\.]+)', line, re.IGNORECASE)
                if m:
                    repo = m.group(2).strip()
                    if repo and repo not in named_repos:
                        named_repos.append(f'{repo} (repo/project from session header)')
                # Committed lines: extract commit hash + description
                m = re.search(r'[Cc]ommitted?\s+`?([a-f0-9]{6,8})`?', line)
                if m:
                    clean = re.sub(r'[*`]', '', line).strip().lstrip('-').strip()
                    if clean and clean not in paths:
                        paths.append(clean[:120])
                        continue
                # Pushed lines
                if re.search(r'[Pp]ushed', line) and ('github' in line.lower() or 'repo' in line.lower() or '.git' in line.lower()):
                    clean = re.sub(r'[*`]', '', line).strip().lstrip('-').strip()
                    if clean and clean not in paths:
                        paths.append(clean[:120])
                        continue
                # Explicit path mentions
                m = re.search(r'(/root/[\w/.\-]+|github/[\w/.\-]+|workspace-[\w]+/[\w/.\-]+)', line)
                if m and m.group(1) not in paths:
                    paths.append(m.group(1))
            # Prepend named repos so they appear first
            paths = named_repos[:3] + paths

            # Deduplicate only — no hard cap; _guard_memory_size trims Searchable
            # entries if total MEMORY.md exceeds bootstrap cap. Recent Context
            # stays at top so it survives trimming (recency wins by position).
            paths = list(dict.fromkeys(paths))
            decisions = list(dict.fromkeys(decisions))

            if paths or decisions:
                lines_out.append(f'### {date_str}')
                if paths:
                    lines_out.append('**Recent work:**')
                    for p in paths:
                        lines_out.append(f'- {p}')
                if decisions:
                    lines_out.append('**Key decisions:**')
                    for d in decisions:
                        lines_out.append(f'- {d}')

        if not lines_out:
            return None

        body = '\n'.join(lines_out)
        marker_start = '<!-- dinomem:recency-start -->'
        marker_end = '<!-- dinomem:recency-end -->'
        return f'{marker_start}\n## Recent Context (from memory flush \u2014 no search needed)\n{body}\n{marker_end}'

    except Exception as e:
        log(f'⚠️  get_recent_flush_context error: {e}')
        return None


def get_previous_session_topics() -> Optional[str]:
    """
    Find the most recently archived session JSONL and extract a topic hint.
    Returns a formatted section string, or None if nothing found.
    """
    if not SESSIONS_DIR.exists():
        return None

    try:
        # Find all archived reset files, sorted by mtime descending
        archived = sorted(
            SESSIONS_DIR.glob('*.archived.reset.*.jsonl'),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        if not archived:
            return None

        latest = archived[0]
        # Extract date from filename timestamp (format: stem.archived.reset.YYYY-MM-DDTHH-MM-SS.jsonl)
        session_date = None
        try:
            ts_part = latest.stem.split('.archived.reset.')[-1]  # e.g. 2026-06-16T06-30-01
            session_date = ts_part[:10]  # YYYY-MM-DD
        except Exception:
            session_date = datetime.fromtimestamp(latest.stat().st_mtime).strftime('%Y-%m-%d')

        # Extract topics from memory files that were generated from this session
        # Strategy: read memory/*.md files dated same day as archived session
        # and pull their slug/title as topic hints
        topics = []

        # Also scan the archived JSONL itself for user message keywords
        # Read first 200 user messages, extract meaningful nouns/topics
        user_messages = []
        try:
            with open(latest, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # Support both flat (role at top) and nested (message.role) schemas
                        msg = entry.get('message', entry)
                        role = msg.get('role') or entry.get('role')
                        if role == 'user':
                            content = ''
                            raw = msg.get('content', '')
                            if isinstance(raw, str):
                                content = raw
                            elif isinstance(raw, list):
                                for block in raw:
                                    if isinstance(block, dict) and block.get('type') == 'text':
                                        content += block.get('text', '')
                            if content and len(content) > 10:
                                user_messages.append(content[:200])
                            if len(user_messages) >= 30:
                                break
                    except (json.JSONDecodeError, Exception):
                        continue
        except Exception as e:
            log(f'⚠️  Could not read archived JSONL for recency: {e}')

        # Also pull slugs from memory files dated on session_date
        if MEMORY_DIR.exists() and session_date:
            for md_file in MEMORY_DIR.glob('*.md'):
                if md_file.name.startswith('_') or md_file.name == 'MEMORY.md':
                    continue
                if session_date in md_file.name:
                    # Extract slug from filename: YYYY-MM-DD_type_slug.md
                    parts = md_file.stem.split('_', 2)
                    if len(parts) >= 3:
                        slug = parts[2].replace('-', ' ')
                        if slug not in topics:
                            topics.append(slug)

        # If no memory files matched, derive topics from user messages
        if not topics and user_messages:
            # Try LLM first for better quality
            combined = ' | '.join(user_messages[:20])
            prompt = (
                f'Extract 5-8 short topic keywords (2-3 words max each) from these user messages. '
                f'Output ONLY a comma-separated list, no explanation:\n{combined[:1500]}'
            )
            success, result = call_llm_gateway(prompt, max_tokens=100)
            if success and result.strip():
                topics = [t.strip() for t in result.split(',') if t.strip()][:8]
            else:
                # Fallback: extract meaningful words from user messages directly
                import re
                stopwords = {'the','a','an','is','are','was','were','be','been','being',
                             'have','has','had','do','does','did','will','would','could',
                             'should','may','might','shall','can','need','dare','ought',
                             'to','of','in','for','on','with','at','by','from','up','about',
                             'into','through','during','before','after','above','below',
                             'this','that','these','those','i','you','he','she','it','we',
                             'they','what','which','who','whom','how','why','when','where',
                             'and','or','but','if','then','than','so','yet','both','either',
                             'not','no','nor','just','very','also','too','only','even',
                             'already','still','now','here','there','any','all','each',
                             'kita','yang','di','ke','dari','dan','atau','ini','itu','ya',
                             'ga','gak','aja','juga','bisa','ada','mau','udah','kalau'}
                word_freq = {}
                for msg in user_messages:
                    words = re.findall(r'[a-zA-Z][a-zA-Z0-9_]{2,}', msg.lower())
                    for w in words:
                        if w not in stopwords and len(w) > 3:
                            word_freq[w] = word_freq.get(w, 0) + 1
                # Take top 8 by frequency
                top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:8]
                topics = [w for w, _ in top_words]

        if not topics:
            return None

        # Cap the Previous Session keyword line. The slug path (above) has no
        # per-item limit — a heavy work-day produces hundreds of dated memory
        # files -> hundreds of slugs -> one multi-KB line that _guard_memory_size
        # can't trim (it trims whole lines, not within a single line). Cap here:
        # keep at most _PREV_SESSION_MAX_TOPICS items AND hard-ceil the joined
        # string length so this block can never dominate the injected MEMORY.md.
        _PREV_SESSION_MAX_TOPICS = 15
        _PREV_SESSION_MAX_CHARS = 600
        topics = topics[:_PREV_SESSION_MAX_TOPICS]
        topics_str = ', '.join(topics)
        if len(topics_str) > _PREV_SESSION_MAX_CHARS:
            topics_str = topics_str[:_PREV_SESSION_MAX_CHARS].rsplit(', ', 1)[0] + ', …'
        return f'## Previous Session ({session_date}) — search if relevant\n{topics_str}'

    except Exception as e:
        log(f'⚠️  get_previous_session_topics error: {e}')
        return None

def track_removed_topics(before_files: set, after_files: set):
    """Track topics that were removed from memory/ for archive section."""
    removed = before_files - after_files
    if not removed:
        return

    archived = load_archive_tracker()
    now = datetime.now().isoformat()[:10]

    for filename in removed:
        # Extract topic name from filename
        name = filename.replace('memory_', '').replace('.md', '').replace('_', ' ').title()
        archived.append({
            'name': name,
            'type': 'project',
            'removed': now,
            'reason': 'cleaned from memory (age)'
        })

    archived = prune_archive(archived)
    save_archive_tracker(archived)
    log(f"Tracked {len(removed)} removed topic(s) in archive")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    success = generate_index(dry_run=dry_run)
    sys.exit(0 if success else 1)
