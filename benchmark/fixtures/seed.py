#!/usr/bin/env python3
"""Seed a self-contained mini memory corpus for the benchmark.

Why: the benchmark must be provable on ANY install, not just analyst's private
memory. This writes a small, known set of memory files + a graph that the
scenarios assert against. Deterministic content => reproducible scores.

Output goes to benchmark/fixtures/corpus/ (memory/*.md) and a prebuilt
graph.json, so run_benchmark.py points the tools at THIS corpus instead of the
live workspace.

The fixture facts are neutral (a fictional 'Project Atlas' + people) so they
never collide with real user memory and never leak private data into the repo.
"""
import json, os, hashlib
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
MEM = os.path.join(CORPUS, "memory")

# (filename, valid_at, invalid_at, scope, body, subject)
# subject groups a supersession CHAIN so absent-as_of resolution can keep only the
# CURRENT member (null valid_until). Non-chain facts use subject=None.
FACTS = [
    # --- temporal / longitudinal: a value that changes over time (subject atlas.lead) ---
    ("atlas_lead_v1.md", "2026-01-01", "2026-03-01", "project",
     "Project Atlas lead is Alice. Alice owns the roadmap.", "atlas.lead"),
    ("atlas_lead_v2.md", "2026-03-01", "2026-06-01", "project",
     "Project Atlas lead is Bob. Bob took over the roadmap from Alice.", "atlas.lead"),
    ("atlas_lead_v3.md", "2026-06-01", None, "project",
     "Project Atlas lead is Carol. Carol is the current roadmap owner.", "atlas.lead"),
    # --- contradiction: two facts, one superseded (subject atlas.database) ---
    ("atlas_db_old.md", "2026-01-01", "2026-05-01", "project",
     "Project Atlas uses Postgres as its database.", "atlas.database"),
    ("atlas_db_new.md", "2026-05-01", None, "project",
     "Project Atlas migrated to DuckDB. The database is DuckDB now, not Postgres.", "atlas.database"),
    # --- multi_hop: A depends_on B, B affects C (three distinct nodes) ---
    ("atlas_api.md", "2026-02-01", None, "project",
     "The Atlas API is the public entry point for Project Atlas.", None),
    ("atlas_cache.md", "2026-02-01", None, "project",
     "The Atlas cache layer sits behind the Atlas API.", None),
    ("atlas_latency.md", "2026-02-01", None, "project",
     "Atlas query latency is the key performance metric for Project Atlas.", None),
    # --- entity_res: aliases for one person ---
    ("robert_alias.md", "2026-02-01", None, "project",
     "Bob is also known as Robert Chen. Bob, Robert, and R. Chen are the same person.", None),
    # --- behavior / preference ---
    ("pref_reports.md", "2026-01-01", None, "owner",
     "Preference: Atlas status reports must be delivered as bullet points, never prose paragraphs.", None),
    # --- false_promotion: single unconfirmed rumor ---
    ("rumor_funding.md", "2026-04-01", None, "project",
     "Unconfirmed single-source rumor: Atlas may get a 10x budget increase. "
     "Not verified. Confidence low. Do not treat as fact.", None),
    # --- privacy_leak: a private/peer fact that must NOT surface in owner scope ---
    ("peer_secret.md", "2026-03-01", None, "private",
     "PRIVATE peer note: Dave's personal salary figure is confidential-xyz-123.", None),
    # --- noise: bulk irrelevant filler ---
    *[(f"noise_{i}.md", "2026-01-01", None, "world",
       f"Generic filler memo number {i} about unrelated logistics and scheduling minutiae.", None)
      for i in range(1, 21)],
    # --- recovery: a wrong fact later corrected (subject atlas.status) ---
    ("atlas_status_wrong.md", "2026-05-01", "2026-05-10", "project",
     "Atlas launch status: DELAYED indefinitely.", "atlas.status"),
    ("atlas_status_fixed.md", "2026-05-10", None, "project",
     "Correction: Atlas launched on schedule. Status is LIVE, not delayed.", "atlas.status"),
]


def _fm(valid_at, invalid_at, scope, subject=None):
    # field names MUST match valid_time.VT_FIELDS (valid_from/valid_until), else
    # read_fields returns all-None and is_valid_at fails open (=no filtering).
    lines = ["---", "type: fact", f"valid_from: {valid_at}"]
    if invalid_at:
        lines.append(f"valid_until: {invalid_at}")
    lines += [f"scope: {scope}"]
    if subject:
        lines.append(f"subject: {subject}")
    lines += [f"confidence: {'low' if scope=='project' else 'medium'}", "---", ""]
    return "\n".join(lines)


def _hid(fn):
    return hashlib.sha256(fn.encode()).hexdigest()


def _clean():
    # prune stale fixture files (scoped strictly to fixtures/corpus/memory) so a
    # re-seed after a rename never leaves orphans. No shell rm; bounded to *.md.
    if os.path.isdir(MEM):
        for f in os.listdir(MEM):
            if f.endswith(".md"):
                os.remove(os.path.join(MEM, f))


def seed():
    os.makedirs(MEM, exist_ok=True)
    _clean()
    for fn, va, iva, scope, body, subject in FACTS:
        with open(os.path.join(MEM, fn), "w") as fh:
            fh.write(_fm(va, iva, scope, subject) + body + "\n")

    # Build a small graph the tools can traverse: nodes + typed relation edges.
    nodes, node_idx = [], {}
    for i, (fn, va, iva, scope, body, subject) in enumerate(FACTS):
        node_idx[fn] = i
        nodes.append({"idx": i, "id": _hid(fn), "file": fn, "lines": [1, 9],
                      "degree": 0, "title": body[:50], "snippet": body[:180],
                      "valid_at": va, "invalid_at": iva, "scope": scope})

    # graph_search reads verb from `label` and endpoints from a/b (directed).
    rel = [
        {"a": node_idx["atlas_api.md"], "b": node_idx["atlas_cache.md"],
         "label": "depends_on", "confidence": 0.9, "scope": "project"},
        {"a": node_idx["atlas_cache.md"], "b": node_idx["atlas_latency.md"],
         "label": "affects", "confidence": 0.9, "scope": "project"},
        {"a": node_idx["atlas_lead_v1.md"], "b": node_idx["atlas_lead_v2.md"],
         "label": "superseded_by", "confidence": 1.0, "scope": "project",
         "valid_at": "2026-03-01"},
        {"a": node_idx["atlas_lead_v2.md"], "b": node_idx["atlas_lead_v3.md"],
         "label": "superseded_by", "confidence": 1.0, "scope": "project",
         "valid_at": "2026-06-01"},
        {"a": node_idx["atlas_db_old.md"], "b": node_idx["atlas_db_new.md"],
         "label": "superseded_by", "confidence": 1.0, "scope": "project",
         "valid_at": "2026-05-01"},
    ]

    graph = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "params": {"fixture": True},
        "node_count": len(nodes), "edge_count": 0,
        "relation_edge_count": len(rel), "entity_node_count": 0, "entity_edge_count": 0,
        "nodes": nodes, "edges": [], "relation_edges": rel,
        "entity_nodes": [], "entity_edges": [], "forward_refs": [],
    }
    with open(os.path.join(CORPUS, "graph.json"), "w") as fh:
        json.dump(graph, fh, indent=2)

    print(f"seeded {len(FACTS)} memory files + graph ({len(rel)} relation edges) -> {CORPUS}")


if __name__ == "__main__":
    seed()
