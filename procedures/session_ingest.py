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
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document

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
MAX_AGE_DAYS    = 7
MIN_TURN_CHARS  = 15
WINDOW_SIZE     = 5   # exchanges per chunk
STEP            = 3   # step between windows
# overlap = WINDOW_SIZE - STEP = 2

import pathlib
# Resolve the local model snapshot dynamically rather than hardcoding a snapshot
# hash (hardcoded hashes break silently when the HF cache pins a different
# revision on another machine). MUST match what session_search.py uses, or
# ingested session vectors won't align with query vectors. Honor HF_HOME; glob
# the snapshots dir; fall back to the model id so HF can resolve it normally.
# Single source of truth via env (fallback const). MUST match session_search.py.
_MODEL_ID = os.environ.get("DINOMEM_EMBED_MODEL", "intfloat/multilingual-e5-small")
_hf_cache = pathlib.Path(
    os.environ.get("HF_HOME")
    or (pathlib.Path.home() / ".cache" / "huggingface")
)
_model_root = _hf_cache / "hub" / ("models--" + _MODEL_ID.replace("/", "--")) / "snapshots"
try:
    _snaps = sorted(p for p in _model_root.iterdir() if p.is_dir()) if _model_root.exists() else []
    LOCAL_MODEL_PATH = str(_snaps[-1]) if _snaps else _MODEL_ID
except Exception:
    LOCAL_MODEL_PATH = _MODEL_ID
_LOCAL_MODEL_RESOLVED = LOCAL_MODEL_PATH != _MODEL_ID

# e5 asymmetric prefixing (query:/passage:) — must match session_search.py so
# ingested (passage) and query vectors align. Disable via DINOMEM_EMBED_PREFIX=0.
_PREFIX_ENABLED = os.environ.get("DINOMEM_EMBED_PREFIX", "1") != "0"
class _PrefixedHFE(HuggingFaceEmbeddings):
    def embed_documents(self, texts):
        if _PREFIX_ENABLED:
            texts = ["passage: " + t for t in texts]
        return super().embed_documents(texts)
    def embed_query(self, text):
        if _PREFIX_ENABLED:
            text = "query: " + text
        return super().embed_query(text)

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

    # Init embeddings
    log("Loading embeddings model...")
    embeddings = _PrefixedHFE(
        model_name=LOCAL_MODEL_PATH,
        model_kwargs={'device': 'cpu', 'local_files_only': _LOCAL_MODEL_RESOLVED},
        encode_kwargs={'normalize_embeddings': True}
    )

    # Init DB
    Path(DB_PATH).mkdir(parents=True, exist_ok=True)
    db = Chroma(
        persist_directory=DB_PATH,
        embedding_function=embeddings,
        collection_metadata={"description": "Recent session store (7-day window, sliding window chunks)"}
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
