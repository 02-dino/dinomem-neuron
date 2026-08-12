#!/usr/bin/env python3
"""
explain_memory.py — P2 #4 observability: "why is this memory here?"

A read-only EXPLAIN/TRACE tool. Given one memory file (or a query), it JOINS the
three provenance surfaces dinomem already produces — it builds no new state:

  1. LIFECYCLE  — what state is this memory in and why      (lifecycle_state.py)
  2. RETRIEVAL  — when/why has it actually been retrieved     (kb/retrieval_log/*.jsonl)
  3. CONFIDENCE — how much is it trusted, on what evidence     (promotion_state.json
                  + confidence_engine evidence stamps: calibrated_confidence,
                  independent_cluster_count, contradictions[], evidence{...})

This answers the proposal's "why was this memory retrieved / what state / why that
confidence" without a human reading five files. READ ONLY, deterministic, no LLM.
Fail-open: any unreadable surface contributes nothing rather than crashing.

Usage:
  python3 explain_memory.py --file memory/_pin_x.md         # full trace for one file
  python3 explain_memory.py --query "atlas lead"            # rank files by retrieval hits on a query substring
  python3 explain_memory.py --file ... --json               # machine-readable
  python3 explain_memory.py --days 30 --file ...            # retrieval window (default 30d)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

_WS_DEFAULT = "/root/.openclaw/workspace-analyst"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(_SCRIPT_DIR.parent)
WS = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
RETRIEVAL_LOG_DIR = WS / "kb/retrieval_log"
PROMO_STATE = WS / "kb/memory_neuron/l4_promoted/promotion_state.json"


def _load_lifecycle():
    try:
        import lifecycle_state as ls
        return ls
    except Exception:
        return None


def _abspath(f):
    p = Path(f)
    return p if p.is_absolute() else (WS / p)


def _rel(p):
    p = Path(p)
    try:
        return str(p.resolve().relative_to(WS))
    except Exception:
        return str(p)


# ── surface 1: lifecycle state ────────────────────────────────────────────────
def lifecycle_of(path):
    ls = _load_lifecycle()
    if ls is None:
        return {"state": "unknown", "why": "lifecycle_state unavailable (fail-open)"}
    try:
        return ls.state_of_file(Path(path))
    except Exception as e:
        return {"state": "unknown", "why": "lifecycle read error: %s" % e}


# ── surface 2: retrieval history ──────────────────────────────────────────────
def _iter_log_rows(days):
    """Yield retrieval-log records within the window (fail-open per file/line)."""
    if not RETRIEVAL_LOG_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for f in sorted(RETRIEVAL_LOG_DIR.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("ts")
                if ts:
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t < cutoff:
                            continue
                    except Exception:
                        pass
                yield d
        except Exception:
            continue


def _uri_matches(uri, target_abs, target_rel):
    if not uri:
        return False
    u = str(uri)
    return (u == str(target_abs) or u == target_rel
            or u.endswith("/" + target_rel) or Path(u).name == Path(target_rel).name)


def retrieval_of(path, days):
    """Every retrieval event whose result_uris include this file, newest-first."""
    target_abs = _abspath(path).resolve()
    target_rel = _rel(path)
    hits = []
    tools_seen = {}
    for d in _iter_log_rows(days):
        uris = d.get("result_uris") or []
        idx = None
        for i, u in enumerate(uris):
            if _uri_matches(u, target_abs, target_rel):
                idx = i
                break
        if idx is None:
            continue
        scores = d.get("top_scores") or []
        score = scores[idx] if idx < len(scores) else None
        tool = d.get("tool", "?")
        tools_seen[tool] = tools_seen.get(tool, 0) + 1
        hits.append({
            "ts": d.get("ts"), "tool": tool, "query": d.get("query"),
            "rank": idx + 1, "of": d.get("n_results", len(uris)), "score": score,
        })
    hits.sort(key=lambda h: h.get("ts") or "", reverse=True)
    return {
        "retrieval_count": len(hits),
        "by_tool": tools_seen,
        "last_retrieved": hits[0]["ts"] if hits else None,
        "events": hits,
    }


# ── surface 3: confidence + evidence ──────────────────────────────────────────
def _load_promo():
    if not PROMO_STATE.exists():
        return {}
    try:
        d = json.loads(PROMO_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def confidence_of(path):
    """If this file backs a tracked insight, surface its calibrated confidence +
    the evidence the confidence_engine stamped (fabrication-resistant facts)."""
    promo = _load_promo()
    if not promo:
        return {"tracked": False, "why": "no promotion_state (cold store or not an insight)"}
    rel = _rel(path)
    name = Path(rel).name
    stem = Path(rel).stem.replace("_pin_", "").replace("_note_", "")
    for key, e in promo.items():
        srcs = e.get("sources") or []
        rule = (e.get("rule_line") or "")
        matched = (name in srcs or rel in srcs
                   or any(name in str(s) or stem in str(s) for s in srcs)
                   or stem in rule)
        if not matched:
            continue
        ev = e.get("evidence") or {}
        return {
            "tracked": True, "insight_key": key, "status": e.get("status"),
            "reinforce_count": e.get("reinforce_count"),
            "raw_confidence": e.get("confidence"),
            "calibrated_confidence": e.get("calibrated_confidence"),
            "independent_cluster_count": e.get("independent_cluster_count"),
            "contradictions": e.get("contradictions"),
            "evidence": {"source_count": ev.get("source_count"),
                         "distinct_day_count": ev.get("distinct_day_count")} if ev else None,
            "pattern": e.get("pattern"),
        }
    return {"tracked": False, "why": "file not linked to any promotion-state insight"}


# ── join + render ─────────────────────────────────────────────────────────────
def explain(path, days):
    p = _abspath(path)
    return {
        "file": _rel(p),
        "exists": p.exists() or (WS / "memory/_history" / Path(path).name).exists(),
        "lifecycle": lifecycle_of(p),
        "retrieval": retrieval_of(p, days),
        "confidence": confidence_of(p),
    }


def rank_by_query(qsub, days, top):
    """Files most retrieved for queries containing a substring — 'what does this
    query pull, and why'."""
    qsub = (qsub or "").lower()
    tally = {}
    for d in _iter_log_rows(days):
        if qsub and qsub not in (d.get("query") or "").lower():
            continue
        scores = d.get("top_scores") or []
        for i, u in enumerate(d.get("result_uris") or []):
            if not u:
                continue
            rec = tally.setdefault(u, {"uri": u, "hits": 0, "best_score": None})
            rec["hits"] += 1
            if i < len(scores) and scores[i] is not None:
                s = scores[i]
                rec["best_score"] = s if rec["best_score"] is None else max(rec["best_score"], s)
    ranked = sorted(tally.values(), key=lambda r: (r["hits"], r["best_score"] or 0), reverse=True)
    return ranked[:top]


