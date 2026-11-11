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
  2. THE MODEL CAN HAVE NO OPINION. Measured on the OLD model
     (cross-encoder/ms-marco-MiniLM-L-6-v2, ENGLISH-ONLY): EN query vs EN
     corpus -> top score 0.4263 (correct order); ID vs ID -> 0.1919 (correct
     order); EN query vs ID corpus -> spread 0.0002, i.e. signal gone. The base
     compose now ships BAAI/bge-reranker-v2-m3 (multilingual, 2026-11-11),
     which closes the language gap, but the guard stays: a collapsed spread is
     indistinguishable from "all equally irrelevant", so MIN_SPREAD returns
     None there rather than committing to a confident random permutation.

FALLBACK LADDER (2026-11-11): TEI cross-encoder -> LLM listwise rerank on the
agent's PRIMARY model (agents.defaults.model.primary in openclaw.json) -> None.
The LLM leg is OPT-IN (DINOMEM_RERANK_LLM_FALLBACK=1). WHY opt-in: bge-v2-m3
takes minutes to warm up on CPU, and a default-on fallback would spend a ~35s
primary-model call (measured) on EVERY recall during that window for what is
otherwise a 2s operation. WHY the primary and not the cheap/compaction tier: ordering
passages against a query is a judgement task; the cheap tier exists for
throwaway generation (HyDE, extraction).

OPTIONAL INFRA: the TEI reranker (docker-compose.tei-rerank.yml in dinomem base)
may simply not be running. Without the LLM fallback that is a no-op, not an error.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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

# LLM listwise fallback. Off by default (see FALLBACK LADDER in the header).
LLM_FALLBACK = os.environ.get("DINOMEM_RERANK_LLM_FALLBACK", "0") == "1"
# MEASURED 2026-11-11 (load ~14 on 8 cores): a trivial `openclaw infer model
# run --gateway` round-trip is 34s wall, 21s of it the Node CLI booting. A 30s
# default sat INSIDE that band and the selftest failed -- same trap as the TEI
# TIMEOUT above. Must clear the slowest measured call with headroom.
LLM_TIMEOUT = float(os.environ.get("DINOMEM_RERANK_LLM_TIMEOUT", "60"))
# DINOMEM_RERANK_LLM_MODEL pins a model; empty -> openclaw.json primary.
_LLM_MODEL_ENV = "DINOMEM_RERANK_LLM_MODEL"

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


def _primary_model() -> str:
    """agents.defaults.model.primary from the openclaw.json THIS agent runs on.
    Path comes from procedures/_cheap_model.config_path() so the multi-instance
    OPENCLAW_CONFIG_PATH fix lives in exactly one place (see that module)."""
    env = os.environ.get(_LLM_MODEL_ENV, "").strip()
    if env:
        return env
    try:
        import importlib.util as _ilu
        hp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "procedures", "_cheap_model.py")
        spec = _ilu.spec_from_file_location("_cheap_model", hp)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with open(mod.config_path()) as f:
            cfg = json.load(f)
        m = cfg.get("agents", {}).get("defaults", {}).get("model", {})
        return (m.get("primary") if isinstance(m, dict) else m) or ""
    except Exception:
        return ""


def _openclaw_bin() -> str:
    b = shutil.which("openclaw")
    if b:
        return b
    for c in ("/home/linuxbrew/.linuxbrew/bin/openclaw",
              "/usr/local/bin/openclaw", "/usr/bin/openclaw"):
        if os.path.exists(c):
            return c
    return "openclaw"


def llm_rerank_scores(query: str, texts: list[str]) -> list[float] | None:
    """Listwise LLM rerank: the model emits a JSON array of indices, best first,
    mapped to descending scores so the return shape matches TEI's. Same
    contract: aligned to `texts` or None. A malformed / partial answer is None,
    never a half-trusted permutation."""
    if not texts or not query:
        return None
    model = _primary_model()
    if not model:
        return None
    n = len(texts)
    passages = "\n".join(f"[{i}] {(t or '')[:MAX_CHARS].replace(chr(10), ' ')}"
                         for i, t in enumerate(texts))
    prompt = (
        "Rank the passages by relevance to the query, most relevant first. "
        f"Reply with ONLY a JSON array of all {n} passage indices, no prose.\n\n"
        f"Query: {query[:MAX_CHARS * 2]}\n\nPassages:\n{passages}"
    )
    cmd = [_openclaw_bin(), "infer", "model", "run", "--prompt", prompt,
           "--gateway", "--json", "--model", model, "--thinking", "off"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=LLM_TIMEOUT)
        if r.returncode != 0:
            return None
        out = json.loads(r.stdout)
        text = (out.get("outputs") or [{}])[0].get("text", "") if out.get("ok") else ""
        start, end = text.find("["), text.rfind("]")
        order = [int(i) for i in json.loads(text[start:end + 1])] if 0 <= start < end else None
    except Exception:
        return None
    if order is None or sorted(order) != list(range(n)):
        return None  # missing/duplicate/out-of-range index: no partial trust
    scores = [0.0] * n
    for rank, idx in enumerate(order):
        scores[idx] = 1.0 - rank / n
    return scores


def rerank_scores(query: str, texts: list[str]) -> list[float] | None:
    """Cross-encoder scores aligned 1:1 with `texts`, or None for no-opinion.

    Only the first MAX_TEXTS entries are scored (cost guard); the caller keeps
    its own ordering for the tail, which is why alignment — not a sorted list —
    is the return contract.

    Ladder: TEI -> (opt-in) LLM listwise on the primary model -> None.
    """
    if not texts or not query:
        return None
    head = texts[:MAX_TEXTS]
    scores = _tei_scores(query, head)
    if scores is None and LLM_FALLBACK:
        scores = llm_rerank_scores(query, head)
    if scores is None:
        return None
    # Tail keeps a score below the scored block so it never jumps the rerank.
    return scores + [min(scores) * 0.5] * (len(texts) - len(head))


def _tei_scores(query: str, head: list[str]) -> list[float] | None:
    if not available():
        return None
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
    return scores


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
          f"timeout={TIMEOUT}s min_spread={MIN_SPREAD} "
          f"llm_fallback={LLM_FALLBACK} llm_model={_primary_model() or '-'}")
    q = "what is the rule about owner language and reply style"
    docs = ["Owner prefers Indonesian, concise replies under 2000 characters.",
            "The vendor contract renews in March at the old rate.",
            "Run the staging smoke test before promoting a release."]
    if not available():
        if LLM_FALLBACK:
            s = rerank_scores(q, docs)
            if s is None or s.index(max(s)) != 0:
                print(f"FAIL: TEI absent, LLM fallback on, but scores={s}")
                return 1
            print(f"OK: TEI absent -> LLM fallback ranked correct doc first "
                  f"{[round(x, 3) for x in s]}")
        else:
            print("OK: reranker absent -> rerank_scores() returns None (no-op path)")
            assert rerank_scores("q", ["a", "b"]) is None
        return 0

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
