#!/usr/bin/env python3
"""
code_query.py — query the ZERO-LLM code-structure graph (dinomem-neuron).

WHY
  Instead of grep+read whole files into context (~20x tokens, lower coverage on
  big repos), ask the pre-built structural index for a SCOPED subgraph: what a
  symbol calls / is called by / inherits / lives in, or the edge path between two
  symbols. The graph is built at $0 by procedures/code_graph.py (tree-sitter AST,
  no LLM). This tool is the read surface over kb/code_graph/.

REUSE (no fork — the whole point of build-quality)
  Imports graph_search.py's traversal primitives (load_graph/_GraphView,
  find_start_node, build_adjacency+bfs_traverse, indexed_bfs_traverse). Same
  store class, same idx==list-position contract, same edge shape. code_query only
  adds the CODE-specific surface: verbs (explain/path/query), file:line rendering,
  EXTRACTED/INFERRED provenance tags, --path subtree scope, staleness flag.

VERBS
  explain <symbol>   neighborhood of a symbol: contains/calls/called-by/imports/
                     inherits, each with file:line + provenance tag.
  path <A> <B>       shortest edge path A -> B (how one symbol reaches another).
  query "<nl>"       fuzzy-find the best-matching symbol, then explain it.

USAGE
  python3 tools/code_query.py explain diff_since [--hops 1] [--path procedures/]
  python3 tools/code_query.py path build_full replace_all
  python3 tools/code_query.py query "git incremental rebuild" [--json]
STALENESS: prints a warning when the code DB's built_at_sha != repo HEAD
  (run procedures/code_graph.py to refresh). Fail-open: no git -> no warning.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict, deque
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))              # graph_search.py (tools/)
sys.path.insert(0, str(_REPO_ROOT / "procedures"))  # graph_store / git_history

# Reuse graph_search traversal primitives — DO NOT reimplement.
import graph_search as _gs  # noqa: E402

try:
    import git_history as _gh  # noqa: E402
except Exception:
    _gh = None

CODE_JSON = _REPO_ROOT / "kb" / "code_graph" / "code_graph.json"


# ── DB open + staleness ──────────────────────────────────────────────────────
def _open(code_json: Path):
    if not code_json.exists():
        print(f"[code_query] no code graph at {code_json}\n"
              f"  build it: python3 procedures/code_graph.py --path <root>",
              file=sys.stderr)
        sys.exit(3)
    return _gs.load_graph(code_json)  # -> _GraphView (or dict on store-import fail)


def _meta(graph):
    """Meta dict from a _GraphView (reuse store.meta) or raw-json fallback."""
    store = getattr(graph, "_s", None)
    if store is not None and hasattr(store, "meta"):
        try:
            return store.meta()
        except Exception:
            return {}
    return graph if isinstance(graph, dict) else {}


def _staleness(graph) -> str | None:
    """Warn string when built_at_sha != repo HEAD, else None. Fail-open."""
    if _gh is None:
        return None
    meta = _meta(graph)
    built = meta.get("built_at_sha")
    root = meta.get("root")
    if not built or not root:
        return None
    try:
        head = (_gh._run(root, ["rev-parse", "HEAD"]) or "").strip()
    except Exception:
        return None
    if head and head != built:
        return (f"[stale] code graph built at {built[:8]} but HEAD is {head[:8]} — "
                f"run procedures/code_graph.py to refresh")
    return None


# ── scope + node helpers ─────────────────────────────────────────────────────
def _nodes(graph):
    return graph.get("nodes", [])  # _GraphView + dict both support .get()


def _in_scope(node, path_prefix):
    return path_prefix is None or node.get("file", "").startswith(path_prefix)


def _fmt_node(n):
    prov = ""  # nodes have no provenance; edges do
    return f"{n.get('kind','?')} {n.get('name','?')}  ({n.get('file','?')}:{n.get('line','?')})"


def _find_all(graph, symbol, path_prefix):
    """ALL nodes exactly named `symbol` (scope-filtered), defs before file nodes.
    Same-named symbols across files are AMBIGUOUS under name-only edge resolution
    (a call to `_run` resolves to one canonical idx); returning every candidate
    lets path/explain try each. Empty -> caller falls back to fuzzy."""
    nodes = _nodes(graph)
    exact = [(i, n) for i, n in enumerate(nodes)
             if n.get("name") == symbol and _in_scope(n, path_prefix)]
    exact.sort(key=lambda t: 0 if t[1].get("kind") != "file" else 1)
    return exact


def _find(graph, symbol, path_prefix):
    """Best single node: first exact (def-preferred), else fuzzy via
    graph_search.find_start_node. Returns (idx, node) or (None, None)."""
    exact = _find_all(graph, symbol, path_prefix)
    if exact:
        return exact[0]
    return _gs.find_start_node(graph, symbol)


# ── traversal (reuse graph_search; code graph has no sim edges/scope/time) ────
def _neighbors(graph, idx, hops):
    """Return graph_search-shaped visited records for idx's N-hop neighborhood.
    Uses the indexed store path when available, else adjacency fallback —
    identical to graph_search.main()'s dispatch."""
    nodes = _nodes(graph)
    store = getattr(graph, "_s", None)
    if store is not None and hasattr(store, "neighbors_filtered"):
        return _gs.indexed_bfs_traverse(idx, store, nodes, hops, 0.0)
    adj = _gs.build_adjacency(graph, 0.0)
    return _gs.bfs_traverse(idx, adj, nodes, hops)


