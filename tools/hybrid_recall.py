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
import re
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
# memory_native sits BESIDE memory_external on every memory-bearing intent: the
# native tool and the direct index reader fail INDEPENDENTLY, so running both is
# what makes the memory leg survive a memory_search outage. Fusion dedup collapses
# the overlap when both are healthy, so the cost of the pair is ~one leg.
INTENTS = {
    "fact":       ["memory_external", "memory_native", "graph_search"],
    "recent":     ["session_search", "memory_native"],
    "semantic":   ["docs_search", "memory_native"],
    "relational": ["graph_search", "memory_external", "memory_native"],
    # KILLER FEATURE: "what does X do AND why does it exist" = code STRUCTURE leg
    # (what calls X / X calls / inherits, file:line) FUSED with the memory legs
    # (the design decision that created X). Neither Graphify (no memory) nor plain
    # dinomem (no code) can answer this alone.
    "code":       ["code_query", "memory_external", "memory_native", "graph_search"],
}
DEFAULT_LEGS = ["memory_external", "memory_native", "docs_search",
                "session_search", "graph_search"]


def classify(query: str, hint: str | None) -> list[str]:
    """Return the ordered list of legs to fire. STEP-1: hint if given, else
    a cheap keyword heuristic, else all legs. Refined in a later step."""
    if hint and hint in INTENTS:
        return INTENTS[hint]
    q = query.lower()
    # code triggers checked FIRST: the 'code' intent already bundles graph+memory
    # for the 'why' half, so a query mixing code terms + 'why' (the killer
    # 'what does X do AND why does it exist') routes here, not to bare relational.
    if any(w in q for w in ("function", "method", "class ", " calls", "called by",
                            "imports", "inherits", "defined in", "code", "what calls")):
        return INTENTS["code"]
    # bare-symbol + do/why: a query that NAMES a code-symbol-shaped token (snake_case,
    # camelCase, or dotted) alongside 'do'/'why'/'exist' is the killer 'what does X
    # do AND why' even without a structural keyword. Now that bash/json/md symbols
    # are modelled, route these to the code leg so the shell/config 'X' fuses with
    # the memory 'why'. Guarded on an identifier-shaped token so plain prose ("why
    # did we decide") still falls through to relational.
    if any(w in q for w in ("do", "does", "why", "exist", "purpose", "what is")) \
            and re.search(r"\b\w*(?:_\w+|[a-z][A-Z])\w*\b|\b\w+\.\w+\b", query):
        return INTENTS["code"]
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
    # Carry session-id attribution through fusion. session_search now emits
    # session_ids (list) + session_id (scalar) per chunk; without forwarding them
    # here the benchmark's recall_at_k axis stays dark (answer.py reads them off
    # the fused candidate). Only this leg is session-attributable by construction.
    return [{"leg": "session_search", "content": h.get("content", ""), "score": h.get("score", 0.0),
             "uri": h.get("session_name", "?"),
             "session_ids": h.get("session_ids", []),
             "session_id": h.get("session_id", "")} for h in (d if isinstance(d, list) else [])]


def leg_graph_search(query: str, k: int, as_of: str | None = None,
                     scope: str | None = None) -> list[dict]:
    # P0-c (2026-08-12): thread as_of/scope INTO the graph traversal so temporal
    # (valid-time) + privacy (scope) filtering happens AT traversal via graph_search's
    # own --as-of/--scope (added in the P0-b reader work), not only in hybrid_recall's
    # post-hoc apply_as_of. Filtering edges before BFS = correct multi-hop results
    # (a hop through an edge invalid/out-of-scope at the as-of date is never taken).
    # Fail-safe: only append flags when set, so legacy/untimed graphs are untouched.
    cmd = [PY, str(TOOLS / "graph_search.py"), query, "--hops", "2", "--output-format", "json"]
    # HYBRID_GRAPH_PATH lets a caller (e.g. the benchmark's fixture run) point the
    # graph leg at an alternate graph.json so its candidates flow THROUGH the
    # compiler. Unset in normal use = the live workspace graph (default).
    import os as _os_g
    _gpath = _os_g.environ.get("HYBRID_GRAPH_PATH")
    if _gpath:
        cmd += ["--graph", _gpath]
    if as_of:
        cmd += ["--as-of", as_of]
    if scope:
        cmd += ["--scope", scope]
    d = _run_json(cmd)
    res = (d or {}).get("results", []) if isinstance(d, dict) else []
    return [{"leg": "graph_search", "content": r.get("snippet", r.get("title", "")),
             "score": r.get("conf", r.get("confidence", 0.0)),
             "uri": f"node:{r.get('idx','?')}"} for r in res][:k]


