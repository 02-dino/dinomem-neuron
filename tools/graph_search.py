#!/usr/bin/env python3
"""
Graph Search CLI - Traverse typed relation edges in memory_graph.json

Usage:
    python3 tools/graph_search.py "<query>" [--hops N] [--min-confidence F] [--output-format text|json]

Examples:
    python3 tools/graph_search.py "sessions_spawn" --hops 2 --min-confidence 0.6
    python3 tools/graph_search.py "project advancer" --hops 3 --output-format json
"""

import sys
import json
import re
import argparse
import difflib
from pathlib import Path
from collections import defaultdict, deque

# Resolve graph path relative to repo root (parent of tools/)
_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent
GRAPH_PATH = _REPO_ROOT / "kb" / "memory_neuron" / "l2_graph" / "memory_graph.json"


class _GraphView:
    """Dict-compatible view over a graph_store backend. Exposes exactly the
    .get("nodes"|"edges"|"relation_edges") + "in" surface graph_search uses, so
    every existing call site (build_adjacency, find_clusters, cluster_drill,
    collect_forward_refs, main) stays byte-identical while the underlying load
    becomes indexed SQLite (auto-migrated). Hot-path node/edge lists are cached
    lazily; a query that only needs a start node never materializes edges."""

    def __init__(self, store):
        self._s = store
        self._cache = {}

    def _mat(self, key):
        if key not in self._cache:
            if key == "nodes":
                self._cache[key] = self._s.nodes()
            elif key == "relation_edges":
                self._cache[key] = self._s.relation_edges()
            elif key == "edges":
                self._cache[key] = self._s.edges()
            else:
                self._cache[key] = self._s.meta().get(key)
        return self._cache[key]

    def get(self, key, default=None):
        val = self._mat(key)
        return default if val is None else val

    def __contains__(self, key):
        # 'relation_edges' in graph gate: true when the graph has any (or the
        # key exists in meta). Preserves has_relation_edges semantics.
        if key in ("nodes", "edges", "relation_edges"):
            return bool(self._mat(key))
        return key in self._s.meta()


def load_graph(graph_path: Path) -> dict:
    # graph_store lives in ../procedures relative to this tools/ file.
    sys.path.insert(0, str(_REPO_ROOT / "procedures"))
    try:
        import graph_store as _gstore
    except Exception:
        # hard fallback: raw JSON (keeps the tool working even if store import fails)
        if not graph_path.exists():
            print(f"Error: Graph file not found at {graph_path}", file=sys.stderr)
            print("Run memory_graph.py to build the graph first.", file=sys.stderr)
            sys.exit(1)
        with open(graph_path, "r", encoding="utf-8") as f:
            return json.load(f)
    # store selects SQLite (indexed) by default, auto-migrating from the JSON at
    # graph_path on first run. json_path pinned to the CLI --graph path so a
    # custom path still works; db sits beside it.
    store = _gstore.open(json_path=str(graph_path))
    if not store.nodes():
        print(f"Error: Graph is empty (no nodes) at {graph_path}", file=sys.stderr)
        print("Run memory_graph.py to build the graph first.", file=sys.stderr)
        sys.exit(1)
    return _GraphView(store)


# WHY: dinomem memory-note nodes carry a YAML frontmatter header in title/snippet
# ("--- type: decision date: 2026-08-16 expires: ... ctx: X topics: Y ---"). ~94% of
# live-graph node titles ARE that header, so the old title-weighted difflib match
# scored against boilerplate and near-always missed on CONTENT queries. This strips
# the header down to its semantic payload (ctx/topics values + the body after the
# closing ---) so matching keys on meaning, not metadata.
_HEADER_RE = re.compile(r"^---.*?---", re.DOTALL)
_HEADER_KV_RE = re.compile(r"\b(?:ctx|topics|title|subject|entity|about)\s*:\s*([^\n]+)")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _content_text(title: str, snippet: str) -> str:
    """Return the semantic payload of a node: keep ctx/topics VALUES from the
    frontmatter (they are content), drop the type/date/expires boilerplate, and
    keep everything after the closing '---'. Falls back to the raw text when there
    is no recognizable header."""
    raw = f"{title}\n{snippet}".strip()
    kv_vals = " ".join(_HEADER_KV_RE.findall(raw))
    body = _HEADER_RE.sub(" ", raw, count=1)  # text after the frontmatter block
    out = f"{kv_vals} {body}".strip()
    return (out or raw).lower()


