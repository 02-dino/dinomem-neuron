#!/usr/bin/env python3
"""
fact_query.py — P0-b fact-graph QUERY layer (the proposal's public graph API).

Answers the four target questions over the neuron relation-edge graph, using the
P0-b enriched fields (valid_at/invalid_at/status/scope/source_memory_ids) written
by memory_graph.py. Pairs with graph_search.py (traversal) — this module is the
TEMPORAL + PROVENANCE query surface, not multi-hop BFS.

Public queries (proposal §"Core queries"):
  current(subject, predicate)      -> latest trusted fact valid NOW
  as_of(subject, predicate, date)  -> fact believed valid at a point in time
  history(subject, predicate)      -> full supersession chain (oldest->newest)
  why(subject, predicate)          -> evidence: source memories + confidence + status
  what_changed(subject, range)     -> state transitions over a time range

DESIGN / HARMONY (matches the rest of dinomem):
  - READ-ONLY over kb/memory_neuron/l2_graph/memory_graph.json (the derived index).
    Never writes; graph stays a derived index, markdown+git canonical.
  - Bi-temporal: valid/event time via valid_at/invalid_at; record time defers to the
    edge's existing first_seen/last_seen (NOT duplicated here).
  - FAIL-OPEN + BACKCOMPAT: legacy edges without P0-b fields are handled gracefully
    (untimed -> always "current"; missing status -> treated provisional; missing
    scope -> owner). A pre-rebuild legacy graph still answers, just without temporal
    resolution. No crash on any field absence.
  - T1 / $0: pure dict + string-date compare. No LLM, no embed.
  - Reuses valid_time date semantics where available (fail-open if absent).

CLI:
  python3 tools/fact_query.py current  --subject dino --predicate lives_in
  python3 tools/fact_query.py as_of    --subject dino --predicate lives_in --date 2026-03-01
  python3 tools/fact_query.py history  --subject dino --predicate lives_in
  python3 tools/fact_query.py why      --subject dino --predicate lives_in
  python3 tools/fact_query.py what_changed --subject dino --from 2025-01-01 --to 2026-12-31
  (--json for machine output; --graph PATH to point at a different index)
"""
import argparse
import json
import sys
from pathlib import Path