def _edge_provenance(graph, a_idx, b_idx, label):
    """Look up the provenance tag (extracted|inferred) for a specific edge."""
    edges = graph.get("relation_edges", [])
    for e in edges:
        if e.get("a") == a_idx and e.get("b") == b_idx and e.get("label") == label:
            return e.get("provenance", "?")
    return "?"


# ── verb: explain ───────────────────────────────────────────
def explain(graph, symbol, hops, path_prefix):
    """Neighborhood of a symbol grouped by relation, each with file:line + tag.
    Also computes called-by (incoming calls) by scanning edges targeting idx."""
    idx, node = _find(graph, symbol, path_prefix)
    if idx is None:
        return {"ok": False, "symbol": symbol, "reason": "symbol not found"}
    nodes = _nodes(graph)
    visited = _neighbors(graph, idx, hops)
    # outgoing grouped by label
    groups = defaultdict(list)
    for r in visited:
        ni = nodes[r["idx"]]
        prov = _edge_provenance(graph, idx, r["idx"], r["label"]) if r["hop"] == 1 else "‑"
        groups[r["label"]].append({
            "target": _fmt_node(ni), "hop": r["hop"],
            "provenance": prov.upper() if prov not in ("?", "‑") else prov,
        })
    # incoming calls (called-by): edges whose b == idx
    edges = graph.get("relation_edges", [])
    called_by = []
    for e in edges:
        if e.get("b") == idx and e.get("label") == "calls":
            src = nodes[e["a"]] if 0 <= e.get("a", -1) < len(nodes) else None
            if src and _in_scope(src, path_prefix):
                called_by.append({"source": _fmt_node(src),
                                  "provenance": e.get("provenance", "?").upper()})
    return {"ok": True, "symbol": symbol, "node": _fmt_node(node),
            "outgoing": dict(groups), "called_by": called_by}


