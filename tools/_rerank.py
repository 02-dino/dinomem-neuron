"""
_rerank.py — cross-encoder reranking primitive (TEI /rerank), fail-open.

WHY IT EXISTS: the dense leg over the native memory index returns a near-FLAT
score band (measured 2026-10-20, analyst corpus: 10 hits spanning cosine
0.856-0.864, spread 0.0070-0.0113). hybrid_recall's _normalize_within_leg
min-max stretches that 0.008 of noise across a full 0-1 axis, so the ordering
it produces is noise wearing a score. A cross-encoder scores (query, passage)
JOINTLY instead of comparing two independently-embedded vectors, and on the
SAME candidates produced spread 0.31/0.50/0.68 — a real ranking signal.

CONTRACT: rerank_scores() returns scores ALIGNED to the input list, or None.
None means "no opinion" and the caller MUST keep its existing ordering. Every
failure mode (reranker down, timeout, bad payload, flat output) returns None,
never a partial or reshuffled list. Precision only: a cross-encoder reorders
what retrieval already found and can never surface a passage the leg missed.

THE NON-OBVIOUS GOTCHA — two guards that exist because of measurements:
  1. BATCH COST IS SUPERLINEAR IN BOTH DIMENSIONS. 8 texts x 400 chars = 2.3s
     but 20 x 2000 = 19-24s on this box. Capping only the count is not enough;
     both count AND per-text length are capped, or a 1.2s warm recall grows a
     20s tail.
  2. ms-marco-MiniLM IS AN ENGLISH MODEL. Controlled decoy test: EN query vs EN
     corpus -> top score 0.4263 (correct order); ID vs ID -> 0.1919 (correct
     order); EN query vs ID corpus -> spread 0.0002, i.e. signal gone. A
     collapsed spread is indistinguishable from "all equally irrelevant", so
     MIN_SPREAD returns None there rather than committing to a confident random
     permutation. Degrading to the old ordering is strictly safer than trusting
     noise with a new label on it.

OPTIONAL INFRA: the TEI reranker (docker-compose.tei-rerank.yml in dinomem base)
may simply not be running. That is a no-op, not an error.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Knobs. Defaults are the measured-safe operating point, not guesses.
_DEF_URL = "http://localhost:8081"
MAX_TEXTS = int(os.environ.get("DINOMEM_RERANK_MAX_TEXTS", "8"))
MAX_CHARS = int(os.environ.get("DINOMEM_RERANK_MAX_CHARS", "400"))
# MEASURED, not guessed: 8x400 costs 2.2-3.0s on a loaded box (load ~14 on 8
# cores). An earlier 2.5s default sat INSIDE that band, so real recalls silently
# timed out and fail-open returned the unreranked order while the selftest --
# which used 3 tiny English strings -- passed. The budget must clear the slowest
# measured batch with headroom, and the selftest must exercise a real-sized one.
TIMEOUT = float(os.environ.get("DINOMEM_RERANK_TIMEOUT", "6.0"))
# Health probe is a separate, much tighter budget: an ABSENT reranker must cost
# ~nothing, and /health never approaches inference latency.
HEALTH_TIMEOUT = float(os.environ.get("DINOMEM_RERANK_HEALTH_TIMEOUT", "1.5"))
# Below this top-to-bottom gap the model has no opinion (see gotcha 2).
MIN_SPREAD = float(os.environ.get("DINOMEM_RERANK_MIN_SPREAD", "0.01"))

_health: bool | None = None  # per-process memo; a down container stays down


def _base() -> str:
    return os.environ.get("DINOMEM_RERANK_URL", _DEF_URL).rstrip("/")


def enabled() -> bool:
    return os.environ.get("DINOMEM_RERANK", "1") != "0"


def available() -> bool:
    """One health probe per process. WHY memoized: the common failure is 'no
    reranker installed', and re-probing a dead port on every call would add the
    timeout to every single recall."""
    global _health
    if _health is not None:
        return _health
    if not enabled():
        _health = False
        return _health
    try:
        with urllib.request.urlopen(_base() + "/health", timeout=HEALTH_TIMEOUT) as r:
            _health = r.status == 200
    except Exception:
        _health = False
    return _health


def rerank_scores(query: str, texts: list[str]) -> list[float] | None:
    """Cross-encoder scores aligned 1:1 with `texts`, or None for no-opinion.

    Only the first MAX_TEXTS entries are scored (cost guard); the caller keeps
    its own ordering for the tail, which is why alignment — not a sorted list —
    is the return contract.
    """
    if not texts or not query or not available():
        return None
    head = texts[:MAX_TEXTS]
    payload = {"query": query[:MAX_CHARS * 2],
               "texts": [(t or "")[:MAX_CHARS] for t in head],
               "truncate": True}
    try:
        req = urllib.request.Request(
            _base() + "/rerank",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None

    scores = [0.0] * len(head)
    for item in data:
        try:
            idx = int(item["index"])
            if 0 <= idx < len(head):
                scores[idx] = float(item.get("score", 0.0))
        except Exception:
            return None  # malformed payload: no partial trust
    if max(scores) - min(scores) < MIN_SPREAD:
        return None  # collapsed signal (cross-language or all-irrelevant)
    # Tail keeps a score below the scored block so it never jumps the rerank.
    return scores + [min(scores) * 0.5] * (len(texts) - len(head))


def rerank(query: str, results: list[dict], top_k: int = 5) -> list[dict]:
    """Back-compat wrapper: sorted+truncated dicts, `rerank_score` stamped on.
    Kept because procedures/memory_rerank.py (semantic_search) consumes this
    shape; new callers should prefer rerank_scores()."""
    if not results:
        return results
    texts = [r.get("content") or r.get("text") or "" for r in results]
    scores = rerank_scores(query, texts)
    if scores is None:
        return results[:top_k]
    order = sorted(range(len(results)), key=lambda i: scores[i], reverse=True)
    out = []
    for i in order[:top_k]:
        d = dict(results[i])
        d["rerank_score"] = round(scores[i], 4)
        out.append(d)
    return out


def _selftest() -> int:
    """Proves BOTH paths: a real rerank when the container is up, and clean
    no-opinion when it is not. Exits 0 either way — an absent reranker is a
    supported configuration, so failing here would be wrong."""
    print(f"reranker url={_base()} enabled={enabled()} available={available()}")
    print(f"caps: max_texts={MAX_TEXTS} max_chars={MAX_CHARS} "
          f"timeout={TIMEOUT}s min_spread={MIN_SPREAD}")
    if not available():
        print("OK: reranker absent -> rerank_scores() returns None (no-op path)")
        assert rerank_scores("q", ["a", "b"]) is None
        return 0

    q = "what is the rule about owner language and reply style"
    docs = ["Owner prefers Indonesian, concise replies under 2000 characters.",
            "Bitcoin funding rate turned negative on Binance perpetuals.",
            "Run rugcheck before calling a microcap clean."]
    s = rerank_scores(q, docs)
    if s is None:
        print("FAIL: live reranker but no scores for an unambiguous query")
        return 1
    print(f"  scores={[round(x, 4) for x in s]}")
    if s.index(max(s)) != 0:
        print("FAIL: correct doc did not win")
        return 1
    print("OK: correct doc ranked first")

    # Edge: alignment must hold when the input exceeds MAX_TEXTS.
    many = docs * 5
    s2 = rerank_scores(q, many)
    if s2 is not None and len(s2) != len(many):
        print(f"FAIL: misaligned ({len(s2)} vs {len(many)})")
        return 1
    print(f"OK: alignment held over cap ({len(many)} in, "
          f"{len(s2) if s2 else 0} out)")

    # REGRESSION GUARD for the bug this file shipped once: a FULL-SIZE batch
    # (MAX_TEXTS x MAX_CHARS) must complete inside TIMEOUT. Toy inputs hide a
    # too-tight budget, because fail-open makes a timeout look like success.
    import time as _t
    full = [("lorem ipsum dolor sit amet " * 40)[:MAX_CHARS]
            for _ in range(MAX_TEXTS)]
    full[0] = docs[0]
    t0 = _t.time()
    s3 = rerank_scores(q, full)
    el = _t.time() - t0
    print(f"  full batch {MAX_TEXTS}x{MAX_CHARS}: {el:.2f}s "
          f"(budget {TIMEOUT}s) -> {'scores' if s3 else 'None'}")
    if s3 is None:
        print("FAIL: full-size batch returned None -- TIMEOUT too tight for "
              "this box; raise DINOMEM_RERANK_TIMEOUT")
        return 1
    print("OK: full-size batch inside budget")

    # Edge: empty + disabled paths must be silent no-ops.
    assert rerank_scores(q, []) is None and rerank_scores("", docs) is None
    print("OK: empty input -> None")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest() if "--selftest" in sys.argv else 0)