def leg_code_query(query: str, k: int) -> list[dict]:
    """CODE-STRUCTURE leg: fuzzy-find a symbol in the code graph, return its
    neighborhood (calls / called-by / imports / inherits) with file:line +
    provenance. FAIL-SOFT: no code graph / no match -> [] (the fusion just has
    one fewer leg). This is the structural half of the 'what does X do AND why'
    fusion; the memory legs supply the 'why'."""
    # Try the full query, then fall back to identifier-like tokens (snake_case,
    # camelCase, dotted, or >=4-char words) — a NL sentence rarely fuzzy-matches a
    # symbol name, but the symbol is usually a token INSIDE it ("what calls
    # diff_since" -> diff_since).
    d = _run_json([PY, str(TOOLS / "code_query.py"), "query", query, "--hops", "1", "--json"])
    if not (isinstance(d, dict) and d.get("ok")):
        import re as _re2
        toks = _re2.findall(r"[A-Za-z_][A-Za-z0-9_.]{3,}", query)
        # prefer identifier-shaped tokens (contain _ or . or camelCase) first
        toks.sort(key=lambda t: 0 if ("_" in t or "." in t or _re2.search(r"[a-z][A-Z]", t)) else 1)
        for t in toks[:6]:
            d = _run_json([PY, str(TOOLS / "code_query.py"), "explain", t, "--hops", "1", "--json"])
            if isinstance(d, dict) and d.get("ok"):
                break
    if not isinstance(d, dict) or not d.get("ok"):
        return []
    out = []
    node = d.get("node", "?")
    # the matched symbol itself is the primary hit (carries file:line)
    out.append({"leg": "code_query", "content": node, "score": 1.0, "uri": node})
    for label, items in (d.get("outgoing", {}) or {}).items():
        for it in items[:k]:
            tag = it.get("provenance", "")
            out.append({"leg": "code_query",
                        "content": f"{node} {label} {it.get('target','?')}"
                                   + (f" [{tag}]" if tag not in ("‑", "?", "") else ""),
                        "score": 0.9, "uri": it.get("target", "?")})
    for cb in (d.get("called_by", []) or [])[:k]:
        out.append({"leg": "code_query",
                    "content": f"{cb.get('source','?')} calls {node}",
                    "score": 0.9, "uri": cb.get("source", "?")})
    return out[: max(k, 1) * 3]

def leg_memory_external(external_hits: list[dict], k: int) -> list[dict]:
    """memory_search results the AGENT ran natively and passed in. Normalize."""
    out = []
    for h in (external_hits or [])[:k]:
        out.append({"leg": "memory_search", "content": h.get("content", h.get("text", "")),
                    "score": h.get("score", 0.0), "uri": h.get("file", h.get("uri", "?"))})
    return out


def leg_memory_native(query: str, k: int) -> list[dict]:
    """FIRST-PARTY memory leg: reads the native memory index directly (SQLite
    FTS5 + dense cosine) instead of waiting for the agent to paste memory_search
    hits in via --external-hits.

    WHY IT EXISTS: memory_external only fires when the agent ALREADY ran the
    native tool, so when memory_search degrades (measured 2026-10-19: timeout
    after 15s on an embedding/provider error) the memory half of the fusion goes
    dark and the rest answers from docs/sessions/graph alone. This leg reads the
    SAME index and keeps working. FAIL-SOFT: [] when the index or TEI is down, so
    a broken box costs one leg, never the whole recall."""
    try:
        sys.path.insert(0, str(TOOLS))
        from _memory_native import search as _mn_search  # type: ignore

        hits = _mn_search(query, k, workspace=WS)
    except Exception:
        return []
    return [{"leg": "memory_native", "content": h.get("content", ""),
             "score": h.get("score", 0.0),
             "uri": f"{h.get('path','?')}#L{h.get('line','?')}"} for h in hits]


