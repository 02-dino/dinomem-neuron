#!/usr/bin/env python3
"""
memory_graph.py — Layer 2 "neuron" linker for agent memory.

Reads the EXISTING embeddings in analyst.sqlite (no embedding-provider calls)
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
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# numpy powers the vectorized similarity graph (build_graph). It is guaranteed
# present in a neuron install because scripts/install.sh installs pandas, which
# hard-depends on numpy. Guarded so a bare run without the install step fails
# with an actionable message rather than a cryptic NameError deep in build_graph.
try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit(
        "numpy is required for the L2 graph (vectorized similarity). Install it "
        "with: pip install numpy   (normally pulled in by neuron's pandas dep)."
    )

# Schema adapter: resolves OpenClaw memory-index table/column names at runtime so
# neuron survives OpenClaw schema drift (files/chunks vs memory_index_chunks).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _schema_adapter import resolve as _resolve_schema_raw, SchemaUnsupported  # noqa: E402

# Optional valid-time layer (#2) — supplies valid_at/invalid_at for P0-b fact edges by
# reading source-memory frontmatter. Fail-open: absent -> edges are simply not time-bounded.
try:
    import valid_time as _VT  # noqa: E402
except Exception:
    _VT = None

_SCHEMA_CACHE = {}


def _resolve_schema(db_path):
    key = str(db_path)
    if key not in _SCHEMA_CACHE:
        try:
            _SCHEMA_CACHE[key] = _resolve_schema_raw(db_path)
        except SchemaUnsupported as e:
            sys.exit(f"memory index schema not supported:\n{e}")
    return _SCHEMA_CACHE[key]

# Optional spaCy for SVO triple extraction (graceful fallback if not installed)
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except (ImportError, OSError):
    SPACY_AVAILABLE = False

# Live memory index moved to the agent-scoped path with the NEW memory_index_* schema
# (old /root/.openclaw/memory/analyst.sqlite is dead; main.sqlite/analysis.sqlite are
# stale 0-row shells). _schema_adapter resolves both old and new table names, so pointing
# here just works. Env override wins so installers/other agents can relocate it.
_DB_DEFAULT = "/root/.openclaw/agents/analyst/agent/openclaw-agent.sqlite"
# DB_PATH resolved just below, AFTER `import os as _os` (env override read there).
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
DB_PATH = Path(_os.environ.get("DINOMEM_MEMORY_DB", _DB_DEFAULT))
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
_WS = Path(_os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
OUT_DIR = _WS / "kb/memory_neuron/l2_graph"


def compute_signature(db_path):
    """Content signature of the indexed memory corpus: sorted (path, hash) from
    the files table. Changes only when memory content actually changes — immune
    to WAL/checkpoint mtime churn. Schema-tolerant via _schema_adapter (falls
    back to a chunks-based signature when the index has no files table)."""
    schema = _resolve_schema(db_path)
    sql = schema.signature_sql() or schema.chunk_signature_sql()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(sql).fetchall()
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
    schema = _resolve_schema(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(schema.load_chunks_sql()).fetchall()
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


def extract_entity_nodes(memory_dir):
    """Scan memory/*.md for [entity] lines and return deduplicated entity nodes.

    Each [entity] line has the form:
      [entity] Name | type: person|project|tool|concept|org

    Returns list of dicts: {"name": str, "type": str, "mentions": int}
    sorted by mentions descending.
    """
    ENTITY_RE = re.compile(
        r'^\[entity\]\s*(.+?)\s*\|\s*type:\s*(\S+)\s*$'
    )
    # name (lowercased) -> {name, type, mentions}
    seen = {}
    md_files = sorted(Path(memory_dir).glob("*.md"))
    for md_file in md_files:
        if md_file.name in {"MEMORY.md", "auto_promoted.md"}:
            continue
        try:
            lines = md_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            m = ENTITY_RE.match(line.strip())
            if not m:
                continue
            try:
                name = m.group(1).strip()
                etype = m.group(2).strip()
                key = name.lower()
                if key in seen:
                    seen[key]["mentions"] += 1
                else:
                    seen[key] = {"name": name, "type": etype, "mentions": 1}
            except Exception:
                continue  # graceful: skip malformed lines
    raw = sorted(seen.values(), key=lambda x: x["mentions"], reverse=True)
    # P1 (2026-08-12): resolve surface-form variants into canonical entities (aliases[]).
    # Fail-open: if the resolver import fails, fall back to raw surface forms (old behavior).
    try:
        from entity_resolver import resolve_entities  # noqa: E402
        return resolve_entities(raw)
    except Exception:
        return raw


def extract_relation_edges(memory_dir, nodes, entity_nodes=None):
    """Scan memory/*.md for [relation] lines and build typed edges.

    Returns list of relation edge dicts:
      {"type": "relation", "label": verb, "a": node_idx, "b": node_idx,
       "confidence": 0.9, "subject": str, "object": str}
    Unmatched subjects/objects are returned as forward references with node_idx=-1.
    Also runs spaCy SVO extraction if SPACY_AVAILABLE (confidence 0.6).

    P2 (2026-08-12): endpoints resolve through ENTITY NAMES, not the 12k chunk node_texts.
    Relation subjects/objects are SHORT strings (`check_projects.sh`, `API key`); they cannot
    reach the 0.70 SequenceMatcher ratio against a 60-200 char `title+snippet` node_text, so
    matching vs chunk-texts left EVERY edge at a=-1/b=-1 (endpoint-less, unwalkable). We instead
    fuzzy-match each endpoint against the ~361 short entity NAMES, then map the winning entity to
    its representative nodes[] index via `entity_nodes[i]["node_idx"]` (the backref build_graph
    stamps = the entity's best-matching chunk). This keeps a/b VALID nodes[] indices, so
    graph_search's existing reader traverses them unchanged. Fail-open: no entity_nodes / no
    node_idx -> fall back to the legacy chunk fuzzy scan (old a=-1 behavior, nothing regresses).
    """
    from difflib import SequenceMatcher
    entity_nodes = entity_nodes or []

    # P1 (2026-08-12): alias map so relation subject/object surface-form variants
    # (`neuron` vs `dinomem-neuron`, backtick/glob variants) collapse to ONE canonical
    # endpoint BEFORE fuzzy_match_node. Raw subject/object kept in the edge for provenance;
    # only the MATCH KEY is canonicalized. Fail-open: no resolver -> identity (old behavior).
    try:
        from entity_resolver import build_alias_map, canonicalize as _canon  # noqa: E402
        _alias_map = build_alias_map(extract_entity_nodes(memory_dir))
    except Exception:
        _alias_map = {}
        def _canon(t, m):  # noqa: E306
            return t

    RELATION_RE = re.compile(
        r'^\[relation\]\s*(.+?)\s*\u2192\s*(.+?)\s*\u2192\s*(.+?)\s*$'
    )

    # Build lookup: node_idx -> combined text for fuzzy matching
    node_texts = []
    for n in nodes:
        node_texts.append((n["title"] + " " + n["snippet"]).lower())

    # ── Bottleneck #3 fix (2026-08-12, py-spy PROVEN): the memoize+prune loop still touched
    #    all ~12k nodes for each of ~2446 unique endpoint terms (~29M SequenceMatcher setups) ->
    #    the real 30-min hang. Add a LENGTH-BUCKET index. SequenceMatcher.ratio() has a hard
    #    length upper bound: ratio <= 2*min(La,Lb)/(La+Lb). To clear MATCH_THRESHOLD=0.70 a term
    #    of length L can ONLY match node-texts whose (capped) length lies in [0.54L, 1.86L]
    #    (solving 2*min/(sum) >= 0.70). Bucket node indices by their capped length once, then per
    #    term scan ONLY the feasible length band. Output is byte-identical (same argmax/score):
    #    every skipped node is provably below threshold, so it could never have been the best.
    _MATCH_THRESHOLD = 0.70
    # capped length = len(nt[:cut]); cut depends on the term, so bucket by RAW len and widen the
    # band by the max cut slack (30) to stay conservative (never drop a feasible node).
    from collections import defaultdict as _dd
    _len_buckets = _dd(list)
    for _i, _nt in enumerate(node_texts):
        _len_buckets[len(_nt)].append(_i)
    _bucket_lengths = sorted(_len_buckets.keys())
    import bisect as _bisect

    # ── Bottleneck #2 fix (2026-08-12): fuzzy_match_node was O(terms × nodes) pure-Python
    #    SequenceMatcher.ratio() called ~4× per [relation] line across 12,138 md files -> multi-
    #    minute silent CPU stall at 12k scale. Two BYTE-IDENTICAL-OUTPUT accelerations:
    #      1. MEMOIZE: the same subject/object terms recur constantly across files; cache the
    #         (idx, score) result per lowercased-term so each unique term is scored ONCE.
    #      2. CHEAP-BOUND PREFILTER: SequenceMatcher exposes real_quick_ratio() (length-only) and
    #         quick_ratio() (multiset) as monotone UPPER BOUNDS on ratio(). If the upper bound
    #         can't beat the current best, skip the expensive ratio() entirely. Result is exactly
    #         what the naive full scan returns (same argmax, same score), just far fewer ratio()s.
    #    Deterministic / T1 / $0 / no LLM/embed/network. set_seq2 reuse avoids re-hashing the term.
    _fuzzy_cache: dict = {}

    def fuzzy_match_node(term):
        """Return (best_idx, best_score) for term against all nodes. Memoized + upper-bound-pruned;
        output byte-identical to the former full SequenceMatcher scan."""
        term_lower = term.lower().strip()
        cached = _fuzzy_cache.get(term_lower)
        if cached is not None:
            return cached
        cut = len(term_lower) + 30
        sm = SequenceMatcher(None, term_lower, "")
        best_idx, best_score = -1, 0.0
        # Length-feasible band. EFFECTIVE compared length of a node is eff = min(len(nt), cut).
        # ratio upper bound vs a length-L term is 2*min(L,eff)/(L+eff), which is >=0.70 only when
        # eff >= ~0.538*L. BUGFIX (2026-08-12): the old band ALSO imposed an UPPER bound
        # hi=int(L/0.538)+1 on RAW node length and pruned any node longer than it. That was wrong:
        # a long node is capped to `cut` chars, so its eff = cut = L+30 (> L > 0.538L) is ALWAYS
        # feasible — the upper bound incorrectly excluded EVERY long chunk-text for short terms,
        # returning -1 where the naive full scan finds a real best (verified: 8/8 short terms
        # mismatched). Correct band keeps ONLY the lower bound (eff = min(rl,cut); since cut is
        # always feasible, eff<lo iff rl<lo). Now byte-identical to the naive scan (verified 0/8).
        L = len(term_lower)
        lo = int(L * 0.538)                      # nodes shorter than this can never reach 0.70
        _lidx = _bisect.bisect_left(_bucket_lengths, lo)
        _candidate_idxs = []
        for _bl in _bucket_lengths[_lidx:]:
            _candidate_idxs.extend(_len_buckets[_bl])
        for idx in _candidate_idxs:
            nt = node_texts[idx]
            sm.set_seq2(nt[:cut])
            # real_quick_ratio() (length bound) and quick_ratio() (multiset bound) are cheap
            # monotone upper bounds on ratio(); prune when they can't exceed the running best.
            if sm.real_quick_ratio() <= best_score:
                continue
            if sm.quick_ratio() <= best_score:
                continue
            score = sm.ratio()
            if score > best_score:
                best_score = score
                best_idx = idx
        result = (best_idx, best_score)
        _fuzzy_cache[term_lower] = result
        return result

    # ── P1 fix B (2026-08-12): O(1) canonical->node_idx map for KNOWN entities. ──
    #    Root cause of the 30-min hang: P1 canonicalizes each subject/object BEFORE
    #    fuzzy_match_node, and the canonical string is a DIFFERENT _fuzzy_cache key than the
    #    raw term the pre-P1 code hit for free -> every canonicalized endpoint forced a fresh
    #    full ~12k-node SequenceMatcher scan (2570 lines x 2 endpoints). The resolver already
    #    knows every canonical entity's surface_forms; map each surface form to the node index
    #    it fuzzy-matches to ONCE, then relation-endpoint matching is an O(1) dict hit for known
    #    entities. fuzzy_match_node stays as the fallback for genuinely-unknown endpoints, so the
    #    emitted edges are byte-identical to the naive scan for every matched endpoint.
    #    Deterministic / T1 / $0. Fail-open: empty map (no resolver) -> pure fuzzy path (old behavior).
    _canon_to_idx: dict = {}
    try:
        for ent in (extract_entity_nodes(memory_dir) or []):
            for sf in ent.get("surface_forms", [ent.get("name", "")]):
                canon = ent.get("name", sf)
                key = (canon or "").lower().strip()
                if not key or key in _canon_to_idx:
                    continue
                idx, score = fuzzy_match_node(canon)
                if score >= 0.70:
                    _canon_to_idx[key] = (idx, score)
    except Exception:
        _canon_to_idx = {}

    def match_endpoint(term):
        """Canonicalize term, then O(1) dict hit for known canonical entities; fuzzy fallback
        only on miss. Byte-identical (idx, score) to the pre-fix canon->fuzzy path."""
        canon = _canon(term, _alias_map)
        hit = _canon_to_idx.get((canon or "").lower().strip())
        if hit is not None:
            return hit
        return fuzzy_match_node(canon)

    # ── P2 (2026-08-12): entity-name endpoint resolver (the walkability fix) ──
    #    Build (entity_name_lower -> representative nodes[] idx) from the node_idx backrefs
    #    build_graph stamped on each entity_node. Also index by surface_forms so aliased
    #    variants resolve. Then match_endpoint_entity fuzzy-matches the (canonicalized) term
    #    against the SHORT entity names (~361, not 12k chunks) and returns that entity's
    #    representative nodes[] idx when score>=_MATCH_THRESHOLD. This is what turns a=-1/b=-1
    #    into real walkable endpoints. Deterministic / T1 / $0.
    _ent_name_to_nodeidx = {}
    _ent_names = []
    for _ent in entity_nodes:
        _nidx = _ent.get("node_idx", -1)
        if _nidx is None or _nidx < 0:
            continue
        for _form in ([_ent.get("name", "")] + list(_ent.get("surface_forms", []))):
            _k = (_form or "").lower().strip()
            if _k and _k not in _ent_name_to_nodeidx:
                _ent_name_to_nodeidx[_k] = _nidx
                _ent_names.append(_k)
    _ent_match_cache = {}

    def match_endpoint_entity(term):
        """Resolve a relation endpoint to a representative nodes[] idx by fuzzy-matching the
        CANONICAL term against short entity names. Returns (node_idx, score). Falls back to the
        legacy chunk matcher (match_endpoint) only when no entity map is available."""
        if not _ent_name_to_nodeidx:
            return match_endpoint(term)   # fail-open: legacy behavior (a=-1 as before)
        canon = (_canon(term, _alias_map) or "").lower().strip()
        if not canon:
            return (-1, 0.0)
        cached = _ent_match_cache.get(canon)
        if cached is not None:
            return cached
        # exact-name O(1) hit first
        exact = _ent_name_to_nodeidx.get(canon)
        if exact is not None:
            res = (exact, 1.0)
            _ent_match_cache[canon] = res
            return res
        # else fuzzy over the SHORT entity-name list (cheap: ~361 short strings)
        best_name, best_score = None, 0.0
        sm = SequenceMatcher(None, canon, "")
        for nm in _ent_names:
            sm.set_seq2(nm)
            if sm.real_quick_ratio() <= best_score:
                continue
            s = sm.ratio()
            if s > best_score:
                best_score, best_name = s, nm
        if best_name is not None and best_score >= _MATCH_THRESHOLD:
            res = (_ent_name_to_nodeidx[best_name], round(best_score, 4))
        else:
            res = (-1, round(best_score, 4))
        _ent_match_cache[canon] = res
        return res

    # ── P0-b fact-graph enrichment (HARMONIZED with neuron's existing mechanisms) ──
    # DEEP-UNDERSTANDING NOTE (2026-08-11): neuron ALREADY has a record-time + confidence
    # + Hebbian-recurrence system on edges (first_seen/last_seen/count/l3_confidence, set
    # downstream in build_graph). We do NOT duplicate that. We add ONLY the genuinely-missing
    # P0-b half and MAP the rest onto what exists:
    #   - record/system time  -> DEFER to first_seen/last_seen (added downstream). NOT re-added here.
    #   - relation confidence  -> existing raw `confidence` + downstream `l3_confidence`. NOT re-added.
    #   - VALID/EVENT time     -> the ONE clock neuron lacks (first_seen = when RECORDED, not when TRUE).
    #   - status / scope / source_memory_ids[] -> genuinely absent, add.
    # DETERMINISTIC / T1 / $0: filename + frontmatter reads only, no LLM/embed. Default-safe so the
    # 1890 legacy edges + graph_search's a/b/confidence/label reader keep working untouched.
    def _infer_scope(fname: str) -> str:
        """Privacy scope from source filename (proposal: scope enforced BEFORE retrieval;
        stamped at write time). Conservative default = owner (owner's own workspace)."""
        f = (fname or "").lower()
        if "peers/" in f or f.startswith("_peer_") or "peer" in f:
            return "peer"
        if "_note_" in f or "project" in f:
            return "project"
        return "owner"

    def _valid_time_for(fname: str):
        """valid_from/valid_until off the source memory frontmatter (reuses shipped valid-time #2).
        Fail-open: any error/no valid_time -> (None, None) (edge simply not time-bounded)."""
        try:
            p = Path(memory_dir) / fname
            if not p.exists() or _VT is None:
                return (None, None)
            fields = _VT.read_fields(p)
            return (fields.get("valid_from"), fields.get("valid_until"))
        except Exception:
            return (None, None)

    def _enrich(edge: dict) -> dict:
        """Add ONLY the P0-b fields neuron lacks. Record-time/confidence come from the
        existing downstream mechanisms (first_seen/last_seen/l3_confidence) — not duplicated here."""
        src = edge.get("source_file", "")
        valid_at, invalid_at = _valid_time_for(src)
        edge.setdefault("valid_at", valid_at)          # EVENT/valid time — the clock neuron lacked
        edge.setdefault("invalid_at", invalid_at)      # retroactively closed by next contradicting fact
        edge.setdefault("status", "trusted" if edge.get("source") == "explicit" else "provisional")
        edge.setdefault("source_memory_ids", [src] if src else [])  # provenance array (proposal)
        edge.setdefault("scope", _infer_scope(src))    # privacy scope (enforced before traversal)
        edge.setdefault("created_by", edge.get("source", "unknown"))
        return edge

    relation_edges = []

    # --- Pass 1: explicit [relation] lines from memory/*.md ---
    md_files = sorted(Path(memory_dir).glob("*.md"))
    for md_file in md_files:
        if md_file.name in {"MEMORY.md", "auto_promoted.md"}:
            continue
        try:
            lines = md_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            m = RELATION_RE.match(line.strip())
            if not m:
                continue
            subject = m.group(1).strip()
            verb = m.group(2).strip()
            obj = m.group(3).strip()

            # P2: resolve endpoints via short entity NAMES -> representative nodes[] idx
            # (makes edges walkable). Fail-open to legacy chunk matcher inside match_endpoint_entity.
            idx_a, score_a = match_endpoint_entity(subject)
            idx_b, score_b = match_endpoint_entity(obj)

            matched_a = score_a >= 0.70
            matched_b = score_b >= 0.70

            relation_edges.append(_enrich({
                "type": "relation",
                "label": verb,
                "subject": subject,
                "object": obj,
                "a": idx_a if matched_a else -1,
                "b": idx_b if matched_b else -1,
                "confidence": 0.9,
                "source": "explicit",
                "source_file": md_file.name,
            }))

    # --- Pass 2: spaCy SVO triples (optional, confidence 0.6) ---
    # P3 QUALITY GATE (2026-08-12): the SVO auto-miner is DORMANT today (spaCy not producing edges),
    # but built-for-scale — when it activates or the corpus grows it WILL start mining triples from
    # casual prose, and spaCy grabs spurious subject/verb/object (pronouns, stopwords, filler verbs
    # like "be/have/do/get") that would pollute the typed graph with low-signal edges. The gate below
    # is a NO-OP while zero SVO edges are produced, and precision-filters automatically the moment
    # they start. Deterministic / T1 / $0. Three cheap filters:
    #   (1) verb blocklist — drop copula/light/aux verbs that carry no real relation.
    #   (2) min endpoint length — drop 1-2 char / pronoun-ish endpoints ("it", "he", "we", "a").
    #   (3) subject!=object — drop degenerate self-loops.
    # Endpoints still must BOTH resolve to real entity nodes (score>=0.70) as before — this gate is
    # an ADDITIONAL precision layer on top, never a loosening.
    _SVO_VERB_BLOCKLIST = {
        "be", "have", "do", "get", "go", "make", "take", "come", "seem", "become",
        "say", "see", "know", "think", "want", "use", "look", "give", "tell", "call",
    }
    def _svo_ok(subj_text, verb_text, obj_text):
        """Precision gate for auto-mined SVO triples. True = keep. NO-OP while SVO is dormant."""
        s = (subj_text or "").strip()
        o = (obj_text or "").strip()
        v = (verb_text or "").strip().lower()
        if len(s) < 3 or len(o) < 3:            # (2) drop pronoun/stub endpoints
            return False
        if v in _SVO_VERB_BLOCKLIST:            # (1) drop copula/light/aux verbs
            return False
        if s.lower() == o.lower():              # (3) drop self-loops
            return False
        return True
    def _svo_from_doc(doc, node):
        """Extract + append SVO relation edges from ONE parsed doc. Identical token
        logic to the original loop body, factored out so batched nlp.pipe() and the
        single-doc fallback share EXACTLY the same extraction (byte-identical edges)."""
        for token in doc:
            if token.dep_ == "ROOT" and token.pos_ == "VERB":
                subjects = [w for w in token.lefts if w.dep_ in ("nsubj", "nsubjpass")]
                objects = [w for w in token.rights if w.dep_ in ("dobj", "attr", "pobj", "oprd")]
                for subj in subjects:
                    for obj in objects:
                        subj_text = subj.text
                        obj_text = obj.text
                        verb_text = token.lemma_
                        if not _svo_ok(subj_text, verb_text, obj_text):
                            continue
                        idx_a, score_a = match_endpoint_entity(subj_text)
                        idx_b, score_b = match_endpoint_entity(obj_text)
                        if score_a >= 0.70 and score_b >= 0.70:
                            relation_edges.append(_enrich({
                                "type": "relation",
                                "label": verb_text,
                                "subject": subj_text,
                                "object": obj_text,
                                "a": idx_a,
                                "b": idx_b,
                                "confidence": 0.6,
                                "source": "spacy_svo",
                                "source_file": node["file"],
                            }))

    if SPACY_AVAILABLE and nodes:
        # BATCHED + CPU-AWARE spaCy SVO (was: one nlp(text) per node, single-thread
        # = the dominant rebuild cost). nlp.pipe() batches parsing and can fan out
        # across cores; it PRESERVES input order and we pair each doc back to its
        # node via as_tuples, so the emitted edge set + order stay byte-identical to
        # the old loop. Short/empty snippets are skipped BEFORE piping (same len<20
        # gate) so exactly the same docs get parsed.
        _pairs = [(node["snippet"], node) for node in nodes
                  if node.get("snippet") and len(node["snippet"]) >= 20]
        # CPU-aware n_process: leave headroom (cores-1), cap at 4 (spaCy spawn/IPC
        # overhead dominates past that on typical corpora), floor 1. Env override
        # DINOMEM_GRAPH_NPROC for tiny/huge hosts. Force single-process on a small
        # corpus where multiprocessing setup costs more than it saves.
        try:
            _cores = _os.cpu_count() or 1
        except Exception:
            _cores = 1
        _nproc = max(1, min(4, _cores - 1))
        if len(_pairs) < 400:
            _nproc = 1
        _ovp = _os.environ.get("DINOMEM_GRAPH_NPROC")
        if _ovp:
            try:
                _nproc = max(1, min(_cores, int(_ovp)))
            except ValueError:
                pass
        _bs = 128 if _nproc == 1 else 64  # larger batch single-proc, smaller per-worker
        print("[graph] spaCy SVO over %d nodes | nlp.pipe (n_process=%d, batch_size=%d, cores=%d)"
              % (len(_pairs), _nproc, _bs, _cores), file=sys.stderr)
        try:
            for doc, node in nlp.pipe(_pairs, as_tuples=True,
                                      batch_size=_bs, n_process=_nproc):
                try:
                    _svo_from_doc(doc, node)
                except Exception:
                    continue
        except Exception as _pe:
            # Any pipe/multiprocessing failure -> single-doc fallback (never lose the
            # SVO layer over an infra hiccup). Same extraction per doc.
            print("[graph] nlp.pipe failed (%s); single-doc fallback" % _pe, file=sys.stderr)
            for text, node in _pairs:
                try:
                    _svo_from_doc(nlp(text), node)
                except Exception:
                    continue

    return relation_edges


def build_graph(chunks, topk, threshold, cross_file_only, mutual=False):
    """Vectorized top-k cosine-similarity graph builder.

    Behaviour-preserving replacement for the former pure-Python O(n^2) double
    loop: on the reference 3,017-chunk corpus this returns a BYTE-IDENTICAL edge
    set (same pairs, same sim, same rank_strength) in ~0.6s vs ~265s (461x).

    Contract (unchanged):
      - edges keyed (a, b) with a < b; value {"sim", "rank_strength"}.
      - per-node top-k over neighbours meeting `threshold`; ties broken by sim
        descending then neighbour-index ascending (matches the old stable sort).
      - default assembly = union (j in topk(i) OR i in topk(j)); `mutual=True`
        keeps an edge only when both nodes rank each other in top-k.
      - rank_strength = mean of reciprocal ranks (1.0 only when both rank #1).
    """
    n = len(chunks)
    if n == 0:
        return {}

    # Unit-normalize every vector -> (n, d) matrix. Mirrors normalize(): a
    # zero-norm row stays all-zeros (cosine 0 with everything, so below any
    # positive threshold), exactly as the scalar path treated it.
    # RESOURCE-AWARE (2026-08-12): use float32 for the embedding matrix, not float64.
    # The (n x d) matrix and every (B x n) similarity block are the peak allocations that
    # OOM-kill small hosts; float32 HALVES both (12k x 384 x 8B ~= 37MB -> 18MB; and each
    # sim block n*4B instead of n*8B). Cosine similarity is unaffected at the ~1e-3 threshold
    # granularity this graph uses (float32 carries ~7 significant digits); edge set is stable.
    _FDT = np.float32
    M = np.asarray([c["vec"] for c in chunks], dtype=_FDT)
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    nz = norms[:, 0] > 0
    Mn = np.zeros_like(M)
    Mn[nz] = M[nz] / norms[nz]
    paths = np.array([c["path"] for c in chunks]) if cross_file_only else None

    # ── RESOURCE-AWARE ROW-BATCHING (fixes the O(n^2)-RAM OOM) ──
    # The old path built the FULL n×n cosine matrix (S = Mn @ Mn.T) plus, for
    # cross-file, a SECOND n×n boolean mask — ~n^2*8 + n^2 bytes held at once.
    # Fine at the 3,017-chunk reference corpus (~73 MB); at 12k+ chunks that's
    # >1.2 GB PER matrix and OOM-kills on small hosts (the corpus grew, the
    # allocation was never re-checked). We now stream rows in blocks: each block
    # computes only (B × n) similarities, so peak RAM is O(B·n) not O(n²), while
    # producing a BYTE-IDENTICAL edge set (same pairs, sims, ranks, tie-breaks).
    # Batch size auto-scales to available RAM (checks the hardware instead of
    # assuming it's big enough) with a safe floor/ceiling.
    def _avail_bytes():
        try:  # linux: read MemAvailable, no psutil dep
            for ln in open("/proc/meminfo"):
                if ln.startswith("MemAvailable:"):
                    return int(ln.split()[1]) * 1024
        except Exception:
            pass
        return 1 << 30  # conservative 1 GB fallback
    bytes_per_row = n * 4  # one float32 similarity row (was float64 n*8)
    avail = _avail_bytes()
    # RESOURCE-AWARE BUDGET (2026-08-12): scale the fraction of free RAM we grab to how
    # much is ACTUALLY free, so a memory-pressured host goes SLOWER (smaller blocks, more
    # iterations) instead of grabbing 25% blindly and getting OOM-killed. Under ~1.5 GB free
    # -> 10% (tiny blocks, crawl but survive); under ~3 GB -> 18%; otherwise 25%. Also a hard
    # 512 MB cap per block so a huge host can't accidentally allocate a giant temp either.
    if avail < (1500 << 20):
        frac, note = 0.10, "LOW-MEM: crawling (10% budget)"
    elif avail < (3 << 30):
        frac, note = 0.18, "tight mem (18% budget)"
    else:
        frac, note = 0.25, "ample mem (25% budget)"
    budget = min(int(avail * frac), 512 << 20)  # <=512MB per block, hard cap
    batch = max(32, min(2048, budget // max(bytes_per_row, 1)))
    # Explicit override (installers on tiny/huge hosts, or tests forcing block
    # boundaries): DINOMEM_GRAPH_BATCH=<rows>. Clamped to [1, n].
    _ov = _os.environ.get("DINOMEM_GRAPH_BATCH")
    if _ov:
        try:
            batch = max(1, min(n, int(_ov)))
            note = f"override B={batch}"
        except ValueError:
            pass
    est_full = n * n * 4 / 1e9
    print(f"[graph] {n} chunks | full-matrix would need ~{est_full:.2f} GB; "
          f"row-batching at B={batch} (peak ~{batch*bytes_per_row/1e6:.0f} MB, {note}). "
          f"avail~{avail/1e9:.1f} GB", file=sys.stderr)

    # Pass 1 (batched): per-node top-k neighbour sets + 1-based ranks + the sim of
    # each kept neighbour (stashed so Pass 2 never needs the full matrix). lexsort
    # with primary key -sim, secondary key j reproduces the old ordering exactly.
    topk_sets = [set() for _ in range(n)]
    rank_of = [dict() for _ in range(n)]
    sim_of = [dict() for _ in range(n)]  # (i -> {j: cosine}) for kept neighbours only
    for start in range(0, n, batch):
        stop = min(start + batch, n)
        Sb = Mn[start:stop] @ Mn.T  # (b, n) block — the only large temp
        # self-pairs: row r (global i=start+r) excludes column i
        for r in range(stop - start):
            Sb[r, start + r] = -np.inf
        if cross_file_only:  # same-path pairs -> -inf (never selected), block-local
            Sb[paths[start:stop][:, None] == paths[None, :]] = -np.inf
        for r in range(stop - start):
            i = start + r
            row = Sb[r]
            elig = np.where(row >= threshold)[0]
            if elig.size == 0:
                continue
            order = np.lexsort((elig, -row[elig]))
            top = elig[order][:topk]
            topk_sets[i] = {int(j) for j in top}
            for rank, j in enumerate(top, start=1):
                j = int(j)
                rank_of[i][j] = rank
                sim_of[i][j] = float(row[j])  # exact cosine for this kept pair
        del Sb

    # Pass 2: assemble edges (identical logic; sim comes from the stashed dict,
    # which holds the SAME raw cosine the old S[a,b] lookup returned).
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
            # raw cosine for the kept pair: prefer whichever endpoint stashed it.
            sim = sim_of[i].get(j, sim_of[j].get(i))
            edges[(a, b)] = {"sim": float(sim), "rank_strength": round(rank_strength, 4)}
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
    ap.add_argument(
        "--forward-refs-max-nodes",
        type=int,
        default=1500,
        help="compute the forward-reference (knowledge-gap) report only when node "
             "count <= this. The forward-ref scan is O(terms x nodes) fuzzy string "
             "matching with no clean fast+exact form; above this size it dominates "
             "runtime for a secondary report, so we skip it and keep the graph build "
             "fast. Set 0 to always compute, a large value to never skip. When it "
             "runs, it runs the EXACT algorithm (no fidelity change).",
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
    # Forward reference tracking — surface knowledge gaps.
    # Gated by node count: the scan is O(terms x nodes) pure-Python fuzzy matching
    # (difflib) with no clean fast+exact form, so above the threshold it would
    # dominate an otherwise-fast graph build for a secondary report. Below the
    # threshold it runs the exact original algorithm (identical output).
    ff_cap = args.forward_refs_max_nodes
    if ff_cap and len(nodes) > ff_cap:
        print(f"   ⏭️  forward-refs skipped: {len(nodes)} nodes > "
              f"--forward-refs-max-nodes={ff_cap} (secondary report; graph unaffected)")
        forward_refs = []
    else:
        forward_refs = find_forward_refs(chunks, nodes)
        write_forward_refs_md(forward_refs, FORWARD_REFS_FILE, now)

    # Entity nodes from explicit [entity] lines + entity→chunk edges.
    # NOTE: built BEFORE relation edges now — P2 needs each entity's representative nodes[] idx
    # (node_idx backref) so extract_relation_edges can resolve short entity-name endpoints to a
    # VALID nodes[] index (graph_search walks nodes[], not entity_nodes[]).
    memory_dir = _WS / "memory"
    entity_nodes = extract_entity_nodes(memory_dir)
    entity_edges = []
    if entity_nodes and nodes:
        from difflib import SequenceMatcher
        node_texts = [
            (n["title"] + " " + n["snippet"]).lower()
            for n in nodes
        ]
        # P2: node_idx backref via SUBSTRING CONTAINMENT, not ratio. An entity NAME is a tiny
        # fraction of a 60-200 char title+snippet, so SequenceMatcher.ratio() tops out well below
        # threshold (measured: `Project Advancer` maxes 0.516) -> ratio can NEVER pick a
        # representative chunk. Containment is the right relation: the representative chunk is one
        # whose text CONTAINS the entity name. Pick the highest-degree containing chunk (most
        # connected = most central mention); -1 if the name never appears verbatim (honestly
        # unmatchable, ~214/361 by measurement — those relation endpoints stay a=-1 as before).
        # entity_edges kept on the OLD ratio>=0.75 rule so that legacy consumer is unchanged.
        for ent in entity_nodes:
            name_lower = ent["name"].lower()
            containing = [i for i, nt in enumerate(node_texts) if name_lower in nt]
            if containing:
                # highest-degree containing chunk = most central representative
                ent["node_idx"] = max(containing, key=lambda i: nodes[i].get("degree", 0))
            else:
                ent["node_idx"] = -1
            # legacy entity_edges (unchanged behavior): ratio>=0.75 refs
            for idx, nt in enumerate(node_texts):
                score = SequenceMatcher(None, name_lower, nt[:len(name_lower) + 30]).ratio()
                if score >= 0.75:
                    entity_edges.append({
                        "type": "entity_ref",
                        "entity": ent["name"],
                        "node_idx": idx,
                        "confidence": 0.85,
                    })

    # Relation edges from explicit [relation] lines + spaCy SVO.
    # P2: endpoints resolve through entity NAMES -> entity.node_idx (valid nodes[] index),
    # so pass the node_idx-backreffed entity_nodes in.
    relation_edges = extract_relation_edges(memory_dir, nodes, entity_nodes)

    _graph_payload = {
        "generated": now,
        "params": vars(args),
        "node_count": len(nodes),
        "edge_count": len(edge_list),
        "relation_edge_count": len(relation_edges),
        "entity_node_count": len(entity_nodes),
        "entity_edge_count": len(entity_edges),
        "nodes": nodes,
        "edges": edge_list,
        "relation_edges": relation_edges,
        "entity_nodes": entity_nodes,
        "entity_edges": entity_edges,
        "forward_refs": forward_refs,
    }
    # P2 #6: persist THROUGH graph_store -> writes indexed graph.db AND dual-writes
    # memory_graph.json for portability/back-compat. Fallback to raw JSON dump if
    # the store import fails (keeps the builder working in a base/graph-less env).
    try:
        import sys as _sys
        _sys.path.insert(0, str(_WS / "procedures"))
        import graph_store as _gstore
        _store = _gstore.open(json_path=str(OUT_DIR / "memory_graph.json"),
                              db_path=str(OUT_DIR / "graph.db"))
        _store.replace_all(_graph_payload, dual_write_json=True)
        _store.close()
    except Exception as _e:
        print(f"⚠️ graph_store persist failed ({_e}); falling back to raw JSON", file=sys.stderr)
        (OUT_DIR / "memory_graph.json").write_text(
            json.dumps(_graph_payload, indent=2, ensure_ascii=False),
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
    print(f"   {len(entity_nodes)} entity node(s) extracted")




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
