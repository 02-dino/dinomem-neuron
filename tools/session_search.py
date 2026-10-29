#!/usr/bin/env python3
"""
session_search.py — Semantic search over recent archived sessions (21-day window).

Searches kb/vector_db_sessions/ — populated by procedures/session_ingest.py.
No HyDE: query and session content are both conversational, raw query embedding
is more accurate than hypothetical expansion.

Trigger (agent-side):
  - memory_search returns thin/uncertain result ([uncertain] tag, few hits, low confidence)
  - AND memory file date < 21 days old
  - AND user intent requires detail

Usage (CLI):
  python3 tools/session_search.py "what did we decide about X"
  python3 tools/session_search.py "the pricing discussion with the vendor" --k 5
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
from datetime import datetime
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

# Cap CPU threads for local torch model loads (multi-agent host) — must run
# before langchain_huggingface import. See tools/_torch_cap.py.
try:
    from tools import _torch_cap  # noqa: F401
except ImportError:
    import _torch_cap  # noqa: F401

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
DB_PATH = str(WORKSPACE / "kb/vector_db_sessions")

import pathlib
# Resolve the local model snapshot dynamically instead of hardcoding a snapshot
# hash (hardcoded hashes break silently when the HF cache pins a different
# revision on another machine). Glob the snapshots dir; fall back to the model
# id so HF can resolve it normally if no local snapshot exists.
# Single source of truth via env (fallback const). Multilingual e5 (EN/ID + majors, 384-dim, 512 ctx).
_MODEL_ID = os.environ.get("DINOMEM_EMBED_MODEL", "intfloat/multilingual-e5-small")
_cache_dir = pathlib.Path(os.environ.get("HF_HOME") or (pathlib.Path.home() / ".cache/huggingface")) / "hub"
_model_root = _cache_dir / ("models--" + _MODEL_ID.replace("/", "--")) / "snapshots"
try:
    _snaps = sorted(p for p in _model_root.iterdir() if p.is_dir()) if _model_root.exists() else []
    LOCAL_MODEL_PATH = str(_snaps[-1]) if _snaps else _MODEL_ID
except Exception:
    LOCAL_MODEL_PATH = _MODEL_ID
_LOCAL_MODEL_RESOLVED = LOCAL_MODEL_PATH != _MODEL_ID

# e5 asymmetric prefixing (query:/passage:). Disable via DINOMEM_EMBED_PREFIX=0 for symmetric models.
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

# ── Lazy init ─────────────────────────────────────────────────────────────────
_embeddings = None
_vectorstore = None

def _get_vectorstore():
    global _embeddings, _vectorstore
    if _embeddings is None:
        _embeddings = _PrefixedHFE(
            model_name=LOCAL_MODEL_PATH,
            model_kwargs={'device': 'cpu', 'local_files_only': _LOCAL_MODEL_RESOLVED},
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

# ── Search ────────────────────────────────────────────────────────────────────
def session_search(
    query: str,
    k: int = 5,
    session_filter: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> List[Dict]:
    """
    Search recent session DB for chunks relevant to query.

    Returns list of dicts: content, score, session_name, archived_date, chunk_index, source_file
    """
    db = _get_vectorstore()

    where = None
    if session_filter:
        where = {"session_name": {"$contains": session_filter}}

    # Over-fetch (k*3) so the recency rerank has room to reorder the top band.
    # Sessions are recalled to answer "what did we decide about X" — almost
    # always the MOST RECENT discussion of X. Pure similarity can surface an
    # older session over a newer one that resolved the topic. We add a LIGHT
    # recency tie-breaker (half-life ~3d, w ~0.15) so among similarly-relevant
    # sessions the fresher wins, without burying a clearly-more-relevant older
    # session. No recency floor / no intraday mode — this is recall, not news.
    fetch_k = max(k * 3, 15)
    try:
        raw = db.similarity_search_with_relevance_scores(query, k=fetch_k, filter=where)
    except Exception:
        raw = db.similarity_search_with_relevance_scores(query, k=fetch_k)

    items = []
    for doc, score in raw:
        content = doc.page_content
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + '...'
        # session_ids: CSV of distinct session_ids this chunk spans (ingest fills
        # it from the archive turns; '' when the archive carried no attribution).
        # Surface it so downstream (answer.py -> retrieval.jsonl -> score.py
        # recall_at_k) can attribute a retrieved chunk to gold answer_session_ids.
        _sids_csv = doc.metadata.get('session_ids', '') or ''
        _sids = [s for s in _sids_csv.split(',') if s]
        items.append({
            'content': content,
            'score': round(score, 4),
            'session_name': doc.metadata.get('session_name', ''),
            'archived_date': doc.metadata.get('archived_date', ''),
            'chunk_index': doc.metadata.get('chunk_index', 0),
            'source_file': doc.metadata.get('source_file', ''),
            'session_ids': _sids,
            # convenience scalar: first attributed session_id (or '' ) for
            # consumers that read a single id via c.get('session_id').
            'session_id': (_sids[0] if _sids else ''),
        })

    return _recency_rerank(items, k)


def _parse_archived_date(s: str):
    """Parse archived_date (YYYY-MM-DD or ISO) to naive datetime; None if unparseable."""
    if not s:
        return None
    try:
        val = str(s).replace('Z', '+00:00')
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], '%Y-%m-%d')
        except Exception:
            return None


def _recency_rerank(items: List[Dict], k: int, half_life_days: float = 3.0,
                    w: float = 0.25) -> List[Dict]:
    """Recency tie-breaker over session results, blended on SIMILARITY SCORE gap
    (not rank position) so the effect scales with how close the matches are.

    final = (1-w)*relevance_norm + w*recency_component
    - relevance_norm: similarity score min-max normalized to [0,1] across the
      candidate set. A SMALL score gap (near-tie) => recency decides; a LARGE
      score gap => relevance dominates and recency cannot override it.
    - recency_component: 0.5 ** (age_days / half_life), in [0,1].
    Sessions with no parseable date use the list-median age (neither boosted
    nor buried). This keeps recency a genuine tie-breaker, never a driver.
    """
    n = len(items)
    if n <= 1:
        return items[:k]
    now = datetime.now()
    ages = []
    for it in items:
        dt = _parse_archived_date(it.get('archived_date', ''))
        ages.append(None if dt is None else max(0.0, (now - dt).total_seconds() / 86400.0))
    known = [a for a in ages if a is not None]
    median_age = sorted(known)[len(known) // 2] if known else 0.0

    scores = [float(it.get('score', 0.0)) for it in items]
    smin, smax = min(scores), max(scores)
    span = (smax - smin) or 1e-9

    scored = []
    for i, it in enumerate(items):
        relevance_norm = (scores[i] - smin) / span  # best=1.0, worst=0.0
        age = ages[i] if ages[i] is not None else median_age
        recency_component = 0.5 ** (age / half_life_days)
        final = (1.0 - w) * relevance_norm + w * recency_component
        scored.append((final, i, it))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [it for _, _, it in scored][:k]

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
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--session', type=str, help='Filter by session name substring')
    parser.add_argument('--list', action='store_true', help='List all indexed sessions')
    parser.add_argument('--json', action='store_true', help='Output raw JSON')
    args = parser.parse_args()

    if args.list:
        try:
            sessions = list_sessions()
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(1)
        if args.json:
            print(json.dumps(sessions, indent=2))
        else:
            if not sessions:
                print("No sessions indexed. Run: python3 procedures/session_ingest.py")
                return
            print(f"\n{'Session':<60} {'Archived':<12}")
            print("-" * 74)
            for s in sessions:
                print(f"{s['session_name']:<60} {s['archived_date']:<12}")
        return

    if not args.query:
        parser.print_help()
        sys.exit(1)

    try:
        results = session_search(args.query, k=args.k, session_filter=args.session)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    try:  # retrieval observability (fail-open)
        sys.path.insert(0, str(WORKSPACE / "procedures"))
        from _retrieval_log import log_retrieval
        log_retrieval(
            WORKSPACE, "session_search", args.query, k=args.k,
            result_uris=[r.get("source_file") or r.get("session_name") for r in results],
            top_scores=[r.get("score") for r in results],
        )
    except Exception:
        pass

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
