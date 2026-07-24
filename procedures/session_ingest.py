#!/usr/bin/env python3
"""
session_ingest.py — Ingest archived session .jsonl files into a vector DB for deep recall.

Scoped to sessions archived within the last 7 days. Auto-cleans entries for
sessions that have been deleted. Uses sliding window chunking over user+assistant
turns (filters out system, tool calls, and short turns < 15 chars).

Chunking strategy:
  - Filter: role user + assistant only, content >= 15 chars
  - Sliding window: window=5 exchanges, step=3, overlap=2
  - Each chunk = consecutive exchanges as one unit

Usage:
  python3 procedures/session_ingest.py              # ingest all recent archives
  python3 procedures/session_ingest.py --dry-run    # show what would be ingested
  python3 procedures/session_ingest.py --cleanup    # remove expired entries only
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

from langchain_chroma import Chroma
from langchain_core.documents import Document

# Shared embedding resolver + e5 query:/passage: prefixing (single source of
# truth: tools/_embed.py). Migrated 2026-07-09 (L1) off the per-file _PrefixedHFE
# + local snapshot-resolution dupes so ingest (passage) and query vectors always
# align to one model/prefix definition. tools/ is a sibling of procedures/.
import sys as _sys_embed
_sys_embed.path.insert(0, str(Path(__file__).parent.parent / "tools"))
try:
    from tools._embed import get_embeddings, model_id, EMBED_MODEL_META_KEY
except ImportError:
    from _embed import get_embeddings, model_id, EMBED_MODEL_META_KEY

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE       = Path(__file__).parent.parent
# Archived sessions are written IN PLACE by session_reset.py into the agent's
# OpenClaw sessions dir as *.archived.*.jsonl (NOT into workspace/archives, which
# is never created). Point the ingester at that real location, filled by the
# installer (DINOMEM_AGENT_SESSIONS_PLACEHOLDER = $OPENCLAW_DIR/agents/$AGENT_ID/
# sessions). Env override wins for non-standard layouts / testing.
ARCHIVES_DIR    = Path(
    os.environ.get("SESSION_ARCHIVES_DIR") or "DINOMEM_AGENT_SESSIONS_PLACEHOLDER"
)
DB_PATH         = str(WORKSPACE / "kb/vector_db_sessions")
LOG_FILE        = WORKSPACE / "logs/session_ingest.log"
MAX_AGE_DAYS    = 14  # widened 7->14: session_search backstop (coupled w/ session_reset.ARCHIVE_MAX_AGE_DAYS)
MIN_TURN_CHARS  = 15
WINDOW_SIZE     = 5   # exchanges per chunk
STEP            = 3   # step between windows
# overlap = WINDOW_SIZE - STEP = 2


os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(LOG_FILE, 'a', encoding='utf-8')

def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    _log_fh.write(line + '\n')
    _log_fh.flush()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()

def is_within_max_age(path: Path) -> bool:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    return mtime >= cutoff

def parse_turns(jsonl_path: Path) -> list[dict]:
    """Parse .jsonl and return filtered user+assistant turns >= MIN_TURN_CHARS."""
    turns = []
    try:
        with open(jsonl_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # OpenClaw jsonl wraps message under entry.message
                msg = entry.get('message', entry)
                role = msg.get('role', '')
                if role not in ('user', 'assistant'):
                    continue

                # Extract text content
                content = msg.get('content', '')
                if isinstance(content, list):
                    # Handle content blocks
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            text_parts.append(block.get('text', ''))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    content = ' '.join(text_parts).strip()

                if not isinstance(content, str):
                    content = str(content)

                content = content.strip()
                if len(content) < MIN_TURN_CHARS:
                    continue

                turns.append({'role': role, 'content': content})
    except Exception as e:
        log(f"  WARN: failed to parse {jsonl_path.name}: {e}")

    return turns

def sliding_window_chunks(turns: list[dict]) -> list[str]:
    """
    Sliding window over turns.
    window=5, step=3, overlap=2.
    Each chunk = consecutive turns formatted as role: content.
    """
    if not turns:
        return []

    chunks = []
    i = 0
    while i < len(turns):
        window = turns[i:i + WINDOW_SIZE]
        chunk_lines = [f"{t['role']}: {t['content']}" for t in window]
        chunk_text = '\n\n'.join(chunk_lines)
        if chunk_text.strip():
            chunks.append(chunk_text)
        i += STEP

    return chunks

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_existing_hashes(db: Chroma) -> dict[str, str]:
    """Return {source_file: file_hash} for all entries in DB."""
    existing = db.get()
    hashes: dict[str, str] = {}
    for meta in (existing.get('metadatas') or []):
        src = meta.get('source_file', '')
        fh = meta.get('file_hash', '')
        if src and fh:
            hashes[src] = fh
    return hashes

def get_all_source_files(db: Chroma) -> set[str]:
    """Return set of all source_file values in DB."""
    existing = db.get()
    sources = set()
    for meta in (existing.get('metadatas') or []):
        src = meta.get('source_file', '')
        if src:
            sources.add(src)
    return sources

# ── Cleanup ───────────────────────────────────────────────────────────────────
def cleanup_expired(db: Chroma, dry_run: bool = False) -> int:
    """Remove DB entries whose source .jsonl no longer exists or is > 7 days old."""
    sources = get_all_source_files(db)
    removed = 0
    for src in sources:
        path = Path(src)
        should_remove = not path.exists() or not is_within_max_age(path)
        if should_remove:
            log(f"  CLEANUP: {path.name}")
            if not dry_run:
                db.delete(where={"source_file": src})
            removed += 1
    return removed

# ── Ingest ────────────────────────────────────────────────────────────────────
def ingest_session(jsonl_path: Path, db: Chroma,
                   existing_hashes: dict[str, str],
                   dry_run: bool = False) -> str:
    """Ingest one .jsonl file. Returns 'added' | 'updated' | 'skipped' | 'error' | 'empty'."""
    try:
        current_hash = get_file_hash(jsonl_path)
        src_key = str(jsonl_path)
        stored_hash = existing_hashes.get(src_key)

        if stored_hash == current_hash:
            return 'skipped'

        turns = parse_turns(jsonl_path)
        if not turns:
            return 'empty'

        chunks = sliding_window_chunks(turns)
        if not chunks:
            return 'empty'

        is_update = stored_hash is not None
        action = 'UPDATE' if is_update else 'NEW'
        log(f"  {action}: {jsonl_path.name} → {len(turns)} turns → {len(chunks)} chunks")

        if not dry_run:
            if is_update:
                db.delete(where={"source_file": src_key})

            docs = []
            archived_date = datetime.fromtimestamp(
                jsonl_path.stat().st_mtime, tz=timezone.utc
            ).strftime('%Y-%m-%d')

            for i, chunk_text in enumerate(chunks):
                meta = {
                    'source_file': src_key,
                    'file_hash': current_hash,
                    'session_name': jsonl_path.stem,
                    'archived_date': archived_date,
                    'chunk_index': i,
                    'turn_count': len(turns),
                }
                # Enrich with metadata prefix for better retrieval
                prefix = f"Session: {jsonl_path.stem} | Date: {archived_date}"
                enriched = f"{prefix}\n\n{chunk_text}"
                docs.append(Document(page_content=enriched, metadata=meta))

            db.add_documents(docs)

        return 'updated' if is_update else 'added'

    except Exception as e:
        log(f"  ERROR: {jsonl_path.name}: {e}")
        return 'error'

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Ingest archived sessions into vector DB')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change, write nothing')
    parser.add_argument('--cleanup', action='store_true', help='Remove expired entries only, no ingestion')
    args = parser.parse_args()

    log("=" * 60)
    log("session_ingest.py starting")
    if args.dry_run:
        log("DRY RUN — no writes")

    # Init embeddings (shared resolver: model id + local-snapshot resolution +
    # asymmetric query:/passage: prefixing all centralized in tools/_embed.py).
    log("Loading embeddings model...")
    embeddings = get_embeddings()

    # Init DB
    Path(DB_PATH).mkdir(parents=True, exist_ok=True)
    db = Chroma(
        persist_directory=DB_PATH,
        embedding_function=embeddings,
        # Stamp the embedding model id so query paths can detect a model mismatch
        # (model swap vs an old-model store => silent garbage). See _embed.warn_on_mismatch.
        collection_metadata={
            "description": "Recent session store (7-day window, sliding window chunks)",
            EMBED_MODEL_META_KEY: model_id(),
        }
    )

    # Cleanup expired entries first
    removed = cleanup_expired(db, dry_run=args.dry_run)
    log(f"Cleanup: {removed} expired entries removed")

    if args.cleanup:
        log("Cleanup-only mode, done.")
        return

    # Find archived .jsonl files within MAX_AGE_DAYS
    if not ARCHIVES_DIR.exists():
        log(f"Archives dir not found: {ARCHIVES_DIR} — nothing to ingest")
        return

    jsonl_files = [
        p for p in ARCHIVES_DIR.glob("*.archived.*.jsonl")
        if is_within_max_age(p)
    ]

    if not jsonl_files:
        log("No recent archived sessions found")
        return

    log(f"Found {len(jsonl_files)} recent archived sessions (within {MAX_AGE_DAYS} days)")

    existing_hashes = get_existing_hashes(db)
    stats = {'added': 0, 'updated': 0, 'skipped': 0, 'empty': 0, 'error': 0}

    for path in sorted(jsonl_files):
        result = ingest_session(path, db, existing_hashes, dry_run=args.dry_run)
        stats[result] = stats.get(result, 0) + 1

    log("=" * 60)
    log(
        f"Done — added: {stats['added']} | updated: {stats['updated']} | "
        f"skipped: {stats['skipped']} | empty: {stats['empty']} | errors: {stats['error']}"
    )
    log(f"DB: {DB_PATH}")

if __name__ == '__main__':
    # ---- dinomem-neuron license guard ----
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from license_check import enforce_license as _enforce_license
        _enforce_license(_os.path.basename(__file__))
    except SystemExit:
        raise
    except Exception:
        pass
    main()
