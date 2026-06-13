#!/usr/bin/env python3
"""
memory_synthesis.py — Layer 3 "neuron firing" for agent memory.

Reads the association graph (kb/memory_neuron/memory_graph.json) produced by
memory_graph.py, finds clusters of RELATED-BUT-NOT-DUPLICATE memories, and asks
the LLM whether a genuine higher-level pattern emerges across each cluster.

DRAFT-ONLY by design:
  - Output goes to proposals/ (a human review queue), NEVER into the memory pool.
  - Nothing is ingested or indexed. No feedback loop.
  - The LLM is told to be skeptical and return NONE when there's no real pattern
    (apophenia guard). Every kept insight carries confidence + source provenance.

Usage:
  python3 memory_synthesis.py             # synthesize, write draft to proposals/
  python3 memory_synthesis.py --dry-run   # show clusters only, no LLM calls
  python3 memory_synthesis.py --max-clusters 6

Why thresholds: near-duplicate edges (sim >= ~0.82) are dedup candidates, not
insights, so they're EXCLUDED here. Genuine emergent patterns live in the
medium band (related themes across different days), so we cluster on
LINK_MIN <= sim <= LINK_MAX cross-file edges.
"""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# Reuse the battle-tested LLM caller (gateway + OpenRouter fallback).
sys.path.insert(0, str(Path(__file__).parent))
from extract_memory import call_llm  # noqa: E402

DB_PATH = Path("DINOMEM_SQLITE_PLACEHOLDER")
GRAPH_JSON = Path("DINOMEM_WORKSPACE_PLACEHOLDER/kb/memory_neuron/l2_graph/memory_graph.json")
# Generated L3 insight drafts live in their own subfolder (they accumulate as
# dated files, unlike the always-overwritten L2 graph). kb/memory_neuron/ is
# scanned by no indexer → no feedback loop. Design docs stay in proposals/.
OUT_DIR = Path("DINOMEM_WORKSPACE_PLACEHOLDER/kb/memory_neuron/l3_synthesis")

LINK_MIN = 0.45   # below this = too weak to be a real relation
LINK_MAX = 0.82   # at/above this = near-duplicate (dedup territory, not insight)
MIN_CLUSTER = 3   # need >=3 memories to claim a cross-cutting pattern

LOCK_FILE = OUT_DIR / ".synth.lock"          # single-instance guard (flock)
STATE_FILE = OUT_DIR / ".synth_state.json"  # {sig, last_run} for the run-gate
SYNTH_FILE = OUT_DIR / "SYNTHESIS.md"        # single living snapshot (overwritten)
HISTORY_FILE = OUT_DIR / "insight_history.jsonl"  # append-only first-seen log
CURRENT_INSIGHTS_FILE = OUT_DIR / "current_insights.json"  # structured handoff → L4 promoter
MIN_INTERVAL_HOURS = 12   # L3 is heavy + insights move slowly → throttle
SAME_INSIGHT_RATIO = 0.5  # difflib ratio above which two patterns = "same insight"
WEAK_PENALTY = 0.5  # confidence multiplier for uncertain/unverifiable (not refuted) insights

# Stage-3 objective gates (NON-LLM): hard thresholds an insight must clear to be
# promotion-eligible. These are facts the LLM cannot fabricate, so they backstop
# the (LLM-based) Stage-2 audit.
GATE_MIN_CONVERGENCE = 2     # >=2 independent clusters surfaced the same pattern
GATE_MIN_CONFIDENCE = 0.8    # high bar; WEAK/penalized insights fall out here
GATE_MIN_RECURRENCE = 3      # edge persistence across rebuilds
GATE_USE_RECURRENCE = True   # activated 2026-06-11 — edge_history mature (49/56 edges recurrence ≥3)

# Adaptive cluster cap: grows with the graph so more memory → more insights,
# but floored (stay useful when small) and ceiled (bound LLM cost when huge).
NODES_PER_CLUSTER = 10     # ~1 synthesis slot per 10 neurons
CLUSTERS_FLOOR = 6
CLUSTERS_CEIL = 40


