#!/usr/bin/env python3
"""
hybrid_recall.py — unified FUZZY retrieval fusion layer for dinomem-neuron.

WHAT: routes a recall query across the dinomem FUZZY legs (docs_search,
session_search, graph_search), folds in native memory_search hits passed by the
agent (--external-hits, since memory_search is a native tool-call with no CLI),
then FUSES + RERANKS + COMPILES a single minimal answer block with provenance,
and logs the call to kb/retrieval_log/.

WHAT IT IS NOT:
  - NOT a DB router (each leg owns its own DB; we call the TOOLS, not the DBs).
  - NOT a memory_search replacement (native, un-wrappable; folded by composition).
  - NOT for EXACT retrieval — data_query (SQL) and exec (regex/grep) stay a
    SEPARATE explicit fork upstream and must NEVER enter this fuzzy fusion.

COMPOSITION with memory_search (the native leg):
  agent runs memory_search natively -> passes its hits via --external-hits <json>
  -> this tool fuses them alongside the legs it runs itself. The script never
  calls memory_search directly.

USAGE:
  python3 hybrid_recall.py "<query>" [--intent-hint fact|recent|semantic|relational]
        [--external-hits <json-file-or-'-'>] [--k 5] [--json]

STATUS: STEP 1 skeleton. classify() + leg dispatch wired; fuse/rerank/compile are
stubs that pass candidates through unranked. Each later step fills ONE stage.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Workspace resolution (mirror the other legs): env > install-time sed > self-locate.
import os as _os
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WS = Path(_os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
TOOLS = WS / "tools"
PY = sys.executable or "python3"

# ---------------------------------------------------------------------------
# Intent classification -> which FUZZY legs to fire.
# data_query / exec (exact) are intentionally ABSENT: they are an upstream fork,
# not a fuzzy leg. memory_search is represented by external hits (folded in).
# ---------------------------------------------------------------------------
INTENTS = {
    "fact":       ["memory_external", "graph_search"],
    "recent":     ["session_search"],
    "semantic":   ["docs_search"],
    "relational": ["graph_search", "memory_external"],
}
DEFAULT_LEGS = ["memory_external", "docs_search", "session_search", "graph_search"]


def classify(query: str, hint: str | None) -> list[str]:
    """Return the ordered list of legs to fire. STEP-1: hint if given, else
    a cheap keyword heuristic, else all legs. Refined in a later step."""
    if hint and hint in INTENTS:
        return INTENTS[hint]
    q = query.lower()
    if any(w in q for w in ("why", "caused", "led to", "depends", "affects", "downstream", "trace", "chain")):
        return INTENTS["relational"]
    if any(w in q for w in ("yesterday", "earlier", "we said", "we decided", "last session", "just now")):
        return INTENTS["recent"]
    if any(w in q for w in ("explain", "like this", "passage", "what does", "how does")):
        return INTENTS["semantic"]
    if any(w in q for w in ("prefer", "decision", "decided", "fact", "remember", "todo")):
        return INTENTS["fact"]
    return DEFAULT_LEGS


# ---------------------------------------------------------------------------
# Leg runners — subprocess the EXISTING tools, normalize to a common candidate:
#   {leg, content, score, uri}
# ---------------------------------------------------------------------------
def _run_json(argv: list[str], env: dict | None = None) -> object | None:
    """Run a leg tool and parse its JSON. Legs sometimes print a plain-text
    notice (e.g. graph_search's 'No node found ...') to stdout even in --json
    mode, so we tolerate leading/trailing non-JSON by extracting the first
    balanced { } or [ ] block. Returns None on no-match / error (never raises).
    Optional `env` overlays os.environ for this call (used to force cost knobs
    like DOCS_HYDE_TIMEOUT=0 on the docs leg in the fusion path)."""
    try:
        _env = None
        if env:
            import os as _os2
            _env = dict(_os2.environ)
            _env.update(env)
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=_env)
        s = (out.stdout or "").strip()
        if out.returncode != 0 or not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        # Salvage: slice from first opening bracket to matching last close.
        for op, cl in (("[", "]"), ("{", "}")):
            i, j = s.find(op), s.rfind(cl)
            if 0 <= i < j:
                try:
                    return json.loads(s[i:j + 1])
                except Exception:
                    continue
        return None
    except Exception:
        return None


def leg_docs_search(query: str, k: int) -> list[dict]:
    # COST (base T1): in a 4-leg FUSION the docs HyDE LLM-expansion is low marginal
    # value (other legs already broaden recall) but adds an LLM call + up to 25s per
    # fire. Force HyDE OFF for the fusion docs leg -> T1/$0/fast. Standalone
    # docs_search keeps HyDE (env only set on THIS subprocess).
    _DOCS_LEG_ENV = {"DOCS_HYDE_TIMEOUT": "0"}
    d = _run_json([PY, str(TOOLS / "docs_search.py"), query, "--k", str(k), "--json"], env=_DOCS_LEG_ENV) or []
    out = []
    for h in (d if isinstance(d, list) else []):
        meta = h.get("metadata", {}) or {}
        title = meta.get("doc_title", meta.get("title", "?"))
        section = meta.get("section_title", meta.get("section", "?"))
        out.append({"leg": "docs_search", "content": h.get("content", ""),
                    "score": h.get("score", 0.0), "uri": f"{title}#{section}"})
    return out


def leg_session_search(query: str, k: int) -> list[dict]:
    d = _run_json([PY, str(TOOLS / "session_search.py"), query, "--k", str(k), "--json"]) or []
    return [{"leg": "session_search", "content": h.get("content", ""), "score": h.get("score", 0.0),
             "uri": h.get("session_name", "?")} for h in (d if isinstance(d, list) else [])]


def leg_graph_search(query: str, k: int) -> list[dict]:
    d = _run_json([PY, str(TOOLS / "graph_search.py"), query, "--hops", "2", "--output-format", "json"])
    res = (d or {}).get("results", []) if isinstance(d, dict) else []
    return [{"leg": "graph_search", "content": r.get("snippet", r.get("title", "")),
             "score": r.get("conf", r.get("confidence", 0.0)),
             "uri": f"node:{r.get('idx','?')}"} for r in res][:k]


def leg_memory_external(external_hits: list[dict], k: int) -> list[dict]:
    """memory_search results the AGENT ran natively and passed in. Normalize."""
    out = []
    for h in (external_hits or [])[:k]:
        out.append({"leg": "memory_search", "content": h.get("content", h.get("text", "")),
                    "score": h.get("score", 0.0), "uri": h.get("file", h.get("uri", "?"))})
    return out


LEG_FN = {
    "docs_search": leg_docs_search,
    "session_search": leg_session_search,
    "graph_search": leg_graph_search,
    "memory_external": leg_memory_external,
}


# --- efficiency: per-leg result cache + parallel dispatch --------------------
# Each leg subprocess cold-loads the multilingual-e5-small embedding model
# (~27s, ~1.6GB) — that dominates cost, not the search. Two wins, no daemon:
#   1) CACHE each leg's result (cache_utils TTL) so repeat/near-repeat queries
#      skip the subprocess entirely (sub-second warm path).
#   2) run the independent legs in PARALLEL so wall time = slowest leg, not sum.
try:
    sys.path.insert(0, str(TOOLS))
    from cache_utils import get as _cache_get, set as _cache_set  # type: ignore
    _CACHE_OK = True
except Exception:
    _CACHE_OK = False

LEG_CACHE_TTL = int(_os.environ.get("HYBRID_RECALL_CACHE_TTL", "900"))  # 15 min default


def _cached_leg(leg: str, query: str, k: int) -> list[dict]:
    """Run one search leg with a TTL result cache keyed on (leg, query, k)."""
    if _CACHE_OK:
        hit = _cache_get("hybrid_recall", leg, query, k, ttl_seconds=LEG_CACHE_TTL)
        if hit is not None:
            return hit
    res = LEG_FN[leg](query, k)
    if _CACHE_OK:
        try:
            _cache_set("hybrid_recall", leg, query, k, result=res)
        except Exception:
            pass
    return res


def fire(legs: list[str], query: str, k: int, external_hits: list[dict]) -> list[dict]:
    """Fire selected legs in PARALLEL (they are independent, each its own
    subprocess). memory_external is in-process (no subprocess) so it's cheap and
    handled inline. Search legs are cached + threaded."""
    from concurrent.futures import ThreadPoolExecutor
    cands: list[dict] = []
    search_legs = [lg for lg in legs if lg != "memory_external"]
    if "memory_external" in legs:
        cands += leg_memory_external(external_hits, k)
    if search_legs:
        # threads are fine: the work is a blocking subprocess wait (releases GIL).
        with ThreadPoolExecutor(max_workers=len(search_legs)) as ex:
            for res in ex.map(lambda lg: _cached_leg(lg, query, k), search_legs):
                cands += res
    return cands


# ---------------------------------------------------------------------------
# STEP 2: real fuse / rerank / compile.
# ---------------------------------------------------------------------------
import hashlib as _hashlib
import re as _re

# Per-leg score normalization. Legs report on DIFFERENT scales (session/docs
# cosine ~0.7-0.9 dense; graph conf 0-1; memory_search 0-1). Min-max WITHIN a
# leg puts every candidate on a comparable 0-1 relevance axis before fusion, so
# one leg's naturally-higher raw range doesn't dominate the merge.
def _normalize_within_leg(cands: list[dict]) -> None:
    by_leg: dict[str, list[dict]] = {}
    for c in cands:
        by_leg.setdefault(c["leg"], []).append(c)
    for group in by_leg.values():
        scores = [c.get("score", 0.0) or 0.0 for c in group]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        for c in group:
            c["norm_sim"] = ((c.get("score", 0.0) or 0.0) - lo) / span if hi != lo else 1.0


def _content_key(text: str) -> str:
    """Normalized content fingerprint for cross-leg dedup: same passage surfaced
    by two legs collapses to one candidate (and counts as corroboration)."""
    norm = _re.sub(r"\s+", " ", (text or "").lower()).strip()[:300]
    return _hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


_DATE_RE = _re.compile(r"(20\d\d)[-/](\d{2})[-/](\d{2})")


def _recency_fit(text: str) -> float:
    """Cheap recency proxy from any YYYY-MM-DD in the content. Newer -> closer to
    1.0, decaying over ~180 days. No date found -> neutral 0.5 (don't punish)."""
    m = _DATE_RE.search(text or "")
    if not m:
        return 0.5
    try:
        from datetime import date
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        age = (date.today() - date(y, mo, d)).days
        if age < 0:
            return 0.5
        return max(0.0, 1.0 - age / 180.0)
    except Exception:
        return 0.5


def fuse(cands: list[dict]) -> list[dict]:
    """Dedup by normalized CONTENT (cross-leg), not by uri. When two legs surface
    the same passage, collapse to one candidate and record corroboration
    (evidence_count + which legs) — that agreement is itself signal for rerank."""
    _normalize_within_leg(cands)
    merged: dict[str, dict] = {}
    for c in cands:
        key = _content_key(c.get("content", ""))
        if key in merged:
            m = merged[key]
            m["evidence_count"] += 1
            m["legs"].add(c["leg"])
            # keep the strongest normalized relevance across corroborating legs
            if c.get("norm_sim", 0.0) > m.get("norm_sim", 0.0):
                m["norm_sim"] = c["norm_sim"]
            m["provenance"].append({"leg": c["leg"], "uri": c["uri"], "score": c.get("score", 0.0)})
        else:
            merged[key] = {
                "content": c.get("content", ""),
                "leg": c["leg"],
                "legs": {c["leg"]},
                "uri": c["uri"],
                "score": c.get("score", 0.0),
                "norm_sim": c.get("norm_sim", 0.0),
                "evidence_count": 1,
                "provenance": [{"leg": c["leg"], "uri": c["uri"], "score": c.get("score", 0.0)}],
            }
    return list(merged.values())


# Rerank weights. Heuristic start (per the note's open question); tune later from
# kb/retrieval_log/ health. evidence_count is log-damped so 5 corroborations
# don't swamp a single strong-relevance hit.
import math as _math
W_SIM = 1.0
W_EVIDENCE = 0.35
W_RECENCY = 0.20
W_REDUNDANCY = 0.15  # penalty applied to near-duplicate survivors (2nd+ of a leg-cluster)


def rerank(cands: list[dict]) -> list[dict]:
    """score = W_SIM*norm_sim + W_EVIDENCE*log1p(evidence-1) + W_RECENCY*recency
             - W_REDUNDANCY*redundancy_rank.
    Contradiction + staleness penalties are deferred to STEP 2b (need
    l3 contradiction_log wiring). Redundancy here = de-emphasize the 2nd+
    same-leg near-neighbor so the top block isn't three paraphrases."""
    for c in cands:
        c["recency_fit"] = _recency_fit(c.get("content", ""))
        base = (W_SIM * c.get("norm_sim", 0.0)
                + W_EVIDENCE * _math.log1p(max(0, c.get("evidence_count", 1) - 1))
                + W_RECENCY * c["recency_fit"])
        c["_base_score"] = base
    ranked = sorted(cands, key=lambda c: c["_base_score"], reverse=True)
    # redundancy pass: demote later members that share the primary leg of an
    # already-surfaced higher item (cheap intra-leg diversity).
    seen_legs: dict[str, int] = {}
    for c in ranked:
        n = seen_legs.get(c["leg"], 0)
        c["final_score"] = c["_base_score"] - W_REDUNDANCY * n
        seen_legs[c["leg"]] = n + 1
    return sorted(ranked, key=lambda c: c["final_score"], reverse=True)


def compile_block(cands: list[dict], k: int) -> dict:
    """Minimal answer block + full provenance trail. L0 = the fused top-k lines;
    provenance carries every corroborating leg/uri/score so the answer is
    auditable (which the by-hand routing never produced)."""
    top = cands[:k]
    return {
        "answer_candidates": [
            {
                "content": c["content"],
                "leg": c["leg"],
                "legs": sorted(c.get("legs", {c["leg"]})),
                "uri": c["uri"],
                "final_score": round(c.get("final_score", c.get("score", 0.0)), 4),
                "norm_sim": round(c.get("norm_sim", 0.0), 4),
                "evidence_count": c.get("evidence_count", 1),
                "recency_fit": round(c.get("recency_fit", 0.5), 3),
            }
            for c in top
        ],
        "provenance": [p for c in top for p in c.get("provenance", [])],
    }


def log_call(tool: str, query: str, k: int, cands: list[dict]) -> None:
    try:
        sys.path.insert(0, str(WS / "procedures"))
        from _retrieval_log import log_retrieval  # type: ignore
        log_retrieval(WS, tool, query, k=k,
                      result_uris=[c["uri"] for c in cands],
                      result_scores=[c.get("score", 0.0) for c in cands])
    except Exception:
        pass  # logging is best-effort, never blocks recall


def main(argv=None):
    ap = argparse.ArgumentParser(description="hybrid_recall — fuzzy retrieval fusion (dinomem-neuron)")
    ap.add_argument("query")
    ap.add_argument("--intent-hint", choices=list(INTENTS.keys()), default=None)
    ap.add_argument("--external-hits", default=None,
                    help="JSON file (or '-' for stdin) of native memory_search hits")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    external = []
    if args.external_hits:
        raw = sys.stdin.read() if args.external_hits == "-" else Path(args.external_hits).read_text()
        try:
            external = json.loads(raw)
            if isinstance(external, dict):
                external = external.get("results", external.get("hits", []))
        except Exception:
            external = []

    legs = classify(args.query, args.intent_hint)
    cands = fire(legs, args.query, args.k, external)
    cands = fuse(cands)
    cands = rerank(cands)
    block = compile_block(cands, args.k)
    block["_meta"] = {"legs_fired": legs, "candidate_count": len(cands),
                      "query": args.query, "k": args.k}
    log_call("hybrid_recall", args.query, args.k, cands)

    if args.json:
        print(json.dumps(block, indent=1, ensure_ascii=False))
    else:
        print(f"legs: {', '.join(legs)}  |  {len(cands)} candidates")
        for i, c in enumerate(block["answer_candidates"], 1):
            legs = "+".join(c.get("legs", [c["leg"]]))
            corr = f" ×{c['evidence_count']}" if c.get("evidence_count", 1) > 1 else ""
            print(f"\n[{i}] ({legs}{corr} · score {c['final_score']:.3f} · sim {c['norm_sim']:.2f} · rec {c['recency_fit']:.2f}) {c['uri']}")
            print("    " + c["content"][:200].replace("\n", " "))


if __name__ == "__main__":
    main()
