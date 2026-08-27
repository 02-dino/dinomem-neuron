#!/usr/bin/env python3
"""
docs_search.py — Hybrid search over long-form documents (RAG).

Searches kb/vector_db_docs/ — the structure-aware document store
populated by procedures/docs_ingest.py.

Retrieval channels (all additive, all local, no cloud call):
  1. Semantic — e5-small cosine over Chroma, optionally HyDE-expanded.
  2. Lexical  — zero-dependency Okapi BM25 over the same chunk corpus, fused
                with the semantic ranking via Reciprocal Rank Fusion (RRF).
                Gives exact tokens ("Pasal 1338", defined terms, citations)
                a dedicated channel where pure semantic is weak. Disable with
                DOCS_BM25=0; tune fusion constant with DOCS_RRF_K (default 60).
  3. Structural — entity-graph augmentation appends chunks sharing entities.

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
import re
import json
import math
import argparse
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Tuple

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

try:
    from tools.cache_utils import cached, get as cache_get, set as cache_set
except ImportError:
    from cache_utils import cached, get as cache_get, set as cache_set

from langchain_chroma import Chroma

# Shared embedding resolver + e5 query:/passage: prefixing (single source of
# truth: tools/_embed.py). Migrated 2026-07-09 (L1) off the per-file _PrefixedHFE
# + local snapshot-resolution dupes so model id, cache resolution, and prefixing
# live in exactly one place.
try:
    from tools._embed import get_embeddings, warn_on_mismatch
except ImportError:
    from _embed import get_embeddings, warn_on_mismatch

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
DB_PATH = str(WORKSPACE / "kb/vector_db_docs")

# ── Lazy init ─────────────────────────────────────────────────────────────────
_embeddings = None
_vectorstore = None

def _get_vectorstore():
    global _embeddings, _vectorstore
    if _embeddings is None:
        # Shared resolver: model id, local-snapshot resolution, and asymmetric
        # query:/passage: prefixing all handled centrally in tools/_embed.py.
        _embeddings = get_embeddings()
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
        # Fail loud if this store was built with a different embedding model than
        # the one now configured (else query vectors silently misalign -> garbage).
        warn_on_mismatch(_vectorstore, where="docs_search:vector_db_docs")
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

# ── BM25 lexical arm (zero-dependency Okapi BM25) ─────────────────────────────
# Semantic (e5-small cosine) is fuzzy on exact tokens: literal "Pasal 42",
# defined terms, article refs, proper nouns. For legal/UU/contract corpora the
# user very often queries by exact citation, which is precisely where a pure
# semantic vector is weakest. BM25 adds a dedicated LEXICAL channel; its ranking
# is fused with the semantic ranking via Reciprocal Rank Fusion (RRF). This is
# ADDITIVE — HyDE query expansion and entity-graph augmentation are untouched;
# BM25+RRF is a third, complementary retrieval channel.
#
# No new dependency: BM25 is ~40 lines of stdlib math over the same Chroma
# chunk corpus (single source of truth). The corpus is loaded once and cached,
# keyed on the Chroma collection count so it rebuilds only when the DB changes.
#
# Toggle: DOCS_BM25=0 disables the lexical arm entirely (semantic-only path,
# byte-identical to the pre-BM25 behavior). RRF constant via DOCS_RRF_K
# (default 60 — the near-universal literature default, needs no tuning).

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

_bm25_cache = None          # (count, docs, metas, idf, doc_len, avg_len, tf_list)

# ---------------------------------------------------------------------------
# BM25 disk cache — persistence across subprocesses.
#
# Each search CLI invocation (docs_search / hybrid_recall leg) is a FRESH
# subprocess, so the in-memory `_bm25_cache` global is always cold. Rebuilding
# BM25 over the full chunk store (~14k chunks) costs ~6s build + ~6s rank = ~12s,
# pushing a small-k query to ~14s — over the 15s memory_search timeout, so recall
# silently returns nothing while the DB itself is fine.
#
# Fix: persist the built index to disk keyed on (collection, chunk-count). Count
# is the existing invalidation signal — any ingest/rebuild changes it, auto-busting
# the cache. Warm queries load the pickle (sub-second) and skip the rebuild.
import pickle as _pickle
import hashlib as _bm25_hashlib
from pathlib import Path as _BM25Path

_BM25_CACHE_DIR = _BM25Path(
    os.environ.get("DINOMEM_BM25_CACHE_DIR")
    or (_BM25Path(__file__).resolve().parent.parent / "cache" / "bm25")
)

def _bm25_disk_path(count: int) -> _BM25Path:
    key = _bm25_hashlib.sha1(f"docs:{count}".encode()).hexdigest()[:16]
    return _BM25_CACHE_DIR / f"bm25_docs_{count}_{key}.pkl"

def _bm25_disk_load(count: int):
    p = _bm25_disk_path(count)
    try:
        if p.is_file():
            with open(p, "rb") as fh:
                data = _pickle.load(fh)
            if isinstance(data, tuple) and data and data[0] == count:
                return data
    except Exception:
        pass
    return None

def _bm25_disk_store(data) -> None:
    try:
        _BM25_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _bm25_disk_path(data[0])
        tmp = p.with_suffix(".pkl.tmp")
        with open(tmp, "wb") as fh:
            _pickle.dump(data, fh, protocol=_pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
        for old in _BM25_CACHE_DIR.glob("bm25_docs_*.pkl"):
            if old != p:
                try:
                    old.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _chunk_key(meta: dict) -> str:
    return f"{meta.get('source_file','')}::{meta.get('chunk_index',0)}"


def _build_bm25():
    """Build (or reuse cached) BM25 corpus from the Chroma chunk store.

    Cache invalidates on Chroma document count change (any ingest/rebuild). BM25
    over the full corpus is cheap here — thousands of chunks, precomputed idf +
    per-doc term frequencies, scored in pure Python at query time.
    """
    global _bm25_cache
    vs = _get_vectorstore()
    all_data = vs.get()
    docs = all_data.get("documents", []) or []
    metas = all_data.get("metadatas", []) or []
    count = len(docs)

    if _bm25_cache is not None and _bm25_cache[0] == count:
        return _bm25_cache

    # warm path: load prebuilt index from disk (survives subprocess boundary)
    disk = _bm25_disk_load(count)
    if disk is not None:
        _bm25_cache = disk
        return _bm25_cache

    tf_list: List[Dict[str, int]] = []
    df: Dict[str, int] = {}
    doc_len: List[int] = []
    for text in docs:
        toks = _tokenize(text)
        doc_len.append(len(toks))
        tf: Dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        tf_list.append(tf)
        for t in tf:
            df[t] = df.get(t, 0) + 1

    n = max(count, 1)
    avg_len = (sum(doc_len) / n) if count else 0.0
    # Okapi BM25 idf with +1 smoothing (always positive).
    idf = {t: math.log(1 + (n - dfi + 0.5) / (dfi + 0.5)) for t, dfi in df.items()}

    _bm25_cache = (count, docs, metas, idf, doc_len, avg_len, tf_list)
    _bm25_disk_store(_bm25_cache)
    return _bm25_cache


def _bm25_rank(query: str, top_n: int, k1: float = 1.5, b: float = 0.75) -> List[Tuple[str, dict, str, float]]:
    """Return top_n chunks by BM25 score as (chunk_key, metadata, content, score)."""
    try:
        count, docs, metas, idf, doc_len, avg_len, tf_list = _build_bm25()
    except Exception:
        return []
    if not count:
        return []
    q_terms = [t for t in _tokenize(query) if t in idf]
    if not q_terms:
        return []

    scored: List[Tuple[float, int]] = []
    for i in range(count):
        tf = tf_list[i]
        dl = doc_len[i] or 1
        s = 0.0
        for t in q_terms:
            f = tf.get(t)
            if not f:
                continue
            denom = f + k1 * (1 - b + b * (dl / (avg_len or 1)))
            s += idf[t] * (f * (k1 + 1)) / denom
        if s > 0.0:
            scored.append((s, i))

    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for s, i in scored[:top_n]:
        meta = metas[i] if i < len(metas) else {}
        out.append((_chunk_key(meta), meta, docs[i], s))
    return out


def _rrf_fuse(semantic: List[dict], lexical: List[Tuple[str, dict, str, float]],
              k: int, rrf_k: int = 60) -> List[dict]:
    """Reciprocal Rank Fusion of the semantic list (result dicts) and the BM25
    list. RRF score = sum over channels of 1/(rrf_k + rank). Parameter-free in
    practice: rrf_k=60 is the standard literature default and needs no tuning.

    Semantic result dicts are the canonical carriers (they hold the already-
    normalized metadata shape). A BM25-only hit (exact-token match the semantic
    arm missed — the whole point) is materialized into the same dict shape and
    appended so it can win on fusion.
    """
    fused: Dict[str, dict] = {}
    scores: Dict[str, float] = {}

    for rank, r in enumerate(semantic, start=1):
        ck = _chunk_key({
            'source_file': r['metadata'].get('source_file', ''),
            'chunk_index': r['metadata'].get('chunk_index', 0),
        })
        fused[ck] = r
        scores[ck] = scores.get(ck, 0.0) + 1.0 / (rrf_k + rank)

    for rank, (ck, meta, content, _bm) in enumerate(lexical, start=1):
        scores[ck] = scores.get(ck, 0.0) + 1.0 / (rrf_k + rank)
        if ck not in fused:
            fused[ck] = {
                'content': content,
                'score': 0.0,          # no semantic score; ranked via fusion
                'lexical_retrieved': True,
                'metadata': {
                    'doc_title': meta.get('title', ''),
                    'section_title': meta.get('section_title', ''),
                    'section_level': meta.get('section_level', 0),
                    'chunk_index': meta.get('chunk_index', 0),
                    'source_file': meta.get('source_file', ''),
                    'date': meta.get('date', ''),
                    'doc_type': meta.get('doc_type', 'document'),
                },
            }

    ordered = sorted(fused.keys(), key=lambda c: (-scores[c],))
    out = []
    for ck in ordered[:k]:
        item = fused[ck]
        item['rrf_score'] = round(scores[ck], 6)
        out.append(item)
    return out


# ── Search ────────────────────────────────────────────────────────────────────
def _hyde_expand(query: str) -> str:
    """Generate a hypothetical document passage for HyDE query expansion.

    Default ON (improves recall on formal/long-form docs). Optimized so the
    expensive LLM call is not repeated for the same query:
      - Persistent disk cache (24h TTL) keyed on the query — HyDE expansion of a
        given question is stable, so a repeat query is a cache hit (no LLM call,
        no 15s latency).
      - Falls back silently to the original query if the LLM/gateway is
        unavailable, so it never breaks search on a fresh machine.
    """
    HYDE_TTL = 86400  # 24h — expansion is stable per query
    cached_val = cache_get("docs_hyde", query, ttl_seconds=HYDE_TTL)
    if cached_val:
        return cached_val
    # Timeout is generous (default 25s) and env-overridable: a too-tight timeout
    # is worst-of-both — it pays the wait AND loses the expansion. A successful
    # expansion is cached for 24h, so the cost is paid once per unique query.
    # Set DOCS_HYDE_TIMEOUT=0 to disable HyDE entirely (instant, recall-only).
    try:
        hyde_timeout = float(os.environ.get('DOCS_HYDE_TIMEOUT', '25'))
    except Exception:
        hyde_timeout = 25.0
    if hyde_timeout <= 0:
        return query
    expanded = query
    try:
        import subprocess, shutil
        openclaw_bin = shutil.which('openclaw') or '/home/linuxbrew/.linuxbrew/bin/openclaw'
        prompt = (
            f'Write a short factual passage (2-3 sentences) that would directly answer '
            f'this question, as if from a formal document or legal text: {query}'
        )
        # COST TIER (base T2): HyDE is a throwaway no-reasoning generation -> pin the
        # CHEAP model, never the gateway default (which is the main/reasoning T3 model).
        # Resolve via the suite's single cheap-model anchor (procedures/_cheap_model.py):
        # DINOMEM_CHEAP_MODEL env -> compaction anchor -> haiku fallback.
        _hyde_model = ''
        try:
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), '..', 'procedures'))
            from _cheap_model import cheap_model as _cm
            _hyde_model = (_cm() or '').strip()
        except Exception:
            _hyde_model = os.environ.get('DINOMEM_CHEAP_MODEL', '').strip()
        _cmd = [openclaw_bin, 'capability', 'model', 'run', '--prompt', prompt, '--gateway', '--json']
        if _hyde_model:
            _cmd += ['--model', _hyde_model]
        result = subprocess.run(
            _cmd,
            capture_output=True, text=True, timeout=hyde_timeout
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data.get('ok') and data.get('outputs'):
                expanded = data['outputs'][0].get('text', query).strip() or query
    except Exception:
        expanded = query
    # Only cache successful expansions; don't cache fallbacks (so a transient
    # gateway outage doesn't pin the un-expanded query for 24h).
    if expanded and expanded != query:
        cache_set("docs_hyde", query, result=expanded)
    return expanded


def docs_search(
    query: str,
    k: int = 5,
    doc_filter: Optional[str] = None,
    section_filter: Optional[str] = None,
    max_chars: Optional[int] = None,
    hyde: bool = True,
    graph: bool = True,
) -> List[Dict]:
    """
    Semantic search over indexed long-form documents.

    Args:
        query:          Search query (natural language)
        k:              Number of results (default 5)
        doc_filter:     Filter by document title/filename substring (case-insensitive)
        section_filter: Filter by section title substring (case-insensitive)
        max_chars:      Truncate chunk content to N chars (None = no limit)
        graph:          Augment results with entity-graph traversal (default True)

    Returns:
        List of dicts: {content, score, metadata: {doc_title, section_title,
                        section_level, chunk_index, source_file, date, doc_type}}
        Graph-retrieved results include graph_retrieved=True and graph_overlap count.

    Example:
        docs_search("force majeure", k=5)
        docs_search("pasal 1", doc_filter="uu-cipta-kerja", k=3)
        docs_search("entity", graph=False)  # skip graph traversal
    """
    # HyDE: expand query with hypothetical document passage for better recall
    search_query = _hyde_expand(query) if hyde else query

    # Fetch depth: normally k*4. When a doc/section filter is active, the filter
    # is applied POST-fetch, so a narrow filter can starve the result set below
    # k even though matching chunks exist deeper in the ranking. In that case we
    # progressively widen the fetch (up to a cap) so filtered queries stay
    # reliable on edge cases, without paying the cost on unfiltered queries.
    has_filter = bool(doc_filter or section_filter)
    fetch_k = k * 4
    FETCH_CAP = 400

    def _collect(raw):
        out = []
        for item in raw:
            meta = item['metadata']
            if doc_filter:
                title = meta.get('title', '').lower()
                src = meta.get('source_file', '').lower()
                if doc_filter.lower() not in title and doc_filter.lower() not in src:
                    continue
            if section_filter:
                section = meta.get('section_title', '').lower()
                if section_filter.lower() not in section:
                    continue
            content = item['page_content']
            if max_chars:
                content = content[:max_chars]
            out.append({
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
            if len(out) >= k:
                break
        return out

    ck = _cache_key(query, fetch_k, doc_filter, section_filter)  # cache key uses original query
    raw = _cached_search(ck, search_query, fetch_k)
    results = _collect(raw)

    # Edge case: filtered query came up short. Widen fetch and retry until we
    # either fill k results or hit the fetch cap / exhaust the corpus.
    while has_filter and len(results) < k and fetch_k < FETCH_CAP:
        fetch_k = min(fetch_k * 2, FETCH_CAP)
        ck = _cache_key(query, fetch_k, doc_filter, section_filter)
        raw_wider = _cached_search(ck, search_query, fetch_k)
        results = _collect(raw_wider)
        if len(raw_wider) < fetch_k:
            break  # corpus exhausted — no more chunks to fetch

    # ── BM25 lexical arm + RRF fusion (additive; HyDE/graph untouched) ────────
    # BM25 runs on the ORIGINAL query (literal tokens), not the HyDE'd prose —
    # the lexical channel exists precisely to catch exact citations/terms the
    # semantic vector blurs. Fuse the two rankings via RRF, then hand off to the
    # entity-graph expansion exactly as before. Disable with DOCS_BM25=0.
    bm25_on = os.environ.get('DOCS_BM25', '1') not in ('0', 'false', 'False', '')
    if bm25_on:
        try:
            rrf_k = int(os.environ.get('DOCS_RRF_K', '60'))
        except Exception:
            rrf_k = 60
        # Over-fetch the lexical arm so RRF has real depth to fuse against.
        lex_raw = _bm25_rank(query, top_n=max(k * 4, 20))
        # Apply the same doc/section filters to lexical hits (filters are a hard
        # constraint, not a ranking signal).
        lex = []
        for lk, meta, content, score in lex_raw:
            if doc_filter:
                title = meta.get('title', '').lower()
                src = meta.get('source_file', '').lower()
                if doc_filter.lower() not in title and doc_filter.lower() not in src:
                    continue
            if section_filter:
                if section_filter.lower() not in meta.get('section_title', '').lower():
                    continue
            if max_chars:
                content = content[:max_chars]
            lex.append((lk, meta, content, score))
        if lex:
            results = _rrf_fuse(results, lex, k=k, rrf_k=rrf_k)

    # Graph-augmented retrieval: find related chunks via entity/relation edges
    if graph and results:
        results = _graph_expand(results, k=k)

    return results

# ── Graph-augmented retrieval ────────────────────────────────────────────────
DOCS_ENTITY_GRAPH = WORKSPACE / "kb" / "docs_entity_graph.json"

_entity_graph_cache: Optional[Dict] = None
_entity_graph_mtime: float = 0.0

def _load_entity_graph() -> Optional[Dict]:
    """Load docs_entity_graph.json with mtime-based cache invalidation."""
    global _entity_graph_cache, _entity_graph_mtime
    if not DOCS_ENTITY_GRAPH.exists():
        return None
    try:
        mtime = DOCS_ENTITY_GRAPH.stat().st_mtime
        if _entity_graph_cache is not None and mtime == _entity_graph_mtime:
            return _entity_graph_cache
        with open(DOCS_ENTITY_GRAPH) as f:
            _entity_graph_cache = json.load(f)
        _entity_graph_mtime = mtime
        return _entity_graph_cache
    except Exception:
        return None

def _graph_expand(results: List[Dict], k: int) -> List[Dict]:
    """Traverse docs_entity_graph to find related chunks not in top-k results.

    Strategy:
    1. Collect entities mentioned in top-k result chunks.
    2. Find other chunks that share those entities (entity_in_chunk edges).
    3. Score candidate chunks by entity overlap count.
    4. Append top candidates (not already in results) up to k extra chunks.
    """
    graph = _load_entity_graph()
    if not graph:
        return results

    chunk_entity_map = graph.get("chunk_entity_map", {})

    # Build chunk_key → source_file + chunk_index mapping from results
    result_chunk_keys: set = set()
    for r in results:
        src = r["metadata"].get("source_file", "")
        idx = r["metadata"].get("chunk_index", 0)
        if src:
            result_chunk_keys.add(f"{src}::{idx}")

    # Collect entity names from result chunks
    result_entities: set = set()
    for ck in result_chunk_keys:
        entry = chunk_entity_map.get(ck, {})
        for ent in entry.get("entities", []):
            result_entities.add(ent.lower())

    if not result_entities:
        return results

    # Score all other chunks by entity overlap
    candidate_scores: Dict[str, int] = {}
    for ck, entry in chunk_entity_map.items():
        if ck in result_chunk_keys:
            continue
        overlap = sum(
            1 for ent in entry.get("entities", [])
            if ent.lower() in result_entities
        )
        if overlap > 0:
            candidate_scores[ck] = overlap

    if not candidate_scores:
        return results

    # Sort candidates by overlap score descending; take top k
    top_candidates = sorted(candidate_scores, key=lambda x: -candidate_scores[x])[:k]

    # Fetch candidate chunk content from vectorstore
    vs = _get_vectorstore()
    all_data = vs.get()
    docs_content = all_data.get("documents", [])
    docs_meta = all_data.get("metadatas", [])

    # Build lookup: chunk_key → (content, metadata)
    chunk_lookup: Dict[str, tuple] = {}
    for content, meta in zip(docs_content, docs_meta):
        src = meta.get("source_file", "")
        idx = meta.get("chunk_index", 0)
        ck = f"{src}::{idx}"
        if ck not in chunk_lookup:
            chunk_lookup[ck] = (content, meta)

    # Build graph-augmented results
    graph_results = []
    for ck in top_candidates:
        if ck not in chunk_lookup:
            continue
        content, meta = chunk_lookup[ck]
        graph_results.append({
            "content": content,
            "score": 0.0,  # graph-retrieved, no vector score
            "graph_retrieved": True,
            "graph_overlap": candidate_scores[ck],
            "metadata": {
                "doc_title": meta.get("title", ""),
                "section_title": meta.get("section_title", ""),
                "section_level": meta.get("section_level", 0),
                "chunk_index": meta.get("chunk_index", 0),
                "source_file": meta.get("source_file", ""),
                "date": meta.get("date", ""),
                "doc_type": meta.get("doc_type", "document"),
            }
        })

    return results + graph_results

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

        try:  # retrieval observability (fail-open)
            sys.path.insert(0, str(WORKSPACE / "procedures"))
            from _retrieval_log import log_retrieval
            log_retrieval(
                WORKSPACE, "docs_search", args.query, k=args.k,
                result_uris=[(r.get("metadata") or {}).get("source_file")
                             or (r.get("metadata") or {}).get("doc_title") for r in results],
                top_scores=[r.get("score") for r in results],
            )
        except Exception:
            pass

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
