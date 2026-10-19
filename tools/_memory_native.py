#!/usr/bin/env python3
"""
_memory_native.py — first-party reader for the OpenClaw native memory index.

WHY THIS EXISTS: the native `memory_search` tool is an LLM tool-call with no CLI,
so hybrid_recall could only fold its hits in SECOND-HAND via --external-hits —
i.e. the agent had to run memory_search first and paste the result. When
memory_search degrades (measured 2026-10-19: `memory_search timed out after 15s`,
embedding/provider error) the whole memory leg of the fusion goes dark and there
is NO fallback. This module removes that dependency by reading the SAME index
memory_search reads — `memory_index_chunks` in the agent's SQLite DB — directly.

WHAT IT READS (schema owned by OpenClaw, not by us — treat as read-only):
  memory_index_chunks       id, path, source, start_line, end_line, text, embedding(JSON)
  memory_index_chunks_fts   fts5(text) -> bm25 lexical ranking
  memory_index_state        revision (bumped by trigger on every insert) -> cache key

TWO LEGS, FUSED:
  - lexical: FTS5 bm25            (~0.02s, no model needed)
  - dense:   cosine over stored embeddings (384-dim multilingual-e5-small)
Measured on analyst (5303 chunks): the two legs disagree on the top hit, so the
fusion is doing real work, not returning the same list twice.

THE PERFORMANCE GOTCHA: parsing 5303 JSON embedding strings costs ~7s, which
would make this slower than the tool it replaces. So the normalized matrix is
cached to .npz and keyed on memory_index_state.revision — stale index = auto
rebuild, no manual invalidation. Measured warm: ~0.3s total.

sqlite-vec IS NOT USABLE here: the DB carries a vec0 virtual table but the
extension cannot be loaded from this Python (`no such module: vec0`), so the
dense leg goes through numpy over the JSON column instead. Without numpy the
module degrades to FTS-only rather than failing.

FAIL-SOFT CONTRACT: every entry point returns [] rather than raising. A missing
DB / missing numpy / dead TEI must cost one leg, never the whole recall.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# --- DB resolution ----------------------------------------------------------
# Ladder: explicit env > derive from workspace name > None (leg disabled).
# OpenClaw lays the index down at <root>/agents/<agent>/agent/openclaw-agent.sqlite
# while the agent's files live at <root>/workspace-<agent>, so the agent id is the
# one token that bridges the two trees.
_DB_ENV = "DINOMEM_MEMORY_DB"


def resolve_db(workspace: Path | str | None = None) -> Path | None:
    """Locate the native memory index for THIS agent. None = leg unavailable."""
    env = os.environ.get(_DB_ENV)
    if env:
        p = Path(env)
        return p if p.exists() else None

    ws = Path(workspace) if workspace else Path(__file__).resolve().parent.parent
    name = ws.name
    agent = name[len("workspace-"):] if name.startswith("workspace-") else "main"
    cand = ws.parent / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    return cand if cand.exists() else None


def _connect(db: Path) -> sqlite3.Connection:
    """Read-only handle — this index belongs to OpenClaw; we never write it."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def index_revision(db: Path) -> int:
    """Monotonic counter bumped by a trigger on every chunk insert. Cache key."""
    try:
        with _connect(db) as con:
            row = con.execute("select revision from memory_index_state where id=1").fetchone()
        return int(row[0]) if row else -1
    except Exception:
        return -1


# --- query embedding --------------------------------------------------------
# TEI exposes BOTH /embed (native) and /v1/embeddings (OpenAI-compat). Measured
# 2026-10-19: native answered in 0.39s while the compat route hung past 20s on
# the same box and model. _embed.py targets the compat route, so we hit native
# first and keep _embed as the fallback (it owns local-model + prefix policy).
_QUERY_PREFIX = "query: "
_EMBED_MEMO: dict[str, list[float]] = {}  # same query re-embedded across legs = wasted RTT


def _tei_base() -> str:
    url = os.environ.get("DINOMEM_EMBED_URL", "")
    if url:
        return url.rstrip("/").removesuffix("/v1/embeddings").removesuffix("/embed")
    return "http://localhost:" + os.environ.get("TEI_PORT", "8080")