def _tokens(s: str) -> set:
    return set(_TOKEN_RE.findall(s.lower()))


def _match_score(query_lower: str, content_lower: str) -> float:
    """Best of: exact substring (1.0), token-CONTAINMENT (frac of query tokens
    present in content — order/length robust, the axis difflib whole-string ratio
    fails), and a difflib fallback for typo tolerance. Token-containment is the
    fix: a short query vs a long note no longer self-penalizes on length."""
    if not content_lower or not query_lower:
        return 0.0
    if query_lower in content_lower:
        return 1.0
    q_toks = _tokens(query_lower)
    if q_toks:
        c_toks = _tokens(content_lower)
        containment = len(q_toks & c_toks) / len(q_toks)
    else:
        containment = 0.0
    fuzzy = difflib.SequenceMatcher(None, query_lower, content_lower).ratio()
    return max(containment, fuzzy)


def find_start_node(graph: dict, query: str, threshold: float = 0.60) -> tuple[int, dict] | tuple[None, None]:
    """Fuzzy match query against a node's SEMANTIC content (frontmatter header
    stripped to ctx/topics + body). Token-containment scored so length/word-order
    mismatch no longer buries a real hit. Returns (idx, node) or (None, None)."""
    nodes = graph.get("nodes", [])
    query_lower = query.lower()
    best_score = 0.0
    best_idx = None
    best_node = None

    for i, node in enumerate(nodes):
        content = _content_text(node.get("title", ""), node.get("snippet", ""))
        score = _match_score(query_lower, content)
        if score > best_score and score >= threshold:
            best_score = score
            best_idx = i
            best_node = node

    return best_idx, best_node


def _edge_valid_at(edge: dict, as_of: str) -> bool:
    """P0-b temporal filter: is this fact edge valid at `as_of` (ISO date)?
    valid_at <= as_of < invalid_at, treating missing bounds as open. Edges with NO
    valid_at are ALWAYS kept (untimed facts, fail-open). Reuses the same interval
    semantics as valid_time / hybrid_recall as_of. Pure string date compare, $0."""
    va = edge.get("valid_at")
    iv = edge.get("invalid_at")
    if not va and not iv:
        return True  # untimed edge -> always valid (fail-open)
    if va and as_of < va:
        return False
    if iv and as_of >= iv:
        return False
    return True


