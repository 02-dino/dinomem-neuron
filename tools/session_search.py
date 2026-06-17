#!/usr/bin/env python3
"""
session_search.py — Semantic search over recent archived sessions (7-day window).

Searches kb/vector_db_sessions/ — populated by procedures/session_ingest.py.
No HyDE: query and session content are both conversational, raw query embedding
is more accurate than hypothetical expansion.

Trigger (agent-side):
  - memory_search score < 0.6 AND source session < 7 days old
  - Agent calls this tool for deeper recall when memory summary is thin

Usage (CLI):
  python3 tools/session_search.py "what did we decide about the API design"
  python3 tools/session_search.py "funding rate discussion" --k 5
  python3 tools/session_search.py --list

Usage (import):
  from tools.session_search import session_search, list_sessions
  results = session_search("what did we decide about X", k=3)
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from typing import List, Dict, Optional

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

try:
    from tools.cache_utils import cached
except ImportError:
    from cache_utils import cached

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
DB_PATH = str(WORKSPACE / "kb/vector_db_sessions")

import pathlib
_cache_dir = pathlib.Path.home() / ".cache/huggingface/hub"
LOCAL_MODEL_PATH = str(
    _cache_dir / "models--sentence-transformers--all-MiniLM-L6-v2"
    / "snapshots/8b3219a92973c328a8e22fadcfa821b5dc75636a"
)

# ── Lazy init ─────────────────────────────────────────────────────────────────
_embeddings = None
_vectorstore = None

def _get_vectorstore():
    global _embeddings, _vectorstore
    if _embeddings is None:
        _embeddings = HuggingFaceEmbeddings(
            model_name=LOCAL_MODEL_PATH,
            model_kwargs={'device': 'cpu', 'local_files_only': True},
            encode_kwargs={'normalize_embeddings': True}
        )
    if _vectorstore is None:
        if not Path(DB_PATH).exists():
            raise FileNotFoundError(
                f"Session DB not found at {DB_PATH}. "
                "Run: python3 procedures/session_ingest.py"
            )
        _vectorstore = Chroma(
            persist_directory=DB_PATH,
            embedding_function=_embeddings
        )
    return _vectorstore

# ── Cache ─────────────────────────────────────────────────────────────────────
def _cache_key(query: str, k: int, session_filter: Optional[str]) -> str:
    raw = f"{query}|{k}|{session_filter}"
    return hashlib.md5(raw.encode()).hexdigest()

# ── Search ────────────────────────────────────────────────────────────────────
def session_search(
    query: str,
    k: int = 5,
    session_filter: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> List[Dict]:
    """
    Search recent session DB for chunks relevant to query.

    Args:
        query:          Natural language query
        k:              Number of results to return
        session_filter: Filter by session name substring (optional)
        max_chars:      Truncate chunk content to this length (optional)

    Returns:
        List of dicts with keys: content, score, session_name, archived_date,
        chunk_index, source_file
    """
    db = _get_vectorstore()

    where = None
    if session_filter:
        where = {"session_name": {"$contains": session_filter}}

    try:
        results = db.similarity_search_with_relevance_scores(
            query, k=k, filter=where
        )
    except Exception:
        # Fallback: no filter if Chroma rejects $contains
        results = db.similarity_search_with_relevance_scores(query, k=k)

    output = []
    for doc, score in results:
        content = doc.page_content
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + '...'
        output.append({
            'content': content,
            'score': round(score, 4),
            'session_name': doc.metadata.get('session_name', ''),
            'archived_date': doc.metadata.get('archived_date', ''),
            'chunk_index': doc.metadata.get('chunk_index', 0),
            'source_file': doc.metadata.get('source_file', ''),
        })

    return output

def list_sessions() -> List[Dict]:
    """List all sessions currently indexed in the DB."""
    db = _get_vectorstore()
    existing = db.get()
    seen = {}
    for meta in (existing.get('metadatas') or []):
        name = meta.get('session_name', '')
        if name and name not in seen:
            seen[name] = {
                'session_name': name,
                'archived_date': meta.get('archived_date', ''),
                'source_file': meta.get('source_file', ''),
            }
    return sorted(seen.values(), key=lambda x: x['archived_date'], reverse=True)

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Search recent session vector DB')
    parser.add_argument('query', nargs='?', help='Search query')
    parser.add_argument('--k', type=int, default=5, help='Number of results (default: 5)')
    parser.add_argument('--session', type=str, help='Filter by session name substring')
    parser.add_argument('--list', action='store_true', help='List all indexed sessions')
    parser.add_argument('--json', action='store_true', help='Output raw JSON')
    args = parser.parse_args()

    if args.list:
        sessions = list_sessions()
        if args.json:
            print(json.dumps(sessions, indent=2))
        else:
            if not sessions:
                print("No sessions indexed. Run: python3 procedures/session_ingest.py")
                return
            print(f"\n{'Session':<50} {'Archived':<12}")
            print("-" * 64)
            for s in sessions:
                print(f"{s['session_name']:<50} {s['archived_date']:<12}")
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    try:
        results = session_search(args.query, k=args.k, session_filter=args.session)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No results found.")
        return

    print(f"\nSession search: '{args.query}' — {len(results)} result(s)\n")
    for i, r in enumerate(results, 1):
        print(f"[{i}] score={r['score']} | session={r['session_name']} | date={r['archived_date']} | chunk={r['chunk_index']}")
        print(r['content'])
        print()

if __name__ == '__main__':
    main()