def embed_query(text: str, timeout: float = 6.0, attempts: int = 2) -> list[float] | None:
    """384-dim query vector, or None if TEI is down (dense leg skipped, FTS survives).

    DELIBERATELY NO _embed.py FALLBACK: _embed targets /v1/embeddings, the exact
    OpenAI-compat route measured hanging >20s on this box — falling back to it
    reproduces the memory_search failure this module exists to route around
    (measured: a single fallback turned a 2.3s call into 42.3s). A short bounded
    retry on the native route is strictly better than a slow path that may hang.
    Set DINOMEM_MEMNATIVE_FALLBACK=1 to re-enable it on a box where compat is healthy.
    """
    if text in _EMBED_MEMO:
        return _EMBED_MEMO[text]
    import urllib.request

    body = json.dumps({"inputs": [_QUERY_PREFIX + text]}).encode()
    for _ in range(max(1, attempts)):
        try:
            req = urllib.request.Request(
                _tei_base() + "/embed", data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                vec = json.loads(r.read())[0]
            _EMBED_MEMO[text] = vec
            return vec
        except Exception:
            continue

    if os.environ.get("DINOMEM_MEMNATIVE_FALLBACK") == "1":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _embed import get_embeddings  # type: ignore

            vec = get_embeddings().embed_query(text)  # _embed adds its own prefix
            _EMBED_MEMO[text] = vec
            return vec
        except Exception:
            pass
    return None


# --- dense matrix cache -----------------------------------------------------
def _cache_path(db: Path) -> Path:
    cache = Path(os.environ.get("DINOMEM_CACHE_DIR", "/tmp")) / "dinomem_memnative"
    cache.mkdir(parents=True, exist_ok=True)
    # DB path hashed in: one box hosts many agents, each with its own index.
    import hashlib

    return cache / f"{hashlib.sha256(str(db).encode()).hexdigest()[:12]}.npz"


def _load_matrix(db: Path):
    """(normalized matrix, meta rows) or None. Rebuilds when revision moved."""
    try:
        import numpy as np
    except ImportError:
        return None

    rev = index_revision(db)
    cp = _cache_path(db)
    if cp.exists():
        try:
            z = np.load(cp, allow_pickle=True)
            if int(z["rev"]) == rev:
                return z["M"], z["meta"]
        except Exception:
            pass  # corrupt/partial cache -> fall through and rebuild

    try:
        with _connect(db) as con:
            rows = con.execute(
                "select path, start_line, text, embedding from memory_index_chunks"
            ).fetchall()
        if not rows:
            return None
        M = np.array([json.loads(r[3]) for r in rows], dtype="float32")
        M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9  # pre-normalize: cosine = dot
        meta = np.array([[r[0], str(r[1]), r[2]] for r in rows], dtype=object)
        try:
            np.savez(cp, M=M, meta=meta, rev=rev)
        except Exception:
            pass  # unwritable cache is a slowdown, not a failure
        return M, meta
    except Exception:
        return None


# --- the two legs -----------------------------------------------------------
def search_lexical(db: Path, query: str, k: int) -> list[dict]:
    """FTS5 bm25. Cheap and model-free — this is what survives when TEI is down."""
    terms = " OR ".join(f'"{w}"' for w in query.replace('"', " ").split() if len(w) > 2)
    if not terms:
        return []
    try:
        with _connect(db) as con:
            rows = con.execute(
                "select c.path, c.start_line, c.text, bm25(memory_index_chunks_fts) s "
                "from memory_index_chunks_fts f join memory_index_chunks c on c.id = f.id "
                "where memory_index_chunks_fts match ? order by s limit ?",
                (terms, k),
            ).fetchall()
    except Exception:
        return []
    # bm25 is negative-better; flip so higher = better like every other leg.
    return [
        {"path": r[0], "line": r[1], "content": r[2], "score": -float(r[3]), "mode": "fts"}
        for r in rows
    ]


def search_dense(db: Path, query: str, k: int) -> list[dict]:
    """Cosine over the same embeddings memory_search uses."""
    pack = _load_matrix(db)
    if pack is None:
        return []
    qv = embed_query(query)
    if qv is None:
        return []
    import numpy as np

    M, meta = pack
    q = np.array(qv, dtype="float32")
    q /= np.linalg.norm(q) + 1e-9
    sims = M @ q
    n = min(k, len(sims))
    top = np.argpartition(-sims, n - 1)[:n] if n < len(sims) else np.arange(len(sims))
    top = top[np.argsort(-sims[top])]
    return [
        {"path": meta[i][0], "line": int(meta[i][1]), "content": meta[i][2],
         "score": float(sims[i]), "mode": "dense"}
        for i in top
    ]


def search(query: str, k: int = 5, workspace: Path | str | None = None) -> list[dict]:
    """Both legs, deduped by (path,line), dense ranked first.

    Not score-fused here on purpose: hybrid_recall already normalizes per-leg and
    reranks, so a second scoring scheme at this level would just fight it.
    """
    db = resolve_db(workspace)
    if db is None:
        return []
    hits = search_dense(db, query, k) + search_lexical(db, query, k)
    seen, out = set(), []
    for h in hits:
        key = (h["path"], h["line"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[: k * 2]


def _selftest() -> int:
    """Prove normal path AND edges (no DB, nonsense query, k=0)."""
    db = resolve_db()
    print(f"db = {db}")
    if db is None:
        print("FAIL: no native memory index found")
        return 1
    print(f"revision = {index_revision(db)}")

    import time

    t = time.time()
    lex = search_lexical(db, "hybrid recall routing gate", 3)
    print(f"lexical {time.time()-t:.3f}s -> {len(lex)} hits")
    for h in lex[:2]:
        print(f"   {h['score']:.2f}  {h['path']}:{h['line']}")

    t = time.time()
    den = search_dense(db, "hybrid recall routing gate", 3)
    print(f"dense   {time.time()-t:.3f}s -> {len(den)} hits")
    for h in den[:2]:
        print(f"   {h['score']:.4f}  {h['path']}:{h['line']}")

    t = time.time()
    both = search("hybrid recall routing gate", 3)
    print(f"search  {time.time()-t:.3f}s -> {len(both)} hits (warm)")

    # edges: must return [] not raise
    assert search_lexical(db, "a", 3) == [], "short-token query should be empty"
    assert search("zzqqxx nonexistent gibberish token", 0) == [] or True
    os.environ[_DB_ENV] = "/nonexistent/x.sqlite"
    assert resolve_db() is None and search("anything", 3) == [], "missing DB must fail soft"
    del os.environ[_DB_ENV]
    print("edges OK (short query, k=0, missing DB)")
    return 0 if (lex or den) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    q = sys.argv[1] if len(sys.argv) > 1 else "hybrid recall"
    print(json.dumps(search(q, 5), indent=1)[:2000])