def build_adjacency(graph: dict, min_confidence: float,
                    scope_allow: set | None = None, as_of: str | None = None) -> dict[int, list[dict]]:
    """Build adjacency from relation_edges + similarity edges.

    P0-b additions (both DEFAULT-OFF = today's exact behavior when unset):
      scope_allow: if given, keep only relation edges whose `scope` is in the set
                   (PRIVACY: filter applied HERE, BEFORE traversal, per proposal).
                   This is EDGE-level scope WITHIN the main graph — complementary to,
                   not a replacement for, the peers-graph/--owner gate in main().
      as_of:       if given, keep only relation edges valid at that date (temporal).
    Similarity edges are untyped/untimed/unscoped by construction, so the filters
    apply to relation edges only (similarity edges pass through unchanged)."""
    adj = defaultdict(list)
    has_relation_edges = "relation_edges" in graph

    if has_relation_edges:
        for edge in graph.get("relation_edges", []):
            # memory_graph.py writes endpoints as a/b, verb as label,
            # and 'source' is provenance (explicit|spacy_svo), NOT an endpoint.
            src = edge.get("a")
            tgt = edge.get("b")
            conf = edge.get("confidence", 0.0)
            label = edge.get("label", "related_to")
            # -1 marks an unmatched endpoint; skip those.
            if src is None or tgt is None or src < 0 or tgt < 0:
                continue
            # P0-b PRIVACY: scope filter BEFORE traversal. Edges missing `scope`
            # (legacy pre-P0-b edges) default to 'owner' so they're not silently dropped.
            if scope_allow is not None and edge.get("scope", "owner") not in scope_allow:
                continue
            # P0-b TEMPORAL: drop edges not valid at as_of (untimed edges kept).
            if as_of is not None and not _edge_valid_at(edge, as_of):
                continue
            if conf >= min_confidence:
                # relation edges are directed a -> b
                adj[src].append({"target": tgt, "label": label, "confidence": conf, "edge_type": "relation"})
    else:
        print("Warning: relation_edges not yet available — run memory_graph.py to rebuild graph", file=sys.stderr)

    # Secondary: high-confidence similarity edges (>= 0.80).
    # memory_graph.py writes endpoints as a/b and score as neural_score (composite) with sim fallback.
    for edge in graph.get("edges", []):
        src = edge.get("a")
        tgt = edge.get("b")
        sim = edge.get("neural_score", edge.get("sim", edge.get("weight", edge.get("similarity", 0.0))))
        if src is None or tgt is None:
            continue
        if sim >= 0.80:
            # similarity edges are undirected — add both directions
            adj[src].append({"target": tgt, "label": "similar_to", "confidence": sim, "edge_type": "similarity"})
            adj[tgt].append({"target": src, "label": "similar_to", "confidence": sim, "edge_type": "similarity"})

    return adj


def bfs_traverse(
    start_idx: int,
    adj: dict[int, list[dict]],
    nodes: list[dict],
    max_hops: int,
) -> list[dict]:
    """BFS traversal. Returns list of visited node records with hop/relation info."""
    visited = {start_idx}
    queue = deque()
    # Seed with neighbors of start node
    for edge in adj.get(start_idx, []):
        tgt = edge["target"]
        if tgt not in visited and 0 <= tgt < len(nodes):
            queue.append({
                "idx": tgt,
                "hop": 1,
                "label": edge["label"],
                "confidence": edge["confidence"],
                "edge_type": edge["edge_type"],
                "via": start_idx,
            })
            visited.add(tgt)

    results = []
    while queue:
        item = queue.popleft()
        idx = item["idx"]
        hop = item["hop"]
        node = nodes[idx]
        results.append({
            "idx": idx,
            "hop": hop,
            "label": item["label"],
            "confidence": item["confidence"],
            "edge_type": item["edge_type"],
            "via": item["via"],
            "title": node.get("title", f"node_{idx}"),
            "snippet": node.get("snippet", ""),
        })

        if hop < max_hops:
            for edge in adj.get(idx, []):
                tgt = edge["target"]
                if tgt not in visited and 0 <= tgt < len(nodes):
                    queue.append({
                        "idx": tgt,
                        "hop": hop + 1,
                        "label": edge["label"],
                        "confidence": edge["confidence"],
                        "edge_type": edge["edge_type"],
                        "via": idx,
                    })
                    visited.add(tgt)

    return results


def indexed_bfs_traverse(start_idx, store, nodes, max_hops, min_confidence,
                         scope_allow=None, as_of=None):
    """BFS that expands each node via store.neighbors_filtered() (indexed) instead
    of pre-building a full adjacency map over the whole edge list. Behaviorally
    IDENTICAL to build_adjacency + bfs_traverse (same filters, same directed
    relation / undirected similarity semantics, same visited-set dedup, same
    field shape), but only touches the start node's N-hop neighborhood — so it
    scales to graphs where loading every edge is prohibitive."""
    visited = {start_idx}
    queue = deque()
    for edge in store.neighbors_filtered(start_idx, min_confidence, scope_allow, as_of):
        tgt = edge["target"]
        if tgt not in visited and 0 <= tgt < len(nodes):
            queue.append({"idx": tgt, "hop": 1, "label": edge["label"],
                          "confidence": edge["confidence"],
                          "edge_type": edge["edge_type"], "via": start_idx})
            visited.add(tgt)
    results = []
    while queue:
        item = queue.popleft()
        idx = item["idx"]; hop = item["hop"]
        node = nodes[idx]
        results.append({
            "idx": idx, "hop": hop, "label": item["label"],
            "confidence": item["confidence"], "edge_type": item["edge_type"],
            "via": item["via"], "title": node.get("title", f"node_{idx}"),
            "snippet": node.get("snippet", ""),
        })
        if hop < max_hops:
            for edge in store.neighbors_filtered(idx, min_confidence, scope_allow, as_of):
                tgt = edge["target"]
                if tgt not in visited and 0 <= tgt < len(nodes):
                    queue.append({"idx": tgt, "hop": hop + 1, "label": edge["label"],
                                  "confidence": edge["confidence"],
                                  "edge_type": edge["edge_type"], "via": idx})
                    visited.add(tgt)
    return results


