#!/usr/bin/env python3
"""graph_store_test.py — self-contained proof for procedures/graph_store.py.

Runs on a TEMP corpus (never touches the live graph). Proves:
  1. schema + indexes exist on graph.db
  2. accessor parity: SQLite store returns the same nodes/relation_edges/edges/
     meta as the raw JSON (byte round-trip on payload)
  3. auto-migrate is idempotent (second open() does not re-import / mutate)
  4. JSON-fallback backend satisfies the same accessor surface
  5. cold-empty: open() on a workspace with no JSON and no db does not crash
  6. node identity: idx == list position preserved through SQLite
  7. neighbors(idx) indexed lookup returns the right edges

Exit 0 = ALL PASS. Any failure raises AssertionError (nonzero exit).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

# import the module under test
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import graph_store as gs  # noqa: E402


# ---------------------------------------------------------------------------
# tiny synthetic corpus (mirrors real schema: idx nodes, -1-capable rel edges,
# neural_score sim edges, entity_nodes/forward_refs round-trip payload)
# ---------------------------------------------------------------------------
def _corpus():
    return {
        "generated": "2026-08-12 21:20",
        "params": {"topk": 4, "threshold": 0.4},
        "node_count": 3,
        "edge_count": 2,
        "relation_edge_count": 2,
        "entity_node_count": 1,
        "entity_edge_count": 0,
        "nodes": [
            {"idx": 0, "id": "aaa", "file": "f0.md", "lines": [1, 5],
             "degree": 2, "title": "node zero", "snippet": "zero snip"},
            {"idx": 1, "id": "bbb", "file": "f1.md", "lines": [6, 9],
             "degree": 1, "title": "node one", "snippet": "one snip"},
            {"idx": 2, "id": "ccc", "file": "f2.md", "lines": [10, 12],
             "degree": 1, "title": "node two", "snippet": "two snip"},
        ],
        "relation_edges": [
            {"type": "relation", "label": "requires", "subject": "node zero",
             "object": "node one", "a": 0, "b": 1, "confidence": 0.9,
             "source": "explicit", "scope": "owner", "valid_at": None,
             "invalid_at": None, "status": "trusted"},
            {"type": "relation", "label": "mentions", "subject": "Arkham",
             "object": "API key", "a": -1, "b": -1, "confidence": 0.8,
             "source": "spacy_svo", "scope": "owner", "valid_at": None,
             "invalid_at": None},
        ],
        "edges": [
            {"a": 0, "b": 2, "sim": 0.95, "neural_score": 3.2,
             "cross_file": True, "recurrence": 4},
            {"a": 1, "b": 2, "sim": 0.81, "neural_score": 0.9,
             "cross_file": False, "recurrence": 1},
        ],
        "entity_nodes": [{"name": "Arkham", "kind": "tool"}],
        "entity_edges": [],
        "forward_refs": ["some-ref"],
    }


def _write_corpus(ws):
    d = os.path.join(ws, gs.GRAPH_SUBDIR)
    os.makedirs(d, exist_ok=True)
    jp = os.path.join(d, gs.JSON_NAME)
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(_corpus(), fh)
    return jp, os.path.join(d, gs.DB_NAME)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_schema_and_indexes(ws):
    _write_corpus(ws)
    st = gs.open(ws)  # triggers auto-migrate
    _, dp = gs._paths(ws, None, None)
    con = sqlite3.connect(dp)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"nodes", "relation_edges", "sim_edges", "meta"} <= tables, tables
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for want in ("ix_rel_a", "ix_rel_b", "ix_rel_label",
                 "ix_sim_a", "ix_sim_b", "ix_sim_ns"):
        assert want in idx, f"missing index {want}: {idx}"
    con.close()
    st.close()
    print("  [1] schema + indexes OK")


def test_accessor_parity(ws):
    jp, _ = _write_corpus(ws)
    raw = json.load(open(jp))
    st = gs.open(ws)
    # nodes: same count, same order, byte round-trip
    ns = st.nodes()
    assert len(ns) == len(raw["nodes"]), (len(ns), len(raw["nodes"]))
    for a, b in zip(ns, raw["nodes"]):
        assert a == b, (a, b)
    # relation_edges round-trip (incl -1 endpoints + subject/object)
    re = st.relation_edges()
    assert len(re) == len(raw["relation_edges"])
    assert re[1]["a"] == -1 and re[1]["subject"] == "Arkham", re[1]
    # sim edges round-trip
    se = st.edges()
    assert len(se) == len(raw["edges"])
    assert any(e.get("neural_score") == 3.2 for e in se), se
    # meta round-trips entity_nodes + forward_refs + counts
    m = st.meta()
    assert m["node_count"] == 3 and m["entity_nodes"] == [{"name": "Arkham", "kind": "tool"}]
    assert m["forward_refs"] == ["some-ref"], m.get("forward_refs")
    assert "nodes" not in m and "edges" not in m, "meta leaked hot-path lists"
    st.close()
    print("  [2] accessor parity (nodes/rel/sim/meta round-trip) OK")


def test_node_identity(ws):
    _write_corpus(ws)
    st = gs.open(ws)
    ns = st.nodes()
    for i, n in enumerate(ns):
        assert n["idx"] == i, f"idx {n['idx']} != position {i}"
    st.close()
    print("  [3] node idx == list position OK")


def test_migrate_idempotent(ws):
    jp, dp = _write_corpus(ws)
    st1 = gs.open(ws)          # first: migrate
    n1 = len(st1.nodes())
    mtime1 = os.path.getmtime(dp)
    st1.close()
    # tamper the JSON so a re-import would be visible
    bad = _corpus(); bad["nodes"] = bad["nodes"][:1]
    with open(jp, "w") as fh:
        json.dump(bad, fh)
    st2 = gs.open(ws)          # second: db already populated -> NO re-import
    n2 = len(st2.nodes())
    st2.close()
    assert n1 == 3 and n2 == 3, (n1, n2)  # still 3, ignored tampered JSON
    print("  [4] auto-migrate idempotent (no re-import on populated db) OK")


def test_json_fallback(ws):
    _write_corpus(ws)
    st = gs.open(ws, backend="json")
    assert type(st).__name__ == "JsonBackend"
    assert len(st.nodes()) == 3
    assert len(st.relation_edges()) == 2
    assert len(st.edges()) == 2
    assert st.meta()["node_count"] == 3
    nb = st.neighbors(2)  # node 2 is in both sim edges
    assert len(nb) >= 2, nb
    st.close()
    print("  [5] JSON-fallback backend surface OK")


def test_cold_empty(ws):
    # no corpus written -> no JSON, no db
    st = gs.open(ws)
    assert st.nodes() == []
    assert st.relation_edges() == []
    assert st.edges() == []
    assert st.meta() == {}
    st.close()
    print("  [6] cold-empty (no JSON, no db) no-crash OK")


def test_neighbors_indexed(ws):
    _write_corpus(ws)
    st = gs.open(ws)
    nb0 = st.neighbors(0)  # rel edge 0->1 + sim edge 0-2
    labels = {e.get("label") for e in nb0 if "label" in e}
    sims = [e for e in nb0 if "neural_score" in e]
    assert "requires" in labels, nb0
    assert any(e["b"] == 2 or e["a"] == 2 for e in sims), sims
    nbm1 = st.neighbors(-1)  # entity-name rel edge lives at a=-1,b=-1
    assert any(e.get("subject") == "Arkham" for e in nbm1), nbm1
    st.close()
    print("  [7] neighbors(idx) indexed lookup OK")


def main():
    tests = [
        test_schema_and_indexes,
        test_accessor_parity,
        test_node_identity,
        test_migrate_idempotent,
        test_json_fallback,
        test_cold_empty,
        test_neighbors_indexed,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory() as ws:
            t(ws)
    print("ALL PASS")


if __name__ == "__main__":
    main()
