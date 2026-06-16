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


def get_openclaw_config():
    """Get model and API config from OpenClaw."""
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if not openclaw_config.exists():
        return None, None, None

    try:
        with open(openclaw_config, 'r') as f:
            config = json.load(f)

        model = config.get('agents', {}).get('defaults', {}).get('model', {}).get('primary')
        providers = config.get('models', {}).get('providers', {})

        # Find provider for this model
        api_key = None
        api_base = None

        if model:
            if model.startswith("kimi") or "/kimi" in model:
                provider = providers.get('kimi-coding', providers.get('kimi-code', {}))
                api_key = provider.get('apiKey')
                api_base = provider.get('baseUrl')
            elif model.startswith("gemini") or "/gemini" in model or model.startswith("google"):
                provider = providers.get('google', {})
                api_key = provider.get('apiKey')
                api_base = provider.get('baseUrl')
            elif model.startswith("openrouter") or "/" in model:
                provider = providers.get('openrouter', {})
                api_key = provider.get('apiKey')
                api_base = provider.get('baseUrl')

        return model, api_key, api_base

    except Exception as e:
        log(f"Error reading OpenClaw config: {e}")
        return None, None, None


def call_llm_gateway(prompt, max_tokens=2000):
    """Call LLM via OpenClaw gateway."""
    import subprocess

    try:
        result = subprocess.run(
            ["/home/linuxbrew/.linuxbrew/bin/openclaw", "capability", "model", "run",
             "--prompt", prompt,
             "--gateway",
             "--json"],
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

DOMAIN must be one of: WORKFLOW | MARKET | PROJECT | PREFERENCE | REFERENCE
AGE: use "[Xd]" for days ago, "[persist]" for behavioral rules
TOPIC_NAME: 3-5 words, specific — not vague
TAGS: 4-6 comma-separated keywords including tickers, project names, exact terms the user might use
BRIEF-DESCRIPTION: max 10 words, only if the tags don't make it obvious

RULES:
1. FLAT LIST — one entry per line. NO indentation, NO subtopics, NO hierarchy
2. KEYWORD-DENSE — every word in tags should be something a user might search for
3. TICKERS FIRST — if a topic mentions BTC, ETH, PLUME, SPK, include the ticker as a tag
4. NO FLUFF — no "discussions about", no "various topics", no "general insights"
5. DISPLAY ORDER — newest first (largest Xd first), then [persist]. This is ORDERING only,
   NOT how you choose what to drop — see SELECTION below.
6. MAX 50 entries (this is a CEILING, not a quota — only fill a slot if the entry is genuinely
   transferable; do not pad to 50). When more than 50 exist, drop by SELECTION — never drop purely by age.
7. GROUP BY DOMAIN only if it helps scanning; otherwise sort by recency
8. For market content: include specific tickers, catalysts, patterns, trade signals

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
        domain = "REFERENCE"
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

                # Determine domain from content signals
                content_lower = line.lower()
                if any(w in content_lower for w in ['workflow', 'process', 'step', 'classify', 'filter']):
                    domain = "WORKFLOW"
                elif any(w in content_lower for w in ['btc', 'eth', 'price', 'trade', 'pump', 'buyback', 'consensus']):
                    domain = "MARKET"
                elif any(w in content_lower for w in ['preference', 'user', 'communication', 'style']):
                    domain = "PREFERENCE"
                elif any(w in content_lower for w in ['project', 'protocol', 'token', 'tvl', 'rwa']):
                    domain = "PROJECT"

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

        log(f"Built summary from {len(summary_parts)} memory file(s)")
        return '\n'.join(summary_parts)

    except Exception as e:
        log(f"Error building document summary: {e}")
        import traceback
        log(traceback.format_exc())
        return ""


def _compute_file_hashes() -> dict:
    """Compute SHA-256 hashes of all memory/*.md files for change detection."""
    import hashlib
    hashes = {}
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
        recency_section = get_previous_session_topics()
        recency_block = f'\n{recency_section}\n' if recency_section else ''
        index_content = f"{persistent}\n{recency_block}\n## Searchable — Active (retrieved via memory_search / memory_get)\n_No documents in memory._\n\n## Archived — Deleted from memory (not searchable)\n_None._\n"
        if not dry_run:
            INDEX_FILE.write_text(index_content, encoding='utf-8')
            memory_content = f"MEMORY.md\n\n{index_content}"
            MEMORY_FILE.write_text(memory_content, encoding='utf-8')
        log("Wrote minimal index (persistent topics only)")
        return True

    # Generate topics with LLM
    doc_count = len(summary.split('\n')) if summary else 0
    log(f"Generating topics from {doc_count} document summaries...")
    topics_text = generate_topics_with_llm(summary)

    # Load persistent topics
    persistent = load_persistent_topics()

    # Get previous session recency hint
    recency_section = get_previous_session_topics()
    recency_block = f'\n{recency_section}\n' if recency_section else ''

    # Assemble final index
    index_content = f"""{persistent}
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
    memory_content = f"MEMORY.md\n\n{index_content}"
    MEMORY_FILE.write_text(memory_content, encoding='utf-8')
    log(f"✅ Wrote {MEMORY_FILE} (auto-injected)")

    # Estimate token count
    token_estimate = len(index_content.split()) * 1.3  # rough estimate
    log(f"📊 Estimated size: ~{int(token_estimate)} tokens")

    return True


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

        topics_str = ', '.join(topics)
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
