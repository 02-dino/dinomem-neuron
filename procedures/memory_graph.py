#!/usr/bin/env python3
"""
memory_graph.py — Layer 2 "neuron" linker for agent memory.

Reads the EXISTING embeddings in DINOMEM_AGENT_ID_PLACEHOLDER.sqlite (no embedding-provider calls)
and builds an association graph: every memory chunk linked to its closest
neighbors by cosine similarity. Non-destructive — never touches memory/*.md.

Outputs:
  memory/_graph/memory_graph.json   machine-readable graph (nodes + edges)
  memory/_graph/memory_graph.md     human-readable "what connects to what"

Usage:
  python3 memory_graph.py                 # build with defaults
  python3 memory_graph.py --topk 5 --threshold 0.45
  python3 memory_graph.py --min-cross-file   # only keep edges across different days

Design notes:
  - Association layer only. Does NOT synthesize new insights (that's layer 3).
  - Read-only on the index; safe to run anytime / on a cron.
  - Edges are symmetric; we keep the max similarity per pair.
"""

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB_PATH = Path("DINOMEM_SQLITE_PLACEHOLDER")
# Output lives in kb/memory_neuron/ — a deliberate home that documents this is
# the "neuron" association layer OVER the memory corpus.
# IMPORTANT: it must NOT sit under memory/ (the memory indexer scans memory/
# recursively) nor under kb/docs/ (the kb ingester scans kb/docs/**/*.md). A
# top-level kb/ subfolder is scanned by neither, so the graph never feeds back
# into any index (avoids the derived-data feedback loop).
# Workspace resolution (priority): DINOMEM_WORKSPACE env var > install-time sed
# substitution > self-locate from this file's location (safety net if sed was
# skipped/failed). DB_PATH (sqlite) stays on sed — it is not self-locatable.
import os as _os
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
_WS = Path(_os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
OUT_DIR = _WS / "kb/memory_neuron/l2_graph"


def compute_signature(db_path):
    """Content signature of the indexed memory corpus: sorted (path, hash) from
    the files table. Changes only when memory content actually changes — immune
    to WAL/checkpoint mtime churn."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute("SELECT path, hash FROM files ORDER BY path").fetchall()
    con.close()
    blob = "\n".join(f"{p}:{h}" for p, h in rows)
    import hashlib

    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


FIREWALL_MARKER = "[[L4_PROMOTED]]"  # chunks bearing this are L3-synthesized insights
FIREWALL_FILES = {"auto_promoted.md", "MEMORY.md"}  # path basenames the graph must skip
# - auto_promoted.md: L4 promotion output (don't re-synthesize own conclusions).
# - MEMORY.md: a DERIVED summary/index of memory/*.md (+ the auto memory-map). It
#   carries no new signal the source files don't already provide, so indexing it
#   only double-counts and inflates connection strength. Skip it.
# These hold L3-synthesized insights auto-promoted back into the memory pool.
# OpenClaw still indexes them (so they change behavior via recall/injection), but
# the neuron graph MUST skip them — otherwise synthesis reads its own conclusions
# back and confidence inflates without new evidence (echo chamber). This is the
# Stage-5 feedback-loop firewall. Filename match is primary (robust to chunk
# boundaries); the inline marker is a belt-and-suspenders fallback.


def load_chunks(db_path):
    """Return list of dicts: id, path, text, vec (list[float]).

    Skips L4-promoted chunks (firewall): see FIREWALL_MARKER.
    """
    if not db_path.exists():
        sys.exit(f"index not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT id, path, start_line, end_line, text, embedding FROM chunks"
    ).fetchall()
    con.close()
    chunks = []
    firewalled = 0
    for cid, path, s, e, text, emb in rows:
        if Path(path).name in FIREWALL_FILES or (text and FIREWALL_MARKER in text):
            firewalled += 1
            continue  # feedback-loop firewall: never re-ingest promoted insights
        try:
            vec = json.loads(emb)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(vec, list) or not vec:
            continue
        chunks.append(
            {
                "id": cid,
                "path": path,
                "start": s,
                "end": e,
                "text": text,
                "vec": vec,
            }
        )
    if firewalled:
        print(f"  🔥 firewall: skipped {firewalled} L4-promoted chunk(s) (no feedback loop)")
    return chunks


def normalize(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine_unit(a, b):
    """a, b already unit-normalized → dot product."""
    return sum(x * y for x, y in zip(a, b))


def short_path(p):
    return Path(p).name


def snippet(text, n=90):
    t = " ".join(text.split())
    return t[:n] + ("…" if len(t) > n else "")


FORWARD_REFS_FILE = OUT_DIR / "forward_refs.md"


def find_forward_refs(chunks, nodes):
    """Extract forward references: concepts mentioned in nodes that have no
    corresponding memory node yet.

    Returns list of {term, mentioned_in: [node_idx, ...], confidence: float}
    sorted by mention count desc, capped at 50.
    """
    import re
    from difflib import SequenceMatcher

    # Build a lookup of existing node titles + snippets for fuzzy matching
    existing_texts = []
    for n in nodes:
        existing_texts.append((n["title"] + " " + n["snippet"]).lower())

    # Patterns to extract candidate terms from snippet text
    # 1. Capitalized multi-word phrases (2-5 words, at least one capitalized)
    cap_phrase = re.compile(
        r'\b([A-Z][a-zA-Z]+(?:\s+[A-Za-z][a-zA-Z]+){1,4})\b'
    )
    # 2. Quoted terms (single or double quotes)
    quoted = re.compile(r'["\u2018\u2019\u201c\u201d]([A-Za-z][^"\u2018\u2019\u201c\u201d]{3,40})["\u2018\u2019\u201c\u201d]')
    # 3. Terms after trigger words
    trigger = re.compile(
        r'(?:about|regarding|related to|see also|covers|describes|mentions)\s+([A-Z][a-zA-Z]+(?:\s+[A-Za-z][a-zA-Z]+){1,4})'
    )

    # Collect candidate terms per node
    term_mentions = defaultdict(set)  # term -> set of node_idx

    for node in nodes:
        text = node["snippet"]
        if len(text) < 20:
            continue
        idx = node["idx"]
        candidates = set()
        for m in cap_phrase.finditer(text):
            phrase = m.group(1).strip()
            words = phrase.split()
            if 2 <= len(words) <= 5:
                candidates.add(phrase)
        for m in quoted.finditer(text):
            phrase = m.group(1).strip()
            words = phrase.split()
            if 2 <= len(words) <= 5:
                candidates.add(phrase)
        for m in trigger.finditer(text):
            phrase = m.group(1).strip()
            words = phrase.split()
            if 2 <= len(words) <= 5:
                candidates.add(phrase)
        for phrase in candidates:
            term_mentions[phrase].add(idx)

    # Filter: keep only terms that do NOT fuzzy-match any existing node
    forward_refs = []
    seen_terms = set()
    for term, mentioned_in in term_mentions.items():
        term_lower = term.lower()
        if term_lower in seen_terms:
            continue
        # Check against all existing node texts
        best_match = 0.0
        for et in existing_texts:
            ratio = SequenceMatcher(None, term_lower, et[:len(term_lower) + 20]).ratio()
            if ratio > best_match:
                best_match = ratio
        if best_match < 0.75:
            # It's a gap — no existing node covers this term
            confidence = round(1.0 - best_match, 4)
            forward_refs.append({
                "term": term,
                "mentioned_in": sorted(mentioned_in),
                "confidence": confidence,
            })
            seen_terms.add(term_lower)

    # Sort by mention count desc, then confidence desc; cap at 50
    forward_refs.sort(key=lambda x: (-len(x["mentioned_in"]), -x["confidence"]))
    return forward_refs[:50]


def write_forward_refs_md(forward_refs, out_path, now):
    """Write the forward_refs.md knowledge-gap report."""
    date_str = now[:10]
    if not forward_refs:
        out_path.write_text(
            "# Forward References\n_No gaps found._\n",
            encoding="utf-8",
        )
        return
    lines = [
        "# Forward References — Knowledge Gaps",
        f"_Generated: {date_str}_",
        "",
        "These concepts are mentioned in memory but have no dedicated node yet.",
        "Consider writing about them.",
        "",
        f"## Gaps ({len(forward_refs)} total)",
        "",
    ]
    for ref in forward_refs:
        n = len(ref["mentioned_in"])
        node_word = "node" if n == 1 else "nodes"
        lines.append(f"- **{ref['term']}** — mentioned in {n} {node_word}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract_title(text, maxlen=48):
    """Extract a clean human-readable title from a memory chunk."""
    import re
    t = " ".join(text.split())
    # Strip leading markers: - [valid], - [pattern], ## Decisions, # 2026-06-01, etc.
    t = re.sub(r'^[-#\s]*\[(valid|pattern|factual|preference|uncertain|invalidated|stale)[^\]]*\]\s*', '', t)
    t = re.sub(r'^#+\s*', '', t)  # markdown headings
    t = re.sub(r'^[-*\s]+', '', t)  # list markers
    t = t.strip()
    # If still looks like a date header, use filename-style label
    if re.match(r'^20\d\d-\d\d-\d\d', t):
        return t[:10]  # just the date
    return t[:maxlen] if t else snippet(text, maxlen)


def build_graph(chunks, topk, threshold, cross_file_only, mutual=False):
    units = [normalize(c["vec"]) for c in chunks]
    n = len(chunks)
    # Pass 1: per-node top-k neighbor sets + ranks (1=closest) + symmetric sim cache.
    topk_sets = []
    rank_of = []     # rank_of[i][j] = 1-based rank of j among i's top-k neighbors
    sim_lookup = {}  # (i,j) i<j -> sim
    for i in range(n):
        sims = []
        for j in range(n):
            if i == j:
                continue
            if cross_file_only and chunks[i]["path"] == chunks[j]["path"]:
                continue
            s = cosine_unit(units[i], units[j])
            if s >= threshold:
                sims.append((j, s))
                a, b = (i, j) if i < j else (j, i)
                prev = sim_lookup.get((a, b))
                if prev is None or s > prev:
                    sim_lookup[(a, b)] = s
        sims.sort(key=lambda x: x[1], reverse=True)
        top = sims[:topk]
        topk_sets.append({j for j, _ in top})
        rank_of.append({j: r for r, (j, _) in enumerate(top, start=1)})
    # Pass 2: assemble edges. Default = union (j in topk(i) OR i in topk(j)).
    # mutual=True keeps an edge only when BOTH nodes rank each other in top-k —
    # kills hub-explosion / one-sided spurious links.
    # rank_strength = mean of reciprocal ranks (1.0 only when both rank #1) —
    # a reciprocity-weighted connection strength, distinct from raw cosine sim.
    edges = {}
    for i in range(n):
        for j in topk_sets[i]:
            if mutual and i not in topk_sets[j]:
                continue
            a, b = (i, j) if i < j else (j, i)
            if (a, b) in edges:
                continue
            inv = [1.0 / r for r in (rank_of[i].get(j), rank_of[j].get(i)) if r]
            rank_strength = sum(inv) / len(inv) if inv else 0.0
            edges[(a, b)] = {"sim": sim_lookup[(a, b)], "rank_strength": round(rank_strength, 4)}
    return edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=4, help="max neighbors per node")
    ap.add_argument("--threshold", type=float, default=0.40, help="min cosine sim")
    ap.add_argument(
        "--min-cross-file",
        action="store_true",
        help="only link chunks from different memory files",
    )
    ap.add_argument(
        "--mutual",
        action="store_true",
        help="mutual-kNN: keep an edge only if BOTH nodes rank each other in top-k (cleaner graph)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if memory content is unchanged",
    )
    args = ap.parse_args()

    # Skip rebuild when the indexed memory content hasn't changed (avoids
    # wasteful runs every 15-min orchestrator cycle).
    sig = compute_signature(DB_PATH)
    sig_file = OUT_DIR / ".sig"
    out_json = OUT_DIR / "memory_graph.json"
    if not args.force and out_json.exists() and sig_file.exists():
        if sig_file.read_text(encoding="utf-8").strip() == sig:
            print("⏭️  memory unchanged — graph is current, skipping rebuild")
            return

    chunks = load_chunks(DB_PATH)
    if not chunks:
        sys.exit("no usable chunks with embeddings found")

    edges = build_graph(chunks, args.topk, args.threshold, args.min_cross_file, args.mutual)

    # degree = how "connected" each memory is (its neuron strength)
    degree = defaultdict(int)
    for (a, b) in edges:
        degree[a] += 1
        degree[b] += 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Edge-history persistence → recurrence (Hebbian: a connection that recurs
    # across rebuilds is stronger than a one-off coincidence). Keyed by STABLE
    # chunk-id pair (not index, which shifts between rebuilds). Only real
    # rebuilds reach here — the change-gate skips unchanged memory — so the count
    # = how many distinct memory states this association survived.
    hist_path = OUT_DIR / "edge_history.json"
    history = {}
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            history = {}
    recurrence = {}
    l3_conf_path = _WS / "kb/memory_neuron/l3_synthesis/l3_edge_confidence.json"
    l3_conf = {}
    if l3_conf_path.exists():
        try:
            l3_conf = json.loads(l3_conf_path.read_text(encoding="utf-8"))
        except Exception:
            l3_conf = {}
    for (a, b) in edges:
        key = "||".join(sorted((str(chunks[a]["id"]), str(chunks[b]["id"]))))
        h = history.get(key)
        if h:
            h["count"] += 1
            h["last_seen"] = now
            h["last_sim"] = round(edges[(a, b)]["sim"], 4)
            # Always update l3_confidence from the latest L3 synthesis file
            h["l3_confidence"] = l3_conf.get(key, h.get("l3_confidence", 0.0))
        else:
            h = {"count": 1, "first_seen": now, "last_seen": now,
                 "last_sim": round(edges[(a, b)]["sim"], 4),
                 "l3_confidence": l3_conf.get(key, 0.0)}
            history[key] = h
        recurrence[(a, b)] = (h["count"], h["first_seen"], h.get("l3_confidence", 0.0))
    hist_path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")

    # JSON graph
    nodes = [
        {
            "idx": i,
            "id": c["id"],
            "file": short_path(c["path"]),
            "lines": [c["start"], c["end"]],
            "degree": degree.get(i, 0),
            "title": extract_title(c["text"]),
            "snippet": snippet(c["text"], 140),
        }
        for i, c in enumerate(chunks)
    ]
    edge_list = []
    for (a, b), v in sorted(edges.items(), key=lambda kv: kv[1]["sim"], reverse=True):
        rec, first_seen, l3_conf = recurrence[(a, b)]
        rec_weight = 1.0 + 0.15 * (rec - 1)
        l3_boost = 1.0 + l3_conf
        neural_score = round(v["sim"] * v["rank_strength"] * rec_weight * l3_boost, 4)
        edge_list.append({
            "a": a,
            "b": b,
            "sim": round(v["sim"], 4),
            "rank_strength": v["rank_strength"],
            "recurrence": rec,
            "first_seen": first_seen,
            "cross_file": chunks[a]["path"] != chunks[b]["path"],
            "neural_score": neural_score,
            "l3_confidence": l3_conf,
        })
    # Forward reference tracking — surface knowledge gaps
    forward_refs = find_forward_refs(chunks, nodes)
    write_forward_refs_md(forward_refs, FORWARD_REFS_FILE, now)

    (OUT_DIR / "memory_graph.json").write_text(
        json.dumps(
            {
                "generated": now,
                "params": vars(args),
                "node_count": len(nodes),
                "edge_count": len(edge_list),
                "nodes": nodes,
                "edges": edge_list,
                "forward_refs": forward_refs,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Human-readable
    lines = [
        f"# Memory Association Graph",
        f"_generated {now} · {len(nodes)} nodes · {len(edge_list)} edges · "
        f"topk={args.topk} threshold={args.threshold} cross_file_only={args.min_cross_file} mutual={args.mutual}_",
        "",
        "## Most connected memories (neuron hubs)",
    ]
    hubs = sorted(nodes, key=lambda n: n["degree"], reverse=True)[:8]
    for n in hubs:
        if n["degree"] == 0:
            continue
        lines.append(f"- **{n['degree']} links** · {n['file']} — {n['snippet']}")
    lines += ["", "## Strongest connections (cross-file = potential new pattern)"]
    for e in edge_list[:25]:
        a, b = chunks[e["a"]], chunks[e["b"]]
        tag = "🔗CROSS" if e["cross_file"] else "·same"
        lines.append(
            f"- sim {e['sim']:.2f} · str {e['rank_strength']:.2f} · ×{e['recurrence']} {tag} | "
            f"{short_path(a['path'])}: {snippet(a['text'],60)}  ⟷  "
            f"{short_path(b['path'])}: {snippet(b['text'],60)}"
        )
    (OUT_DIR / "memory_graph.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / ".sig").write_text(sig, encoding="utf-8")

    cross = sum(1 for e in edge_list if e["cross_file"])
    print(f"✅ graph built: {len(nodes)} nodes, {len(edge_list)} edges ({cross} cross-file)")
    print(f"   {OUT_DIR/'memory_graph.json'}")
    print(f"   {OUT_DIR/'memory_graph.md'}")
    if forward_refs:
        print(f"   {len(forward_refs)} forward reference(s) found → {FORWARD_REFS_FILE}")




if __name__ == "__main__":
    # ---- dinomem-neuron license guard ----
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from license_check import enforce_license as _enforce_license
        _enforce_license(_os.path.basename(__file__))
    except SystemExit:
        raise
    except Exception:
        pass
    try:
        from run_log import RunLog as _RunLog
        with _RunLog("memory_graph") as _log:
            main()
            _log.artifact("graph_json", str(OUT_DIR / "memory_graph.json"))
            # Log forward refs count and path (forward_refs is local to main(),
            # so we read it back from the JSON output)
            try:
                import json as _json
                _gj = _json.loads((OUT_DIR / "memory_graph.json").read_text(encoding="utf-8"))
                _fref_count = len(_gj.get("forward_refs", []))
                _log.artifact("forward_refs_count", str(_fref_count))
                _log.artifact("forward_refs_md", str(FORWARD_REFS_FILE))
            except Exception:
                pass
    except ImportError:
        main()