# Workspace resolution: env > install-time sed > self-locate (matches memory_graph.py).
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run (live analyst / dev)
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
import os as _os
_WS = Path(_os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
GRAPH_PATH = _WS / "kb/memory_neuron/l2_graph/memory_graph.json"

# Optional valid-time date parsing (fail-open if the module isn't importable).
try:
    sys.path.insert(0, str(_WS / "procedures"))
    import valid_time as _VT  # noqa: E402
except Exception:
    _VT = None


def _load_graph(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _norm(s):
    return (s or "").strip().lower()


def _match(edge, subject, predicate):
    """subject+predicate match (case-insensitive). predicate=None matches any."""
    if _norm(edge.get("subject")) != _norm(subject):
        return False
    if predicate is not None and _norm(edge.get("label")) != _norm(predicate):
        return False
    return True


def _edges_for(graph, subject, predicate):
    return [e for e in graph.get("relation_edges", []) if _match(e, subject, predicate)]


def _valid_at(edge, date):
    """Is edge valid at `date`? valid_at <= date < invalid_at, open bounds, untimed=always."""
    va, iv = edge.get("valid_at"), edge.get("invalid_at")
    if not va and not iv:
        return True
    if va and date < va:
        return False
    if iv and date >= iv:
        return False
    return True


def _sort_key(edge):
    """Chronological sort: valid_at, then first_seen (record time), empty last."""
    return (edge.get("valid_at") or "", edge.get("first_seen") or "")


def current(graph, subject, predicate):
    """Latest fact valid now: prefer trusted+untimed-or-open-ended, newest valid_at."""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    cands = [e for e in _edges_for(graph, subject, predicate) if _valid_at(e, today)]
    if not cands:
        return None
    # trusted beats provisional; then newest valid_at/first_seen wins.
    cands.sort(key=lambda e: (e.get("status") == "trusted", _sort_key(e)))
    return cands[-1]


def as_of(graph, subject, predicate, date):
    """Fact believed valid at `date`."""
    cands = [e for e in _edges_for(graph, subject, predicate) if _valid_at(e, date)]
    if not cands:
        return None
    cands.sort(key=lambda e: (e.get("status") == "trusted", _sort_key(e)))
    return cands[-1]


def history(graph, subject, predicate):
    """Full supersession chain, oldest->newest."""
    edges = _edges_for(graph, subject, predicate)
    edges.sort(key=_sort_key)
    return edges


def why(graph, subject, predicate):
    """Evidence for the current fact: source memories, confidence, status, valid window."""
    cur = current(graph, subject, predicate)
    if cur is None:
        return None
    return {
        "subject": cur.get("subject"),
        "predicate": cur.get("label"),
        "object": cur.get("object"),
        "confidence": cur.get("confidence"),
        "status": cur.get("status", "provisional"),
        "scope": cur.get("scope", "owner"),
        "valid_at": cur.get("valid_at"),
        "invalid_at": cur.get("invalid_at"),
        "source_memory_ids": cur.get("source_memory_ids") or (
            [cur.get("source_file")] if cur.get("source_file") else []),
        "created_by": cur.get("created_by", cur.get("source", "unknown")),
        "first_seen": cur.get("first_seen"),  # record-time (neuron's existing clock)
    }


def what_changed(graph, subject, date_from, date_to):
    """State transitions for subject over [date_from, date_to]: edges whose valid window
    opens or closes inside the range. Returns chronological transition list."""
    out = []
    for e in _edges_for(graph, subject, None):
        va, iv = e.get("valid_at"), e.get("invalid_at")
        opened = va and date_from <= va <= date_to
        closed = iv and date_from <= iv <= date_to
        if opened or closed:
            out.append({
                "predicate": e.get("label"),
                "object": e.get("object"),
                "valid_at": va,
                "invalid_at": iv,
                "transition": "opened" if opened else "closed",
                "status": e.get("status", "provisional"),
                "source_memory_ids": e.get("source_memory_ids") or (
                    [e.get("source_file")] if e.get("source_file") else []),
            })
    out.sort(key=lambda t: (t.get("valid_at") or t.get("invalid_at") or ""))
    return out


def main():
    ap = argparse.ArgumentParser(description="P0-b fact-graph query layer (read-only)")
    ap.add_argument("query", choices=["current", "as_of", "history", "why", "what_changed"])
    ap.add_argument("--subject", required=True)
    ap.add_argument("--predicate", default=None)
    ap.add_argument("--date", default=None, help="for as_of")
    ap.add_argument("--from", dest="date_from", default=None, help="for what_changed")
    ap.add_argument("--to", dest="date_to", default=None, help="for what_changed")
    ap.add_argument("--graph", type=Path, default=GRAPH_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    graph = _load_graph(args.graph)
    if not graph.get("relation_edges"):
        msg = {"error": "no relation_edges in graph — run memory_graph.py to build"}
        print(json.dumps(msg) if args.json else msg["error"])
        sys.exit(0)

    if args.query == "current":
        res = current(graph, args.subject, args.predicate)
    elif args.query == "as_of":
        if not args.date:
            ap.error("as_of requires --date")
        res = as_of(graph, args.subject, args.predicate, args.date)
    elif args.query == "history":
        res = history(graph, args.subject, args.predicate)
    elif args.query == "why":
        res = why(graph, args.subject, args.predicate)
    else:  # what_changed
        if not (args.date_from and args.date_to):
            ap.error("what_changed requires --from and --to")
        res = what_changed(graph, args.subject, args.date_from, args.date_to)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if res is None:
            print(f"No fact found for {args.subject} / {args.predicate}")
        elif isinstance(res, list):
            if not res:
                print("(none)")
            for r in res:
                print(json.dumps(r, ensure_ascii=False))
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