def _fmt(rep):
    L = []
    L.append("=== EXPLAIN: %s ===" % rep["file"])
    if not rep["exists"]:
        L.append("  (file not found on disk or in _history/)")
    lc = rep["lifecycle"]
    L.append("LIFECYCLE : %s" % lc.get("state"))
    L.append("            %s" % lc.get("why", ""))
    if lc.get("promo_status"):
        L.append("            insight overlay: %s" % lc.get("promo_status"))
    rt = rep["retrieval"]
    L.append("RETRIEVAL : %d event(s) in window | last=%s | by_tool=%s"
             % (rt["retrieval_count"], rt["last_retrieved"] or "never", rt["by_tool"] or "{}"))
    for ev in rt["events"][:5]:
        L.append("            %s  %-14s rank %s/%s  score=%s  q=%r"
                 % (ev["ts"], ev["tool"], ev["rank"], ev["of"], ev["score"], (ev["query"] or "")[:48]))
    cf = rep["confidence"]
    if cf.get("tracked"):
        L.append("CONFIDENCE : status=%s  raw=%s  calibrated=%s  reinforce=%s"
                 % (cf.get("status"), cf.get("raw_confidence"),
                    cf.get("calibrated_confidence"), cf.get("reinforce_count")))
        L.append("            evidence: clusters=%s  contradictions=%s  %s"
                 % (cf.get("independent_cluster_count"), cf.get("contradictions"), cf.get("evidence")))
    else:
        L.append("CONFIDENCE : untracked — %s" % cf.get("why"))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="P2 #4 observability: why is this memory here?")
    ap.add_argument("--file", default="", help="Memory file to explain (rel or abs).")
    ap.add_argument("--query", default=None,
                    help="Rank files by retrieval hits on queries matching this substring "
                         "(pass empty string for ALL queries in the window).")
    ap.add_argument("--days", type=int, default=30, help="Retrieval-log lookback window (default 30).")
    ap.add_argument("--top", type=int, default=10, help="Top-N for --query.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = ap.parse_args()

    if not args.file and args.query is None:
        ap.error("need --file or --query (\"\" = all queries)")

    if args.query is not None and not args.file:
        ranked = rank_by_query(args.query, args.days, args.top)
        if args.json:
            print(json.dumps({"query_substr": args.query, "days": args.days, "ranked": ranked},
                             ensure_ascii=False, indent=2))
        else:
            print("=== files retrieved for queries ~ %r (last %dd) ===" % (args.query, args.days))
            if not ranked:
                print("  (no retrieval events matched)")
            for r in ranked:
                print("  hits=%-3d best=%-6s %s" % (r["hits"], r["best_score"], r["uri"]))
        return 0

    rep = explain(args.file, args.days)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else _fmt(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