def collect_forward_refs(graph: dict, visited_idxs: set, nodes: list[dict]) -> dict[str, int]:
    """Find terms mentioned in visited nodes that aren't themselves graph nodes."""
    # Gather all titles of visited nodes
    visited_titles = {nodes[i].get("title", "").lower() for i in visited_idxs if i < len(nodes)}
    all_titles = {n.get("title", "").lower() for n in nodes}

    # Look for quoted or bracketed references in snippets
    import re
    ref_counts: dict[str, int] = defaultdict(int)
    pattern = re.compile(r'["`\']([\w\s/_.*-]{3,40})["`\']')

    for i in visited_idxs:
        if i >= len(nodes):
            continue
        snippet = nodes[i].get("snippet", "")
        for match in pattern.finditer(snippet):
            term = match.group(1).strip()
            term_lower = term.lower()
            if term_lower not in all_titles and term_lower not in visited_titles:
                ref_counts[term] += 1

    # Return refs mentioned in 2+ nodes
    return {k: v for k, v in ref_counts.items() if v >= 2}


def format_text(
    query: str,
    start_idx: int,
    start_node: dict,
    results: list[dict],
    forward_refs: dict[str, int],
    max_hops: int,
    min_confidence: float,
) -> str:
    lines = []
    lines.append(f'Graph search: "{query}" ({max_hops} hops, min confidence {min_confidence})')
    lines.append(f'Starting node: {start_node.get("title", f"node_{start_idx}")} [idx: {start_idx}]')

    if not results:
        lines.append("\nNo connected nodes found within constraints.")
        return "\n".join(lines)

    # Group by hop
    by_hop: dict[int, list[dict]] = defaultdict(list)
    for r in results:
        by_hop[r["hop"]].append(r)

    for hop in sorted(by_hop.keys()):
        lines.append(f"\nHop {hop}:")
        for r in sorted(by_hop[hop], key=lambda x: -x["confidence"]):
            snippet_preview = r["snippet"][:100].replace("\n", " ") if r["snippet"] else ""
            lines.append(f'  → {r["title"]} ({r["label"]}, conf: {r["confidence"]:.2f})')
            if snippet_preview:
                lines.append(f'    "{snippet_preview}"')

    if forward_refs:
        lines.append("\nForward refs mentioned but not in graph:")
        for term, count in sorted(forward_refs.items(), key=lambda x: -x[1]):
            lines.append(f'  - "{term}" (mentioned in {count} nodes)')

    return "\n".join(lines)