def adaptive_cap(node_count):
    return max(CLUSTERS_FLOOR, min(CLUSTERS_CEIL, node_count // NODES_PER_CLUSTER))


def memory_signature():
    """Content signature of the indexed memory corpus (path+hash). Matches the
    L2 graph's signature scheme so 'changed?' means the same thing in both."""
    import hashlib

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT path, hash FROM files ORDER BY path").fetchall()
    con.close()
    blob = "\n".join(f"{p}:{h}" for p, h in rows)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_graph():
    if not GRAPH_JSON.exists():
        sys.exit(f"graph not found: {GRAPH_JSON} (run memory_graph.py first)")
    return json.loads(GRAPH_JSON.read_text(encoding="utf-8"))


def load_chunk_texts(ids):
    """Map chunk id -> full text from the live index."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    qmarks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT id, path, text FROM chunks WHERE id IN ({qmarks})", list(ids)
    ).fetchall()
    con.close()
    return {cid: (path, text) for cid, path, text in rows}


def build_clusters(graph):
    """Ego-clusters over medium-similarity CROSS-FILE edges.

    Connected-components collapses a homogeneous corpus into one giant blob, so
    instead we take each node's tight neighborhood (itself + its strongest
    medium-sim cross-file neighbors), cap the size, then drop near-duplicate and
    subset clusters. Result: focused, topical clusters instead of one mega-blob.
    """
    MAX_CLUSTER = 6  # keep clusters small enough to yield a focused insight

    adj = {}  # node -> list of (neighbor, sim)
    for e in graph["edges"]:
        if not e["cross_file"]:
            continue
        if LINK_MIN <= e["sim"] <= LINK_MAX:
            ns = e.get("neural_score", e["sim"])
            adj.setdefault(e["a"], []).append((e["b"], ns))
            adj.setdefault(e["b"], []).append((e["a"], ns))

    raw = []
    for node, nbrs in adj.items():
        nbrs_sorted = sorted(nbrs, key=lambda x: x[1], reverse=True)
        members = [node] + [n for n, _ in nbrs_sorted[: MAX_CLUSTER - 1]]
        # require cross-file diversity so it's not all one day's chunks
        files = {NODE_BY_IDX[m]["file"] for m in members if m in NODE_BY_IDX}
        if len(members) >= MIN_CLUSTER and len(files) >= MIN_CLUSTER:
            raw.append(frozenset(members))

    # Dedupe: drop exact dups and any cluster that's a subset of another
    uniq = []
    for c in sorted(raw, key=len, reverse=True):
        if any(c <= u for u in uniq):
            continue
        # also drop heavy overlaps (Jaccard > 0.7) with an existing cluster
        if any(len(c & u) / len(c | u) > 0.7 for u in uniq):
            continue
        uniq.append(c)

    return [sorted(c) for c in uniq]


def build_clusters_louvain(graph):
    """Louvain community detection over medium-similarity CROSS-FILE edges.

    With the L2 graph already pruned by mutual-kNN, modularity-based communities
    no longer collapse into one blob (the failure mode that forced ego-clusters),
    so we can detect natural topic communities directly. Oversized communities are
    trimmed to their densest MAX_CLUSTER core. Same min-size + cross-file diversity
    + dedup guards as the ego method, so downstream LLM semantics are unchanged.
    """
    import networkx as nx

    MAX_CLUSTER = 6

    G = nx.Graph()
    for e in graph["edges"]:
        if not e["cross_file"]:
            continue
        if LINK_MIN <= e["sim"] <= LINK_MAX:
            G.add_edge(e["a"], e["b"], weight=e.get("neural_score", e["sim"]))
    if G.number_of_edges() == 0:
        return []

    comms = nx.community.louvain_communities(G, weight="weight", seed=42)

    raw = []
    for comm in comms:
        members = set(comm)
        # trim oversized community to its densest core by intra-community weight
        if len(members) > MAX_CLUSTER:
            sub = G.subgraph(members)
            strongest = sorted(
                members, key=lambda n: sub.degree(n, weight="weight"), reverse=True
            )
            members = set(strongest[:MAX_CLUSTER])
        files = {NODE_BY_IDX[m]["file"] for m in members if m in NODE_BY_IDX}
        if len(members) >= MIN_CLUSTER and len(files) >= MIN_CLUSTER:
            raw.append(frozenset(members))

    # Dedupe: drop subsets + heavy overlaps (Jaccard > 0.7) — same as ego method.
    uniq = []
    for c in sorted(raw, key=len, reverse=True):
        if any(c <= u for u in uniq):
            continue
        if any(len(c & u) / len(c | u) > 0.7 for u in uniq):
            continue
        uniq.append(c)

    return [sorted(c) for c in uniq]


SYN_PROMPT = """You are auditing an AI agent's own memory for EMERGENT patterns.
Below are several memory entries that are semantically related but came from
different days. Decide if a genuine, non-obvious higher-level pattern or insight
emerges when you read them TOGETHER — something not stated in any single entry.

Be skeptical. Most clusters are just the same topic repeated; that is NOT an
insight. Only report a pattern if it is real and useful. When in doubt, return
verdict "NONE".

MEMORY ENTRIES:
{entries}

If (and only if) the pattern implies an ACTIONABLE behavioral rule for the agent,
also distill it into ONE compact line in this exact grammar:
  "Short Title | kw1, kw2, kw3 | imperative directive"
The directive must be ≤14 words, imperative ("Do X" / "Avoid Y"), not an
observation. If the pattern is just knowledge (not actionable), set rule_line "".
BE PROBABILISTIC, not extreme: never write absolutes ("always/never/guaranteed").
Use "often/tends to/usually" and prefer conditional directives ("when A, lean B").
A rule built on one dramatic outcome with no base rate is invalid — leave it "".

Output ONLY valid JSON:
{{
  "verdict": "KEEP" or "NONE",
  "pattern": "one or two sentences; the emergent insight (empty if NONE)",
  "confidence": 0.0-1.0,
  "novelty": "obvious" | "notable" | "surprising",
  "why": "brief justification grounded in the entries",
  "rule_line": "Title | kw1, kw2, kw3 | imperative directive  (or empty string)"
}}"""


def synthesize(cluster, texts):
    entries = []
    sources = []
    for idx in cluster:
        node = NODE_BY_IDX.get(idx)
        if not node:
            continue
        cid = node["id"]
        path, text = texts.get(cid, (node["file"], node["snippet"]))
        sources.append(Path(path).name)
        entries.append(f"- ({Path(path).name}) {text.strip()[:400]}")
    prompt = SYN_PROMPT.format(entries="\n".join(entries))
    ok, raw = call_llm(prompt, max_tokens=600)
    if not ok:
        return None
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start : end + 1])
    except Exception:
        return None
    obj["sources"] = sorted(set(sources))
    return obj


MERGE_PROMPT = """You are de-duplicating insights produced from an AI agent's
memory. Several were generated independently from different memory clusters and
some express the SAME underlying pattern in different words. Merge those that
say essentially the same thing into one; keep genuinely distinct ones separate.

When multiple inputs express the same pattern, that convergence is a RELIABILITY
signal — reflect it in "occurrences" (how many inputs collapsed into this one)
and you may raise confidence slightly (cap 0.95).

INSIGHTS (JSON):
{payload}

Output ONLY a valid JSON array, one object per DISTINCT pattern:
[{{
  "pattern": "the merged insight, clearest single phrasing",
  "confidence": 0.0-0.95,
  "novelty": "obvious" | "notable" | "surprising",
  "why": "brief justification",
  "occurrences": <int, how many input insights merged into this>,
  "sources": ["file.md", ...],   // union of all merged sources
  "rule_line": "keep the clearest actionable rule line, or empty string"
}}]"""


def merge_insights(kept):
    """Collapse near-duplicate insights via one LLM pass; convergence boosts
    confidence. Returns the merged list (falls back to input on failure)."""
    if len(kept) < 2:
        for k in kept:
            k.setdefault("occurrences", 1)
        return kept
    payload = json.dumps(
        [
            {
                "pattern": k["pattern"],
                "confidence": k.get("confidence"),
                "novelty": k.get("novelty"),
                "why": k.get("why", ""),
                "sources": k.get("sources", []),
                "rule_line": k.get("rule_line", ""),
            }
            for k in kept
        ],
        ensure_ascii=False,
    )
    ok, raw = call_llm(MERGE_PROMPT.format(payload=payload), max_tokens=1500)
    if not ok:
        for k in kept:
            k.setdefault("occurrences", 1)
        return kept
    try:
        start, end = raw.find("["), raw.rfind("]")
        merged = json.loads(raw[start : end + 1])
        if isinstance(merged, list) and merged:
            for m in merged:
                m["sources"] = sorted(set(m.get("sources", [])))
                m.setdefault("occurrences", 1)
            merged.sort(
                key=lambda m: (m.get("occurrences", 1), m.get("confidence", 0)),
                reverse=True,
            )
            return merged
    except Exception:
        pass
    for k in kept:
        k.setdefault("occurrences", 1)
    return kept


NODE_BY_IDX = {}
_LOCK_FH = None  # held-open file handle for the single-instance flock


VERIFY_PROMPT = """You are an INDEPENDENT adversarial auditor checking a claimed
"emergent insight" extracted from an AI agent's memory. Your job is to REFUTE
it, not to agree. Most claimed patterns are apophenia — the same topic restated,
coincidence, trivially true, or an over-generalization the evidence doesn't support.

CLAIMED INSIGHT:
{pattern}

THE ACTUAL MEMORY ENTRIES IT CLAIMS TO BE BASED ON:
{evidence}

An emergent insight, by design, states a CONNECTION or pattern that is not written
verbatim in any single entry. That is expected and GOOD — do NOT reject merely
because the synthesis itself is not literally quoted. Judge the building blocks,
not the novelty of the combination.

There are THREE possible verdicts. Choose carefully — do not collapse "unproven"
into "wrong":

REJECT (refuted = TRUE) — only if you can POSITIVELY show ONE of these:
  (a) FABRICATION — it rests on a concrete fact that appears in NO entry.
  (b) CONTRADICTION — it conflicts with what the entries actually say.
  (c) TRIVIAL — it just restates one entry / two bullets from the same entry.
  (d) OVERREACH — it generalizes a single instance into a systematic claim.
  (e) AVAILABILITY / BASE-RATE BIAS — it draws a rule from a salient, dramatic, or
      extreme outcome (a big win/blowup) without any sense of how OFTEN the setup
      actually leads there. One vivid case is an anecdote, not a base rate. As a
      market agent we must think probabilistically: reject absolute claims
      ("X always/never leads to Y") and lessons with no denominator.

KEEP (grounded = TRUE, refuted = FALSE) — only if EVERY concrete fact it rests on
is present in the evidence, the pattern is a reasonable synthesis across MULTIPLE
entries, and it is non-trivial.

WEAK (grounded = FALSE, refuted = FALSE) — the DEFAULT when you are genuinely
uncertain: you can neither confirm every fact nor positively refute it, OR the
evidence is too thin to judge. Do NOT reject these — they are unproven, not wrong.

Output ONLY valid JSON:
{{
  "grounded": true or false,
  "refuted": true or false,
  "verdict": "KEEP" or "WEAK" or "REJECT",
  "reason": "one sentence"
}}"""


def verify_call(prompt, model, timeout=120):
    """One-shot call to a SPECIFIC model (cross-model verification). (ok, text)."""
    import subprocess
    try:
        r = subprocess.run(
            [__import__('shutil').which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw", "capability", "model", "run",
             "--prompt", prompt, "--gateway", "--json", "--model", model],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            out = json.loads(r.stdout)
            if out.get("ok") and out.get("outputs"):
                t = out["outputs"][0].get("text", "")
                # gateway returns ok=true even when the provider rejected the call;
                # such replies carry an error sentinel, not JSON — treat as abstain.
                if t and "request failed" not in t.lower():
                    return True, t
    except Exception:
        pass
    return False, ""


def verify_insight(insight, evidence, models):
    """Adversarial cross-model audit of one insight. Returns (status, verdicts)
    where status is "keep" | "weak" | "reject".

    Three-way, not binary: only an ACTIVE refutation (fabrication/contradiction/
    trivial/overreach) hard-kills an insight ("reject"). Things that are merely
    unproven — uncertain verdicts, ungrounded-but-not-refuted, or unverifiable
    (no model responded) — degrade to "weak" instead of dying. Weak insights
    survive into the draft with a confidence penalty; they are unproven, not wrong.
    A model that errors ABSTAINS (never counts as approval).
    """
    prompt = VERIFY_PROMPT.format(
        pattern=insight["pattern"], evidence=evidence or "(no evidence text available)"
    )
    # No models specified → same-model audit via the default synthesizer model
    # (call_llm). The adversarial prompt alone flips the model's stance; catches
    # over-claims/ungrounded extrapolation, but NOT shared blind spots. Pass
    # explicit --verifier-models for cross-vendor independence.
    if models:
        raw_results = [(m, *verify_call(prompt, m)) for m in models]
    else:
        ok, raw = call_llm(prompt, max_tokens=400)
        raw_results = [("default", ok, raw)]
    verdicts = []
    for m, ok, raw in raw_results:
        if not ok:
            verdicts.append({"model": m, "ok": False})
            continue
        try:
            s, e = raw.find("{"), raw.rfind("}")
            obj = json.loads(raw[s : e + 1])
            obj["model"], obj["ok"] = m, True
            verdicts.append(obj)
        except Exception:
            verdicts.append({"model": m, "ok": False})
    responded = [v for v in verdicts if v.get("ok")]
    if not responded:
        return "weak", verdicts  # unverifiable: unproven, not wrong → degrade, don't kill
    # An active refutation by ANY responding model is fatal.
    if any(v.get("refuted") or v.get("verdict") == "REJECT" for v in responded):
        return "reject", verdicts
    # Full keep needs a majority of confident, grounded KEEPs.
    keeps = sum(1 for v in responded
                if v.get("verdict") == "KEEP" and v.get("grounded") is not False)
    if keeps >= len(responded) / 2.0:
        return "keep", verdicts
    return "weak", verdicts  # uncertain / ungrounded-but-not-refuted → degrade


def write_l3_confidence(kept_insights, cluster_raw, out_dir):
    """Feed L3 confidence back to L2 edges: every edge that participated in a
    verified/weak insight cluster gets a confidence boost, weighted by insight
    confidence. Weak insights contribute at 30 % strength. Persisted in
    l3_edge_confidence.json so the next graph build folds it into neural_score."""
    from collections import defaultdict

    # Map each source file to clusters that contained it
    source_to_clusters = defaultdict(list)
    for cluster, raw in cluster_raw:
        for src in raw.get("sources", []):
            source_to_clusters[src].append(list(cluster))

    l3_conf = {}
    for insight in kept_insights:
        sources = insight.get("sources", [])
        conf = float(insight.get("confidence", 0.5))
        status = insight.get("verify_status")
        if status == "weak":
            conf *= 0.3
        elif status != "verified":
            continue
        if conf <= 0:
            continue

        seen = set()
        for src in sources:
            for cluster in source_to_clusters.get(src, []):
                c_key = tuple(sorted(cluster))
                if c_key in seen:
                    continue
                seen.add(c_key)
                for i in range(len(cluster)):
                    for j in range(i + 1, len(cluster)):
                        a, b = cluster[i], cluster[j]
                        node_a = NODE_BY_IDX.get(a)
                        node_b = NODE_BY_IDX.get(b)
                        if not node_a or not node_b:
                            continue
                        key = "||".join(sorted((str(node_a["id"]), str(node_b["id"]))))
                        l3_conf[key] = max(l3_conf.get(key, 0.0), conf)

    if l3_conf:
        out_path = out_dir / "l3_edge_confidence.json"
        out_path.write_text(json.dumps(l3_conf, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote L3 confidence for {len(l3_conf)} edges → {out_path}")


def objective_gate(insight):
    """Stage 3: non-LLM hard gates deciding promotion-eligibility.

    Returns (passed, reasons). Unlike the Stage-2 audit (LLM judging LLM), these
    are objective facts the model can't fabricate: convergence across independent
    clusters + a high confidence floor. The recurrence gate is wired but DISABLED
    (GATE_USE_RECURRENCE) until edge_history accumulates real cross-rebuild depth.
    """
    reasons = []
    conv = insight.get("occurrences", 1)
    if conv < GATE_MIN_CONVERGENCE:
        reasons.append(f"convergence {conv}<{GATE_MIN_CONVERGENCE}")
    try:
        conf = float(insight.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < GATE_MIN_CONFIDENCE:
        reasons.append(f"confidence {conf}<{GATE_MIN_CONFIDENCE}")
    if GATE_USE_RECURRENCE:
        rec = insight.get("recurrence", 0)
        if rec < GATE_MIN_RECURRENCE:
            reasons.append(f"recurrence {rec}<{GATE_MIN_RECURRENCE}")
    return (not reasons), reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show clusters, no LLM")
    ap.add_argument("--max-clusters", type=int, default=None,
                    help="override the adaptive cap (default: scales with node count)")
    ap.add_argument("--force", action="store_true", help="ignore change-gate + throttle")
    ap.add_argument("--min-interval-hours", type=float, default=MIN_INTERVAL_HOURS)
    ap.add_argument("--cluster-method", choices=["ego", "louvain"], default="ego",
                    help="ego = per-node neighborhoods (legacy); louvain = community detection on mutual-kNN graph")
    ap.add_argument("--verifier-models", default="",
                    help="comma-separated models for Stage-2 audit. Empty = same default model, adversarial prompt only. Set cross-vendor refs (e.g. kimi-coding/k2.6-code-preview,google/gemini-2.5-flash) for independent verification.")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip Stage-2 cross-model verification (debug/compare only)")
    args = ap.parse_args()

    # Single-instance guard: L3 fans out into many heavy LLM subprocesses, so two
    # overlapping runs (orchestrator cron firing while a manual --force is live,
    # or several --force runs stacked) can exhaust RAM and OOM-kill each other.
    # Take a NON-BLOCKING exclusive lock; if another run holds it, skip cleanly
    # (exit 0, not an error — the next cycle will pick it up). The fd is kept open
    # for the process lifetime via a module global; the lock releases on exit.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    global _LOCK_FH
    _LOCK_FH = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⏭️  another L3 synthesis run is already in progress — skipping")
        return
    _LOCK_FH.write(str(os.getpid()))
    _LOCK_FH.flush()

    # Run-gate: only synthesize if memory CHANGED since last L3 run AND enough
    # time has passed (throttle). Keeps a fully-automated L3 cheap + quiet.
    sig = memory_signature()
    if not args.force and not args.dry_run and STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            unchanged = state.get("sig") == sig
            last = state.get("last_run")
            too_soon = False
            if last:
                elapsed_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
                too_soon = elapsed_h < args.min_interval_hours
            if unchanged:
                print("⏭️  memory unchanged since last L3 — skipping")
                return
            if too_soon:
                print(f"⏭️  last L3 run < {args.min_interval_hours}h ago — throttled, skipping")
                return
        except Exception:
            pass  # malformed state → proceed and rewrite it

    graph = load_graph()
    global NODE_BY_IDX
    NODE_BY_IDX = {n["idx"]: n for n in graph["nodes"]}

    if args.cluster_method == "louvain":
        clusters = build_clusters_louvain(graph)
    else:
        clusters = build_clusters(graph)
    if not clusters:
        print("no medium-similarity cross-file clusters — nothing to synthesize")
        return
    cap = args.max_clusters if args.max_clusters else adaptive_cap(len(graph["nodes"]))
    total = len(clusters)
    clusters = clusters[:cap]
    print(f"{total} cluster(s) found · cap={cap} ({len(graph['nodes'])} nodes) → processing {len(clusters)}")

    if args.dry_run:
        for i, c in enumerate(clusters, 1):
            files = sorted({NODE_BY_IDX[idx]["file"] for idx in c})
            print(f"  cluster {i}: {len(c)} nodes across {len(files)} files → {files}")
        return

    all_ids = {NODE_BY_IDX[idx]["id"] for c in clusters for idx in c}
    texts = load_chunk_texts(all_ids)

    kept = []
    cluster_raw = []
    for i, c in enumerate(clusters, 1):
        print(f"  synthesizing cluster {i}/{len(clusters)} ({len(c)} nodes)...")
        res = synthesize(c, texts)
        if res and res.get("verdict") == "KEEP" and res.get("pattern"):
            res["_cluster"] = list(c)
            kept.append(res)
            cluster_raw.append((c, res))

    raw_count = len(kept)
    if kept:
        print(f"  merging {raw_count} insight(s) (dedup + convergence boost)...")
        kept = merge_insights(kept)
        print(f"  → {len(kept)} distinct insight(s) after merge")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Stage 2: independent cross-model verification (apophenia guard) ──
    # Each merged insight is audited by model(s) DIFFERENT from the synthesizer,
    # prompted to REFUTE + check grounding against the actual source entries.
    # This stands in for the human's independent check in the zero-human pipeline.
    if kept and not args.no_verify:
        vmodels = [m.strip() for m in args.verifier_models.split(",") if m.strip()]
        vlabel = f"{len(vmodels)} independent model(s): {vmodels}" if vmodels else "default model (adversarial prompt)"
        print(f"  verifying {len(kept)} insight(s) via {vlabel}")
        by_file = {}
        for _cid, (path, text) in texts.items():
            by_file.setdefault(Path(path).name, []).append(text.strip()[:400])
        verified, weak, rejected = [], [], []
        for k in kept:
            evidence = "\n".join(
                f"- ({fn}) {t}" for fn in k.get("sources", []) for t in by_file.get(fn, [])
            )
            status, verdicts = verify_insight(k, evidence, vmodels)
            k["verification"] = verdicts
            if status == "reject":
                k["verify_status"] = "rejected"
                rejected.append(k)
                mark = "❌ REJECT"
            elif status == "weak":
                # Unproven, not wrong: keep but penalize confidence so it ranks
                # below verified insights and can't masquerade as trusted.
                k["verify_status"] = "weak"
                try:
                    c = float(k.get("confidence"))
                    k["confidence_raw"] = c
                    k["confidence"] = round(c * WEAK_PENALTY, 2)
                except (TypeError, ValueError):
                    pass
                weak.append(k)
                mark = "⚠️  WEAK"
            else:
                k["verify_status"] = "verified"
                verified.append(k)
                mark = "✅ KEEP"
            print(f"    {mark} — {k['pattern'][:60]}...")
        if rejected:
            rej_path = OUT_DIR / "rejected_insights.jsonl"
            with rej_path.open("a", encoding="utf-8") as f:
                for r in rejected:
                    f.write(json.dumps(
                        {"date": stamp, "pattern": r["pattern"],
                         "confidence": r.get("confidence"), "verification": r.get("verification")},
                        ensure_ascii=False) + "\n")
        print(f"  → {len(verified)} verified, {len(weak)} weak, {len(rejected)} rejected by independent audit")
        # Weak insights survive into the draft (flagged), ranked after verified ones.
        kept = verified + weak
        # Feed L3 confidence back to L2 edges for next graph build
        if cluster_raw:
            write_l3_confidence(kept, cluster_raw, OUT_DIR)

    # ── Stage 3: objective (non-LLM) gates → promotion-eligibility tag ──
    # Hard thresholds the LLM can't fabricate. Nothing is promoted yet (that's a
    # later stage); we only TAG each insight so the future auto-promoter can trust
    # `gate_status == "promotable"`. WEAK insights are never promotable by design.
    n_promotable = 0
    for k in kept:
        if k.get("verify_status") == "weak":
            k["gate_status"], k["gate_reasons"] = "draft", ["weak/unverified"]
            continue
        passed, reasons = objective_gate(k)
        k["gate_status"] = "promotable" if passed else "draft"
        k["gate_reasons"] = reasons
        if passed:
            n_promotable += 1
    if kept:
        rec_note = "" if GATE_USE_RECURRENCE else " (recurrence gate deferred)"
        print(f"  → Stage 3 gates: {n_promotable}/{len(kept)} promotable{rec_note}")

    # First-seen tracking: match each current insight against the append-only
    # history (fuzzy, so LLM rephrasings don't count as new). Reuse the prior
    # first_seen date for known patterns; log + date the genuinely new ones.
    history = []
    if HISTORY_FILE.exists():
        for ln in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    history.append(json.loads(ln))
                except Exception:
                    pass
    new_entries = []
    for k in kept:
        pat = k["pattern"].lower()
        best = max(
            (SequenceMatcher(None, pat, h["pattern"].lower()).ratio() for h in history),
            default=0.0,
        )
        match = None
        if history:
            match = max(history, key=lambda h: SequenceMatcher(None, pat, h["pattern"].lower()).ratio())
        if best >= SAME_INSIGHT_RATIO and match:
            k["first_seen"] = match.get("first_seen", today)
        else:
            k["first_seen"] = today
            entry = {
                "first_seen": today,
                "pattern": k["pattern"],
                "confidence": k.get("confidence"),
                "novelty": k.get("novelty"),
                "sources": k.get("sources", []),
            }
            new_entries.append(entry)
    if new_entries:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for e in new_entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    lines = [
        f"# Memory Synthesis (layer 3) — current snapshot",
        f"_generated {stamp} · {len(kept)} distinct insight(s) from {raw_count} raw / {len(clusters)} cluster(s) · {len(new_entries)} new this run_",
        "",
        "> DRAFT for human review. NOT memory. Nothing here is indexed or trusted",
        "> until you promote it. Each item is an LLM-proposed emergent pattern with",
        "> confidence + source provenance — verify before believing.",
        "> `convergence` = how many independent clusters surfaced the same pattern.",
        "> `first seen` = when this insight first appeared (see insight_history.jsonl).",
        "",
    ]
    if not kept:
        lines.append("_No genuine emergent patterns found this run (all clusters returned NONE)._")
    for i, k in enumerate(kept, 1):
        occ = k.get("occurrences", 1)
        conv = f" · convergence: **{occ}×**" if occ > 1 else ""
        newtag = " · 🆕 NEW" if k.get("first_seen") == today else ""
        weaktag = ""
        if k.get("verify_status") == "weak":
            raw = k.get("confidence_raw")
            penalized = f" (raw {raw} → ×{WEAK_PENALTY})" if raw is not None else ""
            weaktag = f" · ⚠️ WEAK — unproven by audit, not refuted{penalized}"
        if k.get("gate_status") == "promotable":
            gatetag = "- gate: ✅ PROMOTABLE (objective gates passed)"
        else:
            gr = ", ".join(k.get("gate_reasons", [])) or "—"
            gatetag = f"- gate: 🔒 draft — fails: {gr}"
        lines += [
            f"## {i}. {k['pattern']}",
            f"- confidence: **{k.get('confidence','?')}** · novelty: {k.get('novelty','?')}{conv}{weaktag}",
            gatetag,
            f"- first seen: {k.get('first_seen', today)}{newtag}",
            f"- why: {k.get('why','')}",
            f"- sources: {', '.join(k.get('sources', []))}",
            "",
        ]
    SYNTH_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Structured handoff for the Stage-4 promoter (memory_promote.py). Markdown is
    # for humans; this JSON is the machine contract. Only fields the promoter needs.
    CURRENT_INSIGHTS_FILE.write_text(
        json.dumps(
            {
                "generated": stamp,
                "sig": sig,
                "insights": [
                    {
                        "pattern": k["pattern"],
                        "confidence": k.get("confidence"),
                        "confidence_raw": k.get("confidence_raw", k.get("confidence")),
                        "occurrences": k.get("occurrences", 1),
                        "verify_status": k.get("verify_status"),
                        "gate_status": k.get("gate_status"),
                        "gate_reasons": k.get("gate_reasons", []),
                        "first_seen": k.get("first_seen", today),
                        "sources": k.get("sources", []),
                        "why": k.get("why", ""),
                        "rule_line": k.get("rule_line", ""),
                    }
                    for k in kept
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    STATE_FILE.write_text(
        json.dumps({"sig": sig, "last_run": datetime.now().isoformat()}),
        encoding="utf-8",
    )
    print(f"✅ {SYNTH_FILE.name}: {len(kept)} distinct ({len(new_entries)} new) · history={len(history)+len(new_entries)}")


if __name__ == "__main__":
    main()
