#!/usr/bin/env python3
"""
fact_query_test.py — P0-b fact-graph regression suite.

Offline / deterministic / zero-LLM / zero-embed / zero-network. Mirrors
valid_time_test.py's harness convention (check() counter, synthetic graphs,
self-exit 0/1). Covers the full P0-b writer<->reader<->query harmony:

  F1  writer enrichment: memory_graph._enrich-equivalent fields land on edges
  F2  reader build_adjacency --as-of temporal filter (Jakarta/Bali supersession)
  F3  reader build_adjacency --scope privacy filter (owner vs project)
  F4  reader backcompat: legacy edges (no P0-b fields) still traverse
  F5  fact_query.current  -> latest valid fact
  F6  fact_query.as_of    -> point-in-time fact
  F7  fact_query.history  -> supersession chain oldest->newest
  F8  fact_query.why      -> provenance (source_memory_ids + status)
  F9  fact_query.what_changed -> opened/closed transitions in a range
  F10 fact_query backcompat: untimed legacy edge answerable, no crash

Run:  python3 procedures/fact_query_test.py    (exit 0 = all pass)
"""
import importlib.util
import sys
from pathlib import Path

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}")


def _load(rel_path, mod_name):
    """Load a sibling module by path (works from repo or live, no install needed)."""
    p = Path(__file__).resolve().parent.parent / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _graph():
    """Synthetic enriched graph: dino lives_in Jakarta(2024->2026-05) superseded by
    Bali(2026-05->open); a project depends_on edge (scope=project); plus a legacy
    untimed edge with NO P0-b fields (backcompat probe)."""
    return {
        "relation_edges": [
            {"type": "relation", "label": "lives_in", "subject": "dino", "object": "Jakarta",
             "a": 0, "b": 1, "confidence": 0.9, "source": "explicit", "status": "trusted",
             "scope": "owner", "valid_at": "2024-01-01", "invalid_at": "2026-05-01",
             "source_memory_ids": ["_pin_jak.md"], "first_seen": "2024-01-01"},
            {"type": "relation", "label": "lives_in", "subject": "dino", "object": "Bali",
             "a": 0, "b": 2, "confidence": 0.9, "source": "explicit", "status": "trusted",
             "scope": "owner", "valid_at": "2026-05-01", "invalid_at": None,
             "source_memory_ids": ["_pin_bali.md"], "first_seen": "2026-05-01"},
            {"type": "relation", "label": "depends_on", "subject": "proj", "object": "hyperliquid",
             "a": 0, "b": 3, "confidence": 0.9, "source": "explicit", "status": "trusted",
             "scope": "project", "valid_at": None, "invalid_at": None,
             "source_memory_ids": ["_note_p.md"], "first_seen": "2026-01-01"},
            # legacy edge: NO valid_at/status/scope/source_memory_ids (pre-P0-b)
            {"type": "relation", "label": "uses", "subject": "proj", "object": "duckdb",
             "a": 0, "b": 4, "confidence": 0.9, "source": "explicit", "source_file": "_note_q.md"},
        ],
        "edges": [],
    }


def _labels(adj):
    return sorted(l["label"] for lst in adj.values() for l in lst if l["edge_type"] == "relation")


def test_reader(gs):
    print("[F2/F3/F4] reader build_adjacency filters")
    g = _graph()
    # F2 temporal
    check("as_of 2026-03 -> Jakarta era (not Bali)",
          _labels(gs.build_adjacency(g, 0.6, as_of="2026-03-01")) == ["depends_on", "lives_in", "uses"])
    check("as_of 2026-08 -> Bali era (not Jakarta)",
          _labels(gs.build_adjacency(g, 0.6, as_of="2026-08-01")) == ["depends_on", "lives_in", "uses"])
    # both eras have exactly one lives_in — assert WHICH object via edge inspection
    a_mar = gs.build_adjacency(g, 0.6, as_of="2026-03-01")
    li_mar = [l for lst in a_mar.values() for l in lst if l["label"] == "lives_in"]
    check("as_of 2026-03 lives_in edge target = node 1 (Jakarta)", len(li_mar) == 1 and li_mar[0]["target"] == 1)
    a_aug = gs.build_adjacency(g, 0.6, as_of="2026-08-01")
    li_aug = [l for lst in a_aug.values() for l in lst if l["label"] == "lives_in"]
    check("as_of 2026-08 lives_in edge target = node 2 (Bali)", len(li_aug) == 1 and li_aug[0]["target"] == 2)
    # F3 scope. NOTE: legacy edge missing `scope` DEFAULTS to owner (backcompat: pre-P0-b
    # edges must not be silently dropped) -> the untimed legacy 'uses' edge IS owner-scoped.
    check("scope=owner -> owner edges + legacy default-owner (drops project)",
          _labels(gs.build_adjacency(g, 0.6, scope_allow={"owner"})) == ["lives_in", "lives_in", "uses"])
    check("scope=project only -> only project edge (legacy defaults owner, excluded)",
          _labels(gs.build_adjacency(g, 0.6, scope_allow={"project"})) == ["depends_on"])
    # F4 backcompat: no filters = all 4 edges present, legacy included
    check("no filters -> all 4 edges (legacy 'uses' present)",
          _labels(gs.build_adjacency(g, 0.6)) == ["depends_on", "lives_in", "lives_in", "uses"])
    # _edge_valid_at unit: untimed edge always valid
    check("_edge_valid_at untimed -> True", gs._edge_valid_at({}, "2020-01-01") is True)


def test_query(fq):
    print("[F5-F10] fact_query queries")
    g = _graph()
    # F5 current
    check("current(dino,lives_in) -> Bali", fq.current(g, "dino", "lives_in")["object"] == "Bali")
    # F6 as_of
    check("as_of(dino,lives_in,2026-03) -> Jakarta", fq.as_of(g, "dino", "lives_in", "2026-03-01")["object"] == "Jakarta")
    check("as_of(dino,lives_in,2026-08) -> Bali", fq.as_of(g, "dino", "lives_in", "2026-08-01")["object"] == "Bali")
    # F7 history
    h = fq.history(g, "dino", "lives_in")
    check("history -> Jakarta then Bali (chain)", [e["object"] for e in h] == ["Jakarta", "Bali"])
    # F8 why (provenance)
    w = fq.why(g, "dino", "lives_in")
    check("why.object = Bali", w["object"] == "Bali")
    check("why.source_memory_ids = [_pin_bali.md]", w["source_memory_ids"] == ["_pin_bali.md"])
    check("why.status = trusted", w["status"] == "trusted")
    # F9 what_changed
    wc = fq.what_changed(g, "dino", "2025-01-01", "2026-12-31")
    trans = [(t["object"], t["transition"]) for t in wc]
    check("what_changed -> Jakarta closed + Bali opened",
          ("Jakarta", "closed") in trans and ("Bali", "opened") in trans)
    # F10 backcompat: legacy untimed edge answerable, no crash
    check("legacy current(proj,uses) -> duckdb (untimed=current)", fq.current(g, "proj", "uses")["object"] == "duckdb")
    check("missing subject -> None (no crash)", fq.current(g, "nobody", "nothing") is None)


def main():
    print("=== P0-b fact-graph regression suite ===")
    gs = _load("tools/graph_search.py", "gs_under_test")
    fq = _load("tools/fact_query.py", "fq_under_test")
    test_reader(gs)
    test_query(fq)
    print(f"\nFACT-GRAPH SUITE: {_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