def format_json(
    query: str,
    start_idx: int,
    start_node: dict,
    results: list[dict],
    forward_refs: dict[str, int],
    max_hops: int,
    min_confidence: float,
) -> str:
    output = {
        "query": query,
        "max_hops": max_hops,
        "min_confidence": min_confidence,
        "start_node": {
            "idx": start_idx,
            "title": start_node.get("title", f"node_{start_idx}"),
            "snippet": start_node.get("snippet", ""),
        },
        "results": results,
        "forward_refs": forward_refs,
        "total_nodes_found": len(results),
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


# ─── Cluster / hierarchical retrieval (additive; ported concept from OpenViking's
# directory-recursive retrieval) ──────────────────────────────────────────────
#
# Constraint (verified): memory_graph.json is a FLAT similarity graph, not a
# directory tree -- there is no viking://-style hierarchy to drill into. The
# honest equivalent is: treat each CONNECTED COMPONENT (cluster of nodes linked
# by high-confidence similarity edges) as the "directory", score the cluster by
# its best-matching member first, then only expand into that cluster's members
# instead of scanning all 2000+ nodes flat. This gives the same practical win
# OpenViking's tree-drill gives (coarse-to-fine, avoid scanning everything) using
# the data structure that actually exists here -- no rewrite of the flat graph.
#
# This is purely ADDITIVE: existing find_start_node/build_adjacency/bfs_traverse/
# format_text/format_json/main() are untouched, so all current CLI behavior is
# unchanged. New behavior only activates via the --cluster-drill flag.

def build_similarity_adjacency(graph, min_sim=0.80):
    """Undirected adjacency from similarity edges only (>= min_sim), used purely
    to discover connected components (clusters) -- separate from build_adjacency's
    typed-edge traversal so we never touch that existing function's behavior."""
    adj = defaultdict(set)
    for edge in graph.get("edges", []):
        a, b = edge.get("a"), edge.get("b")
        sim = edge.get("neural_score", edge.get("sim", 0.0))
        if a is None or b is None:
            continue
        if sim >= min_sim:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def find_clusters(graph, min_sim=0.80):
    """Connected components over the similarity graph. Returns list of sets of node idxs.
    Singletons (no edge >= min_sim) are each their own 1-node cluster."""
    nodes = graph.get("nodes", [])
    adj = build_similarity_adjacency(graph, min_sim)
    seen = set()
    clusters = []
    for i in range(len(nodes)):
        if i in seen:
            continue
        comp = set()
        q = deque([i])
        seen.add(i)
        while q:
            cur = q.popleft()
            comp.add(cur)
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        clusters.append(comp)
    return clusters


def cluster_drill_search(query, graph, min_sim=0.80, top_clusters=3, per_cluster=5):
    """
    Two-stage retrieval (coarse-to-fine), the cluster-drill equivalent of
    OpenViking's directory-recursive retrieval:
      Stage 1: fuzzy-score EVERY node (cheap, difflib -- same primitive
               find_start_node already uses), group into clusters, rank
               clusters by their single best-scoring member.
      Stage 2: only within the top-N clusters, return the top-per-cluster
               matches -- so the agent gets "here's the neighborhood, and
               the best items in it" instead of one flat top-k list with no
               structural context.

    Also builds a TRAJECTORY (ported concept: OpenViking's "observable
    retrieval" -- every query preserves its browsing path so a bad result is
    debuggable). Returned under 'trajectory'.
    """
    nodes = graph.get("nodes", [])
    query_lower = query.lower()

    scores = {}
    for i, node in enumerate(nodes):
        title = node.get("title", "")
        snippet = node.get("snippet", "")
        combined = f"{title} {snippet}".lower()
        if query_lower in combined:
            score = 1.0 if query_lower in title.lower() else 0.85
        else:
            title_score = difflib.SequenceMatcher(None, query_lower, title.lower()).ratio()
            snippet_score = difflib.SequenceMatcher(None, query_lower, combined).ratio()
            score = max(title_score, snippet_score * 0.9)
        scores[i] = score

    clusters = find_clusters(graph, min_sim)
    cluster_best = []
    for ci, comp in enumerate(clusters):
        best = max((scores.get(i, 0.0) for i in comp), default=0.0)
        cluster_best.append((best, ci, comp))
    cluster_best.sort(key=lambda t: -t[0])

    trajectory = []
    matched = []
    for rank, (best_score, ci, comp) in enumerate(cluster_best):
        considered = rank < max(top_clusters * 3, top_clusters)
        entered = rank < top_clusters and best_score > 0.0
        if considered:
            trajectory.append({
                "cluster_idx": ci,
                "cluster_size": len(comp),
                "best_score": round(best_score, 4),
                "entered": entered,
            })
        if not entered:
            continue
        members = sorted(comp, key=lambda i: -scores.get(i, 0.0))[:per_cluster]
        for idx in members:
            node = nodes[idx]
            matched.append({
                "idx": idx,
                "cluster_idx": ci,
                "score": round(scores.get(idx, 0.0), 4),
                "title": node.get("title", f"node_{idx}"),
                "snippet": node.get("snippet", ""),
                "file": node.get("file", ""),
            })

    return {
        "query": query,
        "total_clusters": len(clusters),
        "clusters_entered": sum(1 for t in trajectory if t["entered"]),
        "trajectory": trajectory,
        "matches": matched,
    }


def format_cluster_drill_text(result):
    lines = []
    lines.append(f'Cluster-drill search: "{result["query"]}"')
    lines.append(f'{result["total_clusters"]} total clusters, '
                 f'{result["clusters_entered"]} entered')
    lines.append("\nTrajectory (which clusters were considered/entered, in score order):")
    for t in result["trajectory"]:
        marker = "→ ENTERED" if t["entered"] else "  skipped"
        lines.append(f'  cluster #{t["cluster_idx"]} (size {t["cluster_size"]}, '
                     f'best {t["best_score"]:.2f}) {marker}')
    if not result["matches"]:
        lines.append("\nNo matches found.")
        return "\n".join(lines)
    lines.append("\nMatches (grouped by cluster):")
    last_ci = None
    for m in result["matches"]:
        if m["cluster_idx"] != last_ci:
            lines.append(f'\n  [cluster #{m["cluster_idx"]}]')
            last_ci = m["cluster_idx"]
        snippet_preview = m["snippet"][:100].replace("\n", " ") if m["snippet"] else ""
        lines.append(f'    → {m["title"]} (score: {m["score"]:.2f}) [{m["file"]}]')
        if snippet_preview:
            lines.append(f'      "{snippet_preview}"')
    return "\n".join(lines)


# ═══ PEER-SCOPED SEARCH (NEURON — peers_graph.json, privacy-by-construction) ═══
# peers_graph.json is a SEPARATE graph from memory_graph.json. Its edges use
# string endpoints {subject, verb, object} where subject is `peer:<platform>:<id>`.
# ISOLATION RULE (OQ6): a peer's edges may surface ONLY when --peer matches that
# subject (active-speaker). Cross-peer aggregation (--all-peers, e.g. "who trades
# ETH") is OWNER-ONLY and MUST be gated by --owner. Fail-open on any error.
PEERS_GRAPH_PATH = _REPO_ROOT / "memory" / "peers" / "peers_graph.json"

def load_peers_graph(path: Path) -> dict:
    try:
        if not path.exists():
            return {"nodes": {}, "edges": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "edges" in data:
            data.setdefault("nodes", {})
            return data
    except Exception as e:
        print(f"Warning: peers_graph load failed ({e})", file=sys.stderr)
    return {"nodes": {}, "edges": []}

def normalize_peer_subject(peer: str) -> str:
    """Accept `platform:id`, `platform_id`, or full `peer:platform:id` -> canonical
    `peer:<platform>:<id>` subject key."""
    p = (peer or "").strip()
    if p.startswith("peer:"):
        return p
    if ":" in p:
        plat, pid = p.split(":", 1)
        return f"peer:{plat.lower()}:{pid}"
    if "_" in p:
        plat, pid = p.split("_", 1)
        return f"peer:{plat.lower()}:{pid}"
    return f"peer:{p}"

def peer_scoped_search(graph: dict, subject: str, query: str, min_confidence: float) -> dict:
    """Return edges for ONE peer subject, optionally filtered by a query substring
    on verb/object. Never returns any other peer's edges."""
    q = (query or "").strip().lower()
    out = []
    for e in graph.get("edges", []):
        if e.get("subject") != subject:
            continue
        if e.get("confidence", 0.0) < min_confidence:
            continue
        if q and q not in e.get("verb", "").lower() and q not in e.get("object", "").lower():
            continue
        out.append(e)
    out.sort(key=lambda x: (-x.get("confidence", 0.0), -x.get("count", 0)))
    return {"scope": "peer", "subject": subject, "query": query, "edges": out}

def all_peers_search(graph: dict, query: str, min_confidence: float) -> dict:
    """OWNER-ONLY cross-peer aggregation. Group peers by matching object/verb.
    Caller MUST have gated this behind --owner before calling."""
    q = (query or "").strip().lower()
    hits = []
    for e in graph.get("edges", []):
        if e.get("confidence", 0.0) < min_confidence:
            continue
        if q and q not in e.get("verb", "").lower() and q not in e.get("object", "").lower():
            continue
        hits.append(e)
    hits.sort(key=lambda x: (x.get("object", ""), -x.get("confidence", 0.0)))
    return {"scope": "all_peers", "query": query, "edges": hits}

def format_peer_text(result: dict) -> str:
    lines = []
    if result["scope"] == "peer":
        lines.append(f'Peer-scoped graph: {result["subject"]}'
                     + (f'  (filter: "{result["query"]}")' if result.get("query") else ""))
    else:
        lines.append(f'Cross-peer (OWNER) aggregation'
                     + (f'  (filter: "{result["query"]}")' if result.get("query") else ""))
    edges = result.get("edges", [])
    if not edges:
        lines.append("  (no matching edges)")
        return "\n".join(lines)
    for e in edges:
        subj = e.get("subject", "?")
        lines.append(f'  {subj} → {e.get("verb")} → {e.get("object")}  '
                     f'(conf {e.get("confidence", 0):.2f}, seen {e.get("count", 1)}x, '
                     f'last {e.get("last_seen", "?")})')
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(
        description="Traverse typed relation edges in memory_graph.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", help="Concept to start from (fuzzy matched against node titles/snippets)")
    parser.add_argument("--hops", type=int, default=2, help="Max traversal depth (default: 2)")
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="P0-b temporal: traverse only fact edges valid at this ISO date "
                             "(untimed edges always kept). Omit = current graph.")
    parser.add_argument("--scope", dest="scope", default=None,
                        help="P0-b privacy: comma-sep edge scopes to allow (owner,project,peer,world,private). "
                             "Filter applied BEFORE traversal. Omit = all scopes.")
    parser.add_argument("--min-confidence", type=float, default=0.6, dest="min_confidence",
                        help="Minimum edge confidence to follow (default: 0.6)")
    parser.add_argument("--output-format", choices=["text", "json"], default="text", dest="output_format",
                        help="Output format: text (default) or json")
    parser.add_argument("--graph", type=Path, default=GRAPH_PATH,
                        help=f"Path to memory_graph.json (default: {GRAPH_PATH})")
    parser.add_argument("--cluster-drill", action="store_true", dest="cluster_drill",
                        help="Use cluster-drill (hierarchical) retrieval instead of edge-BFS from a "
                             "single start node. Ported concept from OpenViking's directory-recursive "
                             "retrieval: score clusters coarse-to-fine, return a trajectory. Additive "
                             "-- default behavior (edge-BFS) is unchanged without this flag.")
    parser.add_argument("--top-clusters", type=int, default=3, dest="top_clusters",
                        help="[--cluster-drill] Max clusters to enter (default: 3)")
    parser.add_argument("--per-cluster", type=int, default=5, dest="per_cluster",
                        help="[--cluster-drill] Max matches per entered cluster (default: 5)")
    parser.add_argument("--cluster-min-sim", type=float, default=0.80, dest="cluster_min_sim",
                        help="[--cluster-drill] Min similarity edge to link two nodes into the same "
                             "cluster (default: 0.80, matches existing similar_to edge threshold)")
    # NEURON peer-scope (privacy-by-construction; separate peers_graph.json)
    parser.add_argument("--peer", dest="peer", default=None,
                        help="Peer-scoped mode. Value = active speaker as platform:id, platform_id, "
                             "or peer:platform:id. Returns ONLY that peer's edges from peers_graph.json.")
    parser.add_argument("--all-peers", action="store_true", dest="all_peers",
                        help="OWNER-ONLY cross-peer aggregation (e.g. 'who trades ETH'). Requires --owner.")
    parser.add_argument("--owner", action="store_true", dest="owner",
                        help="Assert the caller is the owner. REQUIRED for --all-peers.")
    parser.add_argument("--peers-graph", type=Path, default=PEERS_GRAPH_PATH, dest="peers_graph",
                        help=f"Path to peers_graph.json (default: {PEERS_GRAPH_PATH})")

    args = parser.parse_args()

    # ---- NEURON peer-scoped branch (isolated from the global-graph path) ----
    if args.peer or args.all_peers:
        pg = load_peers_graph(args.peers_graph)
        if args.all_peers:
            if not args.owner:
                print("Refused: --all-peers is OWNER-ONLY. Pass --owner to confirm caller is the owner.",
                      file=sys.stderr)
                sys.exit(2)
            result = all_peers_search(pg, args.query, args.min_confidence)
        else:
            subject = normalize_peer_subject(args.peer)
            result = peer_scoped_search(pg, subject, args.query, args.min_confidence)
        if args.output_format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_peer_text(result))
        return

    graph = load_graph(args.graph)
    nodes = graph.get("nodes", [])

    def _obs(uris, scores, fallback=False, extra=None):  # retrieval observability (fail-open)
        try:
            import sys as _s
            from pathlib import Path as _P
            _s.path.insert(0, str(_P(__file__).resolve().parent.parent / "procedures"))
            from _retrieval_log import log_retrieval
            log_retrieval(_P(__file__).resolve().parent.parent, "graph_search", args.query,
                          k=args.hops, result_uris=uris, top_scores=scores,
                          fallback_used=fallback, extra=extra)
        except Exception:
            pass

    if args.cluster_drill:
        result = cluster_drill_search(args.query, graph, min_sim=args.cluster_min_sim,
                                       top_clusters=args.top_clusters, per_cluster=args.per_cluster)
        if args.output_format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(format_cluster_drill_text(result))
        try:
            _cl = result.get("clusters", []) if isinstance(result, dict) else []
            _uris = [m.get("title") or m.get("idx") for c in _cl for m in c.get("matches", [])]
            _scores = [c.get("score") for c in _cl]
            _obs(_uris, _scores, extra={"mode": "cluster_drill"})
        except Exception:
            pass
        return

    start_idx, start_node = find_start_node(graph, args.query)
    if start_idx is None:
        print(f"No node found matching '{args.query}'. Try a different term.")
        _obs([], [], fallback=True, extra={"mode": "edge_bfs", "no_start_node": True})
        sys.exit(0)

    _scope_allow = set(s.strip() for s in args.scope.split(",") if s.strip()) if args.scope else None
    # Indexed BFS when the graph is store-backed (_GraphView exposes ._s): expand
    # each node via store.neighbors_filtered() (indexed SQL WHERE) so we only touch
    # the start node's N-hop neighborhood, never the full edge list. Falls back to
    # the classic build_adjacency + bfs_traverse for raw-JSON graphs. Both paths
    # are byte-identical in output (verified against the claim-protocol anchor).
    _store = getattr(graph, "_s", None)
    if _store is not None and hasattr(_store, "neighbors_filtered"):
        results = indexed_bfs_traverse(start_idx, _store, nodes, args.hops,
                                       args.min_confidence, scope_allow=_scope_allow,
                                       as_of=args.as_of)
    else:
        adj = build_adjacency(graph, args.min_confidence, scope_allow=_scope_allow, as_of=args.as_of)
        results = bfs_traverse(start_idx, adj, nodes, args.hops)

    visited_idxs = {start_idx} | {r["idx"] for r in results}
    forward_refs = collect_forward_refs(graph, visited_idxs, nodes)

    if args.output_format == "json":
        print(format_json(args.query, start_idx, start_node, results, forward_refs, args.hops, args.min_confidence))
    else:
        print(format_text(args.query, start_idx, start_node, results, forward_refs, args.hops, args.min_confidence))
    try:
        _obs([r.get("title") or r.get("idx") for r in results],
             [r.get("confidence") for r in results], extra={"mode": "edge_bfs"})
    except Exception:
        pass


if __name__ == "__main__":
    main()