LEG_FN = {
    "docs_search": leg_docs_search,
    "session_search": leg_session_search,
    "graph_search": leg_graph_search,
    "code_query": leg_code_query,
    "memory_external": leg_memory_external,
    "memory_native": leg_memory_native,
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


def _cached_leg(leg: str, query: str, k: int, as_of: str | None = None,
                scope: str | None = None) -> list[dict]:
    """Run one search leg with a TTL result cache keyed on (leg, query, k[, as_of, scope]).
    P0-c: as_of/scope MUST enter the cache key so a filtered graph result never
    collides with an unfiltered one for the same query."""
    # only the graph leg consumes as_of/scope; keep the key stable (no None noise) for others.
    ckey = (leg, query, k) if leg != "graph_search" else (leg, query, k, as_of or "", scope or "")
    if _CACHE_OK:
        hit = _cache_get("hybrid_recall", *ckey, ttl_seconds=LEG_CACHE_TTL)
        if hit is not None:
            return hit
    if leg == "graph_search":
        res = leg_graph_search(query, k, as_of=as_of, scope=scope)
    else:
        res = LEG_FN[leg](query, k)
    if _CACHE_OK:
        try:
            _cache_set("hybrid_recall", *ckey, result=res)
        except Exception:
            pass
    return res


def fire(legs: list[str], query: str, k: int, external_hits: list[dict],
         as_of: str | None = None, scope: str | None = None) -> list[dict]:
    """Fire selected legs in PARALLEL (they are independent, each its own
    subprocess). memory_external is in-process (no subprocess) so it's cheap and
    handled inline. Search legs are cached + threaded.
    P0-c: as_of/scope are threaded to the graph leg so temporal/privacy filtering
    happens AT traversal (correct multi-hop), not only post-hoc."""
    from concurrent.futures import ThreadPoolExecutor
    cands: list[dict] = []
    search_legs = [lg for lg in legs if lg != "memory_external"]
    if "memory_external" in legs:
        cands += leg_memory_external(external_hits, k)
    if search_legs:
        # threads are fine: the work is a blocking subprocess wait (releases GIL).
        with ThreadPoolExecutor(max_workers=len(search_legs)) as ex:
            for res in ex.map(lambda lg: _cached_leg(lg, query, k, as_of, scope), search_legs):
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
            # Corroboration branch: if THIS leg carries session attribution and the
            # kept candidate lacked it (e.g. session_search corroborates a passage
            # first surfaced by the memory shim), adopt it so recall_at_k can score.
            if not m.get("session_ids") and c.get("session_ids"):
                m["session_ids"] = c.get("session_ids")
                m["session_id"] = c.get("session_id", "")
        else:
            merged[key] = {
                "content": c.get("content", ""),
                "leg": c["leg"],
                "legs": {c["leg"]},
                "uri": c["uri"],
                "score": c.get("score", 0.0),
                "norm_sim": c.get("norm_sim", 0.0),
                "evidence_count": 1,
                # Carry session attribution through fusion -> answer.py reads it off
                # the fused candidate -> recall_at_k lights up. Absent on non-session
                # legs (memory shim/graph/docs) by construction; '' / [] there.
                "session_ids": c.get("session_ids", []),
                "session_id": c.get("session_id", ""),
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


def apply_as_of(cands: list[dict], as_of: str | None) -> list[dict]:
    """Valid-time filter (STEP 4). When --as-of DATE is given, drop candidates
    whose backing memory file is NOT valid at that date (valid_from <= DATE <
    valid_until). Candidates whose uri does NOT resolve to a real _pin_/_note_
    memory file (docs/session/graph legs, or a missing file) PASS THROUGH — we
    can't judge their validity, so fail-open (keep). No --as-of = today's exact
    behavior (no filtering; only current facts naturally surface anyway).
    DETERMINISTIC / T1 / $0: pure frontmatter date compare, no LLM, no embed."""
    if not as_of:
        return cands
    # Import valid_time robustly: try WS/procedures AND the tool's own sibling
    # ../procedures (tools/ and procedures/ are siblings under the workspace root).
    _vt = None
    for _cand_dir in (WS / "procedures", Path(__file__).resolve().parent.parent / "procedures"):
        try:
            sys.path.insert(0, str(_cand_dir))
            import valid_time as _vt  # type: ignore
            break
        except Exception:
            _vt = None
    if _vt is None:
        return cands  # fail-open: no valid_time -> no filtering
    kept = []
    for c in cands:
        uri = c.get("uri", "")
        # only memory-file-backed candidates are judgeable
        p = None
        if isinstance(uri, str) and (uri.endswith(".md") or "_pin_" in uri or "_note_" in uri):
            cand_path = (WS / uri) if not uri.startswith("/") else Path(uri)
            if cand_path.exists():
                p = cand_path
            else:
                # try memory/ + _history/ by basename
                base = Path(uri).name
                for cand in (WS / "memory" / base, WS / "memory" / "_history" / base):
                    if cand.exists():
                        p = cand
                        break
        if p is None:
            kept.append(c)  # unjudgeable -> fail-open keep
            continue
        if _vt.is_valid_at(_vt.read_fields(p), as_of):
            kept.append(c)
    return kept


def _load_valid_time():
    """Import valid_time from WS/procedures or the tool's sibling ../procedures."""
    for _cand_dir in (WS / "procedures", Path(__file__).resolve().parent.parent / "procedures"):
        try:
            sys.path.insert(0, str(_cand_dir))
            import valid_time as _vt  # type: ignore
            return _vt
        except Exception:
            continue
    return None


def _read_scope(path, content: str) -> str | None:
    """Read the frontmatter `scope:` field. valid_time.VT_FIELDS does NOT include
    scope (it only parses valid-time), so the compiler needs its own reader or the
    scope gate silently no-ops (=privacy leak). Prefer the file's head; fall back
    to the candidate content's head. Returns the scope string or None."""
    head = ""
    if path is not None:
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            head = ""
    if not head:
        head = content or ""
    for line in head.splitlines()[:12]:
        m = _re.match(r"\s*scope\s*:\s*(\S+)", line)
        if m:
            v = m.group(1).strip()
            return None if v.lower() in ("null", "none", "") else v
    return None


def _resolve_candidate_file(uri: str):
    """Resolve a candidate uri to a real memory file path, or None."""
    if not isinstance(uri, str) or not uri:
        return None
    p = Path(uri) if uri.startswith("/") else (WS / uri)
    if p.exists():
        return p
    base = Path(uri).name
    for cand in (WS / "memory" / base, WS / "memory" / "_history" / base):
        if cand.exists():
            return cand
    return None


def compile_context(cands: list[dict], as_of: str | None, scope: str | None) -> list[dict]:
    """P2 #3 CONTEXT COMPILER.

    Applies scope-gate + temporal (valid-time) resolution to EVERY candidate,
    including the folded memory_external hits — not just the graph leg. Closes the
    gap where as_of/scope only protected graph traversal, so memory_search hits
    leaked stale/out-of-scope facts.

    Order:
      1. SCOPE GATE — drop candidates whose backing-file scope is not allowed.
      2. TEMPORAL   — drop candidates not valid at as_of (valid_from<=DATE<until).
    Unresolvable-uri candidates fail-open for scope; for temporal we also read
    valid-time straight from candidate CONTENT frontmatter. DETERMINISTIC, $0.
    Emits a per-candidate _compiler trace (why kept/dropped).
    """
    # NOTE: do NOT early-return on (not as_of and not scope): supersession
    # resolution (step 3) runs precisely when as_of is ABSENT (a 'current' query),
    # dropping superseded chain members. Only skip the whole compiler if there's
    # no valid_time module (nothing to judge with).
    _vt = _load_valid_time()
    if _vt is None:
        return cands  # fail-open: no valid_time module -> no filtering

    scope_allow = None
    if scope:
        scope_allow = {s.strip() for s in scope.split(",") if s.strip()}

    kept: list[dict] = []
    for c in cands:
        uri = c.get("uri", "")
        p = _resolve_candidate_file(uri)
        if p is not None:
            fields = _vt.read_fields(p)
        else:
            fields = _vt.fields_from_text(c.get("content", ""))
        trace = {"scope": None, "temporal": None}

        # 1. SCOPE GATE. Read scope via our OWN parser (valid_time doesn't carry
        #    it). Missing scope defaults to 'owner' (don't silently drop legacy
        #    untagged facts). Graph-leg cands already scope-filtered at traversal;
        #    this catches memory/docs/session cands too.
        cand_scope = _read_scope(p, c.get("content", "")) or "owner"
        if scope_allow is not None and cand_scope not in scope_allow:
            trace["scope"] = f"DROP({cand_scope} not in {sorted(scope_allow)})"
            c["_compiler"] = trace
            continue
        trace["scope"] = f"ok({cand_scope})"

        # 2. TEMPORAL. Only judge candidates that carry valid-time; undated ones
        #    fail-open keep.
        if as_of and (fields.get("valid_from") or fields.get("valid_until")
                      or fields.get("valid_at") or fields.get("invalid_at")):
            if not _vt.is_valid_at(fields, as_of):
                trace["temporal"] = f"DROP(not valid at {as_of})"
                c["_compiler"] = trace
                continue
            trace["temporal"] = f"ok(valid at {as_of})"
        else:
            trace["temporal"] = "undated(keep)" if as_of else "n/a"

        c["_compiler"] = trace
        kept.append(c)

    # 3. SUPERSESSION RESOLUTION (only when NO as_of = a 'current' query). Group
    #    survivors by subject; if a group has any CURRENT member (valid_until null
    #    AND not superseded_by), drop the non-current members of that group. This
    #    is what a bare 'current lead' query needs: without a temporal anchor the
    #    scope/temporal passes can't drop stale-but-in-scope facts, so all chain
    #    versions would surface. Fail-safe: cands with no subject are untouched;
    #    a group with NO current member keeps everything (don't guess).
    if not as_of:
        by_subject: dict[str, list[dict]] = {}
        for c in kept:
            p = _resolve_candidate_file(c.get("uri", ""))
            fields = _vt.read_fields(p) if p is not None else _vt.fields_from_text(c.get("content", ""))
            subj = fields.get("subject")
            c["_vt_fields"] = fields
            if subj:
                by_subject.setdefault(subj, []).append(c)
        drop = set()
        for subj, group in by_subject.items():
            if len(group) < 2:
                continue
            currents = [c for c in group if _vt.is_current(c["_vt_fields"])]
            if not currents:
                continue  # no clear current -> keep all (fail-open)
            for c in group:
                if c not in currents:
                    tr = c.setdefault("_compiler", {})
                    tr["supersession"] = f"DROP(superseded in subject '{subj}')"
                    drop.add(id(c))
        if drop:
            kept = [c for c in kept if id(c) not in drop]
        for c in kept:
            c.pop("_vt_fields", None)  # internal scratch, don't leak into output
    return kept


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
                # THE terminal hop: answer.py parses answer_candidates and reads
                # session_ids off each entry for recall_at_k. This output whitelist
                # (not fuse()'s intermediate dict) is what the benchmark consumes —
                # omitting it here re-drops attribution after every upstream hop
                # carried it. Absent on non-session legs -> [] / '' by construction.
                "session_ids": c.get("session_ids", []),
                "session_id": c.get("session_id", ""),
                # compiler trace (why kept/dropped in apply_as_of scope+temporal).
                "_compiler": c.get("_compiler"),
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
    ap.add_argument("--as-of", default=None,
                    help="Bitemporal point-in-time query (ISO date). Keep only "
                         "candidates whose backing memory file was VALID at this "
                         "date. Omit = current facts only (today's behavior).")
    ap.add_argument("--scope", default=None,
                    help="Privacy scope filter (owner|project|peer) applied to the "
                         "graph leg's edges BEFORE BFS traversal. Omit = no scope "
                         "restriction (all edges eligible).")
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
    cands = fire(legs, args.query, args.k, external, as_of=args.as_of, scope=args.scope)
    cands = fuse(cands)
    cands = rerank(cands)
    cands = apply_as_of(cands, args.as_of)
    # P2 #3: context compiler applies scope-gate + temporal resolution to ALL
    # candidates (incl. folded memory hits), superseding the graph-leg-only
    # apply_as_of. This is what lets neuron actually beat base on temporal/scope.
    cands = compile_context(cands, args.as_of, args.scope)
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
