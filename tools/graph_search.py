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
import argparse
import difflib
from pathlib import Path
from collections import defaultdict, deque

# Resolve graph path relative to repo root (parent of tools/)
_SCRIPT_DIR = Path(__file__).parent
_REPO_ROOT = _SCRIPT_DIR.parent
GRAPH_PATH = _REPO_ROOT / "kb" / "memory_neuron" / "l2_graph" / "memory_graph.json"


def load_graph(graph_path: Path) -> dict:
    if not graph_path.exists():
        print(f"Error: Graph file not found at {graph_path}", file=sys.stderr)
        print("Run memory_graph.py to build the graph first.", file=sys.stderr)
        sys.exit(1)
    with open(graph_path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_start_node(graph: dict, query: str, threshold: float = 0.60) -> tuple[int, dict] | tuple[None, None]:
    """Fuzzy match query against node title and snippet fields. Returns (idx, node) or (None, None)."""
    nodes = graph.get("nodes", [])
    query_lower = query.lower()
    best_score = 0.0
    best_idx = None
    best_node = None

    for i, node in enumerate(nodes):
        title = node.get("title", "")
        snippet = node.get("snippet", "")
        combined = f"{title} {snippet}".lower()

        # Direct substring match gets priority
        if query_lower in combined:
            score = 1.0 if query_lower in title.lower() else 0.85
        else:
            # difflib ratio against title (weighted higher) and snippet
            title_score = difflib.SequenceMatcher(None, query_lower, title.lower()).ratio()
            snippet_score = difflib.SequenceMatcher(None, query_lower, combined).ratio()
            score = max(title_score, snippet_score * 0.9)

        if score > best_score and score >= threshold:
            best_score = score
            best_idx = i
            best_node = node

    return best_idx, best_node


def build_adjacency(graph: dict, min_confidence: float) -> dict[int, list[dict]]:
    """Build adjacency list from relation_edges and similarity edges."""
    adj = defaultdict(list)
    has_relation_edges = "relation_edges" in graph

    if has_relation_edges:
        for edge in graph.get("relation_edges", []):
            src = edge.get("source")
            tgt = edge.get("target")
            conf = edge.get("confidence", 0.0)
            label = edge.get("relation", "related_to")
            if src is None or tgt is None:
                continue
            if conf >= min_confidence:
                adj[src].append({"target": tgt, "label": label, "confidence": conf, "edge_type": "relation"})
    else:
        print("Warning: relation_edges not yet available — run memory_graph.py to rebuild graph", file=sys.stderr)

    # Secondary: high-confidence similarity edges (sim >= 0.80)
    for edge in graph.get("edges", []):
        src = edge.get("source")
        tgt = edge.get("target")
        sim = edge.get("weight", edge.get("similarity", 0.0))
        if src is None or tgt is None:
            continue
        if sim >= 0.80:
            adj[src].append({"target": tgt, "label": "similar_to", "confidence": sim, "edge_type": "similarity"})

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


def main():
    parser = argparse.ArgumentParser(
        description="Traverse typed relation edges in memory_graph.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", help="Concept to start from (fuzzy matched against node titles/snippets)")
    parser.add_argument("--hops", type=int, default=2, help="Max traversal depth (default: 2)")
    parser.add_argument("--min-confidence", type=float, default=0.6, dest="min_confidence",
                        help="Minimum edge confidence to follow (default: 0.6)")
    parser.add_argument("--output-format", choices=["text", "json"], default="text", dest="output_format",
                        help="Output format: text (default) or json")
    parser.add_argument("--graph", type=Path, default=GRAPH_PATH,
                        help=f"Path to memory_graph.json (default: {GRAPH_PATH})")

    args = parser.parse_args()

    graph = load_graph(args.graph)
    nodes = graph.get("nodes", [])

    start_idx, start_node = find_start_node(graph, args.query)
    if start_idx is None:
        print(f"No node found matching '{args.query}'. Try a different term.")
        sys.exit(0)

    adj = build_adjacency(graph, args.min_confidence)
    results = bfs_traverse(start_idx, adj, nodes, args.hops)

    visited_idxs = {start_idx} | {r["idx"] for r in results}
    forward_refs = collect_forward_refs(graph, visited_idxs, nodes)

    if args.output_format == "json":
        print(format_json(args.query, start_idx, start_node, results, forward_refs, args.hops, args.min_confidence))
    else:
        print(format_text(args.query, start_idx, start_node, results, forward_refs, args.hops, args.min_confidence))


if __name__ == "__main__":
    main()
