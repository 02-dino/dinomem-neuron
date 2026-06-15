#!/usr/bin/env python3
"""
docs_search.py — Semantic search over long-form documents (RAG).

Searches kb/vector_db_docs/ — the structure-aware document store
populated by procedures/docs_ingest.py.

Usage (CLI):
  python3 tools/docs_search.py "pasal ketenagakerjaan" --k 5
  python3 tools/docs_search.py "force majeure" --doc "uu-cipta-kerja"
  python3 tools/docs_search.py "definisi" --section "BAB I" --k 3
  python3 tools/docs_search.py --list                    # list all indexed docs

Usage (import):
  from tools.docs_search import docs_search, list_docs
  results = docs_search("force majeure", k=5)
  docs    = list_docs()
"""

import os
import sys
import json
import argparse
import hashlib
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
DB_PATH = str(WORKSPACE / "kb/vector_db_docs")

LOCAL_MODEL_PATH = (
    "DINOMEM_HF_CACHE_PLACEHOLDER/hub/models--sentence-transformers--all-MiniLM-L6-v2"
    "/snapshots/8b3219a92973c328a8e22fadcfa821b5dc75636a"
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
                f"Docs DB not found at {DB_PATH}. "
                "Run: python3 procedures/docs_ingest.py"
            )
        _vectorstore = Chroma(
            persist_directory=DB_PATH,
            embedding_function=_embeddings
        )
    return _vectorstore

# ── Cache ─────────────────────────────────────────────────────────────────────
def _cache_key(query: str, k: int, doc_filter: Optional[str],
               section_filter: Optional[str]) -> str:
    raw = f"{query}|{k}|{doc_filter}|{section_filter}"
    return hashlib.md5(raw.encode()).hexdigest()

@cached("docs_search", ttl_seconds=300)
def _cached_search(cache_key: str, query: str, fetch_k: int):
    vs = _get_vectorstore()
    raw = vs.similarity_search_with_score(query, k=fetch_k)
    return [
        {"page_content": doc.page_content, "metadata": doc.metadata, "score": float(score)}
        for doc, score in raw
    ]