# ── verb: path (shortest edge path A -> B) ───────────────────────────
def find_path(graph, a_sym, b_sym, path_prefix, max_hops=6):
    """Shortest directed edge path a_sym -> b_sym over relation edges. BFS on the
    same adjacency graph_search builds (reused). Returns the labeled hop chain."""
    a_cands = _find_all(graph, a_sym, path_prefix) or ([_gs.find_start_node(graph, a_sym)] if _gs.find_start_node(graph, a_sym)[0] is not None else [])
    b_cands = _find_all(graph, b_sym, path_prefix) or ([_gs.find_start_node(graph, b_sym)] if _gs.find_start_node(graph, b_sym)[0] is not None else [])
    if not a_cands or not b_cands:
        miss = a_sym if not a_cands else b_sym
        return {"ok": False, "reason": f"symbol not found: {miss}"}
    nodes = _nodes(graph)
    adj = _gs.build_adjacency(graph, 0.0)
    b_set = {bi for bi, _ in b_cands}
    # multi-source BFS from every A candidate; first B candidate reached wins.
    prev = {}
    q = deque()
    for ai, _ in a_cands:
        prev[ai] = (None, None)
        q.append(ai)
    bi = None
    while q:
        cur = q.popleft()
        if cur in b_set:
            bi = cur
            break
        for e in adj.get(cur, []):
            t = e["target"]
            if t not in prev and 0 <= t < len(nodes):
                prev[t] = (cur, e["label"])
                q.append(t)
    if bi is None:
        return {"ok": False, "reason": f"no path {a_sym} -> {b_sym} within graph"}
    # reconstruct
    chain = []
    cur = bi
    while cur is not None:
        p, lbl = prev[cur]
        chain.append((cur, lbl))
        cur = p
    chain.reverse()
    steps = []
    for i, (ni, lbl) in enumerate(chain):
        n = nodes[ni]
        steps.append({"node": _fmt_node(n), "via": lbl if i > 0 else None})
    return {"ok": True, "from": a_sym, "to": b_sym, "hops": len(steps) - 1, "steps": steps}


# ── rendering ──────────────────────────────────────────────
def _render_explain(res):
    if not res["ok"]:
        return f"✗ {res['symbol']}: {res['reason']}"
    out = [f"◉ {res['node']}"]
    for label, items in res["outgoing"].items():
        out.append(f"  {label}:")
        for it in items:
            tag = f" [{it['provenance']}]" if it["provenance"] not in ("‑", "?") else ""
            out.append(f"    → {it['target']}{tag}" + (f"  (hop {it['hop']})" if it["hop"] > 1 else ""))
    if res["called_by"]:
        out.append("  called-by:")
        for cb in res["called_by"][:25]:
            out.append(f"    ← {cb['source']} [{cb['provenance']}]")
    return "\n".join(out)


def _render_path(res):
    if not res["ok"]:
        return f"✗ {res['reason']}"
    out = [f"◉ {res['from']} → {res['to']}  ({res['hops']} hop(s))"]
    for i, s in enumerate(res["steps"]):
        arrow = f"  ─{s['via']}→ " if s["via"] else ""
        out.append(f"  {i}. {arrow}{s['node']}")
    return "\n".join(out)


# ── main ────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Query the ZERO-LLM code-structure graph (dinomem-neuron)")
    sub = ap.add_subparsers(dest="verb", required=True)
    pe = sub.add_parser("explain", help="neighborhood of a symbol")
    pe.add_argument("symbol")
    pe.add_argument("--hops", type=int, default=1)
    pp = sub.add_parser("path", help="shortest edge path A -> B")
    pp.add_argument("a"); pp.add_argument("b")
    pq = sub.add_parser("query", help="fuzzy-find a symbol then explain it")
    pq.add_argument("text")
    pq.add_argument("--hops", type=int, default=1)
    for p in (pe, pp, pq):
        p.add_argument("--path", dest="path_prefix", default=None,
                       help="scope to a file-path prefix (e.g. procedures/)")
        p.add_argument("--db", dest="db", default=str(CODE_JSON),
                       help="code graph json path (default: kb/code_graph/code_graph.json)")
        p.add_argument("--json", action="store_true")
    args = ap.parse_args()

    graph = _open(Path(args.db))
    stale = _staleness(graph)

    if args.verb == "explain":
        res = explain(graph, args.symbol, args.hops, args.path_prefix)
        rendered = _render_explain(res)
    elif args.verb == "path":
        res = find_path(graph, args.a, args.b, args.path_prefix)
        rendered = _render_path(res)
    else:  # query
        idx, node = _find(graph, args.text, args.path_prefix)
        if idx is None:
            res = {"ok": False, "symbol": args.text, "reason": "no fuzzy match"}
            rendered = f"✗ no symbol matched '{args.text}'"
        else:
            res = explain(graph, node.get("name"), args.hops, args.path_prefix)
            rendered = f"(matched '{node.get('name')}')\n" + _render_explain(res)

    if args.json:
        if stale:
            res["staleness"] = stale
        print(json.dumps(res, indent=2))
    else:
        if stale:
            print(stale, file=sys.stderr)
        print(rendered)


if __name__ == "__main__":
    main()
