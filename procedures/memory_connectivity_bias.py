#!/usr/bin/env python3
"""
memory_connectivity_bias.py — NEURON-only retention keep-bias (soft).

The base retention rule (memory_retention.py) is graph-blind: not-[valid] at
terminal age -> delete. This neuron enhancement adds ONE thing on top: a SOFT
keep-bias that can rescue a not-fully-valid entry/file ONLY when it is a
structurally load-bearing HUB in the memory graph.

DESIGN (see _note_upcay-guillotine-fix-brainstorm.md, BENANG section):
- SOFT, never hard. It NUDGES borderline fate toward keep. It NEVER forces a
  deletion and NEVER overrides an [invalidated] or [noise] verdict (a
  well-connected but PROVABLY-FALSE fact is worse, not better — many things
  lean on wrong info). It rescues UNCERTAIN / MARGINAL-VALID hubs only.
- Metric = CROSS-FILE RECURRENCE-weighted importance, NOT raw similarity
  degree. A single high-sim edge is a dedup-twin (dedup owns that). Real
  importance = a concept that recurs across DIFFERENT files over time. Raw
  degree is gameable by verbosity; cross-file recurrence is not.
- Graph is a NEURON L2 feature. Base-only installs have no graph and simply
  don't run this. Regenerate the graph near review time or staleness biases on
  old structure.

OUTPUT: keep_bias_files() -> set of file basenames whose entries should be
soft-rescued from deletion. A retention pre-pass (neuron install only) consults
this set: for a file at terminal age whose verdict is uncertain/marginal, if it
is in the keep-set, spare it this cycle. invalidated/noise are never spared.

This module is READ-ONLY: it never edits or deletes memory files. It only reads
the graph and reports which files are hubs.
"""

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))

# Graph location (neuron L2). Overridable for the analyst live layout.
_GRAPH_CANDIDATES = [
    WORKSPACE / "kb" / "memory_neuron" / "l2_graph" / "memory_graph.json",
    WORKSPACE / "memory" / "_graph" / "memory_graph.json",
]

# A file's hub score must clear this to earn keep-bias. Tuned conservative:
# rescue only genuine hubs, not every node with one cross-file link.
HUB_SCORE_THRESHOLD = float(os.environ.get("DINOMEM_HUB_SCORE_THRESHOLD", "2.0"))

# Verdicts that connectivity may rescue. invalidated/noise are NEVER rescued.
RESCUABLE_VERDICTS = ("uncertain", "valid")  # 'valid' = marginal-valid; already kept, bias only fast-tracks


def _find_graph():
    for c in _GRAPH_CANDIDATES:
        if c.exists():
            return c
    return None


def load_graph(graph_path=None):
    p = Path(graph_path) if graph_path else _find_graph()
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def compute_hub_scores(graph):
    """
    Per-FILE cross-file-recurrence-weighted hub score.

    For each edge with cross_file=True, contribute recurrence weight to BOTH
    endpoint files. recurrence>1 means the connection re-formed across graph
    rebuilds (Hebbian persistence) — real structural importance. A single-shot
    cross-file edge (recurrence==1) contributes a small base weight; recurring
    ones scale up. Same-file edges contribute nothing (that's just intra-doc
    similarity / dedup territory).

    Returns {file_basename: score}.
    """
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return {}

    idx_to_file = {}
    for n in nodes:
        f = n.get("file", "")
        idx_to_file[n.get("idx")] = os.path.basename(f) if f else ""

    scores = defaultdict(float)
    for e in edges:
        if not e.get("cross_file"):
            continue
        rec = e.get("recurrence", 1) or 1
        # recurrence 1 -> 0.5 base; each extra recurrence adds 1.0. Cross-file
        # recurrence is the importance signal we weight on.
        w = 0.5 + 1.0 * (rec - 1)
        fa = idx_to_file.get(e.get("a"), "")
        fb = idx_to_file.get(e.get("b"), "")
        if fa:
            scores[fa] += w
        if fb and fb != fa:
            scores[fb] += w
    return dict(scores)


def keep_bias_files(graph_path=None, threshold=HUB_SCORE_THRESHOLD):
    """
    Set of file basenames that are cross-file hubs worth soft-rescuing.
    Empty set if no graph (base-only install / graph missing) — meaning NO
    bias, base rule applies unchanged.
    """
    graph = load_graph(graph_path)
    if not graph:
        return set()
    scores = compute_hub_scores(graph)
    return {f for f, s in scores.items() if s >= threshold}


def should_rescue(filename, verdict, graph_path=None, threshold=HUB_SCORE_THRESHOLD):
    """
    Soft keep-bias decision for one file/verdict.

    Returns True ONLY when: verdict is rescuable (uncertain / marginal-valid)
    AND the file is a cross-file hub. NEVER True for invalidated/noise —
    connectivity never immortalizes provably-false memory.
    """
    v = (verdict or "").strip().lstrip("[").rstrip("]").lower()
    if v not in RESCUABLE_VERDICTS:
        return False
    return os.path.basename(filename) in keep_bias_files(graph_path, threshold)


def main():
    ap = argparse.ArgumentParser(description="Neuron connectivity keep-bias (read-only report).")
    ap.add_argument("--graph", help="Path to memory_graph.json (default: auto-locate)")
    ap.add_argument("--threshold", type=float, default=HUB_SCORE_THRESHOLD,
                    help=f"Hub score threshold (default {HUB_SCORE_THRESHOLD})")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    graph = load_graph(args.graph)
    if not graph:
        print("No graph found — base-only install, no keep-bias applies.")
        return
    scores = compute_hub_scores(graph)
    hubs = {f: s for f, s in scores.items() if s >= args.threshold}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    if args.json:
        print(json.dumps({
            "threshold": args.threshold,
            "hub_files": sorted(hubs.keys()),
            "all_scores": dict(ranked),
        }, indent=2, ensure_ascii=False))
        return

    print(f"=== CONNECTIVITY KEEP-BIAS (threshold {args.threshold}) ===")
    print(f"Hub files (soft-rescue eligible for uncertain/marginal-valid): {len(hubs)}")
    for f, s in ranked:
        mark = "HUB" if s >= args.threshold else "   "
        print(f"  [{mark}] {s:6.2f}  {f}")


if __name__ == "__main__":
    main()