# ── Search ────────────────────────────────────────────────────────────────────
def _hyde_expand(query: str) -> str:
    """Generate a hypothetical document passage for HyDE query expansion.
    Falls back to original query if LLM unavailable."""
    try:
        import subprocess, shutil
        openclaw_bin = shutil.which('openclaw') or '/home/linuxbrew/.linuxbrew/bin/openclaw'
        prompt = (
            f'Write a short factual passage (2-3 sentences) that would directly answer '
            f'this question, as if from a formal document or legal text: {query}'
        )
        result = subprocess.run(
            [openclaw_bin, 'capability', 'model', 'run', '--prompt', prompt, '--gateway', '--json'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data.get('ok') and data.get('outputs'):
                return data['outputs'][0].get('text', query).strip() or query
    except Exception:
        pass
    return query


def docs_search(
    query: str,
    k: int = 5,
    doc_filter: Optional[str] = None,
    section_filter: Optional[str] = None,
    max_chars: Optional[int] = None,
    hyde: bool = True,
) -> List[Dict]:
    """
    Semantic search over indexed long-form documents.

    Args:
        query:          Search query (natural language)
        k:              Number of results (default 5)
        doc_filter:     Filter by document title/filename substring (case-insensitive)
        section_filter: Filter by section title substring (case-insensitive)
        max_chars:      Truncate chunk content to N chars (None = no limit)

    Returns:
        List of dicts: {content, score, metadata: {doc_title, section_title,
                        section_level, chunk_index, source_file, date, doc_type}}

    Example:
        docs_search("force majeure", k=5)
        docs_search("pasal 1", doc_filter="uu-cipta-kerja", k=3)
    """
    # HyDE: expand query with hypothetical document passage for better recall
    search_query = _hyde_expand(query) if hyde else query
    ck = _cache_key(query, k, doc_filter, section_filter)  # cache key uses original query
    raw = _cached_search(ck, search_query, k * 4)

    results = []
    for item in raw:
        meta = item['metadata']

        # doc_filter — match against title or source_file
        if doc_filter:
            title = meta.get('title', '').lower()
            src = meta.get('source_file', '').lower()
            if doc_filter.lower() not in title and doc_filter.lower() not in src:
                continue

        # section_filter
        if section_filter:
            section = meta.get('section_title', '').lower()
            if section_filter.lower() not in section:
                continue

        content = item['page_content']
        if max_chars:
            content = content[:max_chars]

        results.append({
            'content': content,
            'score': item['score'],
            'metadata': {
                'doc_title': meta.get('title', ''),
                'section_title': meta.get('section_title', ''),
                'section_level': meta.get('section_level', 0),
                'chunk_index': meta.get('chunk_index', 0),
                'source_file': meta.get('source_file', ''),
                'date': meta.get('date', ''),
                'doc_type': meta.get('doc_type', 'document'),
            }
        })

        if len(results) >= k:
            break

    return results

# ── List docs ─────────────────────────────────────────────────────────────────
def list_docs() -> List[Dict]:
    """
    List all indexed documents with chunk counts.

    Returns:
        List of dicts: {title, source_file, doc_type, date, chunk_count}
    """
    vs = _get_vectorstore()
    all_data = vs.get()
    metadatas = all_data.get('metadatas') or []

    seen: Dict[str, Dict] = {}
    for meta in metadatas:
        src = meta.get('source_file', '')
        if not src:
            continue
        if src not in seen:
            seen[src] = {
                'title': meta.get('title', Path(src).stem),
                'source_file': src,
                'doc_type': meta.get('doc_type', 'document'),
                'date': meta.get('date', ''),
                'chunk_count': 0,
            }
        seen[src]['chunk_count'] += 1

    return sorted(seen.values(), key=lambda x: x['title'])

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='Semantic search over long-form documents',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 tools/docs_search.py "force majeure"
  python3 tools/docs_search.py "pasal ketenagakerjaan" --k 5
  python3 tools/docs_search.py "definisi" --doc "uu-cipta-kerja" --k 3
  python3 tools/docs_search.py "BAB I" --section "Ketentuan Umum"
  python3 tools/docs_search.py --list
        """
    )
    parser.add_argument('query', nargs='?', default='', help='Search query')
    parser.add_argument('--k', type=int, default=5, help='Number of results (default: 5)')
    parser.add_argument('--doc', type=str, default=None, help='Filter by document title/filename')
    parser.add_argument('--section', type=str, default=None, help='Filter by section title')
    parser.add_argument('--max-chars', type=int, default=1500, help='Truncate chunk content (default: 1500)')
    parser.add_argument('--list', action='store_true', help='List all indexed documents')
    parser.add_argument('--json', action='store_true', help='Output raw JSON')
    args = parser.parse_args()

    try:
        if args.list:
            docs = list_docs()
            if args.json:
                print(json.dumps(docs, ensure_ascii=False, indent=2))
            else:
                if not docs:
                    print("No documents indexed. Run: python3 procedures/docs_ingest.py")
                else:
                    print(f"{'Title':<40} {'Type':<12} {'Date':<12} {'Chunks':>6}")
                    print('-' * 74)
                    for d in docs:
                        print(f"{d['title'][:39]:<40} {d['doc_type'][:11]:<12} {d['date']:<12} {d['chunk_count']:>6}")
            return

        if not args.query:
            parser.print_help()
            sys.exit(1)

        results = docs_search(
            args.query,
            k=args.k,
            doc_filter=args.doc,
            section_filter=args.section,
            max_chars=args.max_chars,
        )

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return

        if not results:
            print("No results found.")
            return

        for i, r in enumerate(results, 1):
            m = r['metadata']
            print(f"\n{'─'*60}")
            print(f"[{i}] {m['doc_title']} › {m['section_title']} (chunk {m['chunk_index']})")
            print(f"    score: {r['score']:.4f} | type: {m['doc_type']} | date: {m['date']}")
            print(f"    file: {Path(m['source_file']).name}")
            print()
            print(r['content'])

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
