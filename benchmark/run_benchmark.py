#!/usr/bin/env python3
"""dinomem benchmark runner — LongMemEval-style A/B measurement harness.

Loads graded cases from benchmark/scenarios/*.jsonl, runs each through one or
more retrieval configs (base|neuron|full), scores expect/forbid, and writes a
per-config + per-scenario report to benchmark/results/.

Design notes
------------
* Deterministic-first: the retrieval legs are real subprocess calls to the
  on-disk tools (hybrid_recall, graph_search, valid_time). No LLM in the loop,
  so scores are reproducible and cheap. This is a RETRIEVAL benchmark: it asks
  "did the right memory surface, and did the wrong one stay buried?", which is
  exactly the false-promotion / stale-recall / multi-hop axis the proposal
  wants measured before tuning the evidence engine.
* A case's `expect` = substrings that MUST appear in the fused retrieval text.
  `forbid` = substrings that must NOT appear (false-promotion / privacy-leak
  guards). latency_cost cases score against `budget` instead.
"""
import argparse, json, os, subprocess, sys, time, glob
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCEN_DIR = os.path.join(ROOT, "benchmark", "scenarios")
RES_DIR = os.path.join(ROOT, "benchmark", "results")
PY = sys.executable

# neuron = base + upgrade layer (SUPERSET). A neuron install has everything base
# has PLUS hybrid_recall/graph/temporal/scope. There is no "neuron minus graph"
# install, so the A/B is exactly two real configs: the base floor vs the full
# neuron stack. That is what the upgrade actually buys you.
CONFIGS = ("base", "neuron")

# scenario id -> filename stem
SCENARIOS = [
    "temporal", "contradiction", "multi_hop", "entity_res", "noise_resistance",
    "longitudinal", "false_promotion", "behavior", "recovery", "privacy_leak",
    "latency_cost", "structured_query",
]


def _tool_path(name):
    """Resolve a backing tool; return None if absent (=> scenario auto-skips)."""
    for sub in ("tools", "procedures"):
        p = os.path.join(ROOT, sub, f"{name}.py")
        if os.path.exists(p):
            return p
    return None


def _run(cmd, timeout=60):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    except subprocess.TimeoutExpired:
        out = "__TIMEOUT__"
    except Exception as e:  # noqa: BLE001
        out = f"__ERROR__ {e}"
    return out, time.time() - t0


def retrieve_structured(case):
    """EXACT/SQL door: fork to data_query. Orthogonal to the fuzzy legs.
    Returns (text, meta, skip_reason|None)."""
    dq = _tool_path("data_query")
    if not dq:
        return "", {"latency_s": 0.0, "calls": 0}, "data_query not installed"
    cmd = [PY, dq, "--sql", case["sql"], "--format", "json"]
    out, dt = _run(cmd, timeout=case.get("timeout", 30))
    if "no such table" in out.lower() or "__error__" in out.lower():
        return out, {"latency_s": round(dt, 3), "calls": 1}, "fixture table not registered"
    return out, {"latency_s": round(dt, 3), "calls": 1}, None


def _tokset(s):
    import re
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def retrieve_memory_only(query, case, with_uri=False):
    """base config: memory_search-equivalent. Plain lexical recall over the
    fixture memory/*.md — NO hybrid_recall, NO graph, NO temporal. This is the
    honest public-dinomem floor. Deliberately blind to as_of/scope/relations so
    the A/B delta vs neuron/full is REAL, not inflated.

    Ranking: token-overlap between query and each file body; return top-k bodies
    concatenated. Mirrors what a substring/embedding memory_search would surface,
    minus any temporal/graph machinery base doesn't have.
    """
    corpus_mem = case.get("_fixture_mem")
    if not corpus_mem or not os.path.isdir(corpus_mem):
        return "", {"latency_s": 0.0, "calls": 0}
    t0 = time.time()
    q = _tokset(query)
    scored = []
    for fn in os.listdir(corpus_mem):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(corpus_mem, fn)) as fh:
            body = fh.read()
        # strip front-matter so base can't 'see' valid_at/scope metadata
        parts = body.split("---")
        text = parts[-1] if len(parts) >= 3 else body
        overlap = len(q & _tokset(text))
        if overlap:
            scored.append((overlap, text.strip(), os.path.join(corpus_mem, fn)))
    scored.sort(key=lambda x: -x[0])
    picks = scored[: case.get("k", 8)]
    top = "\n\n".join(t for _, t, _ in picks)
    meta = {"latency_s": round(time.time() - t0, 3), "calls": 1}
    if with_uri:
        return top, meta, [(t, u) for _, t, u in picks]
    return top, meta


def retrieve(query, config, case):
    """Return (text, meta) for a query under a given config.

    base   -> graph_search off, only the fuzzy fact leg via hybrid_recall with a
              narrow intent (no graph/temporal). Approximates memory_search-only.
    neuron -> hybrid_recall default (session/docs/memory legs, NO graph/as_of).
    full   -> hybrid_recall with graph leg + as_of/scope passthrough.
    """
    # base = memory-only floor (own leg, NOT hybrid_recall).
    if config == "base":
        return retrieve_memory_only(query, case)

    # neuron = full stack: hybrid_recall + graph + temporal (as_of) + scope.
    calls = 0
    as_of = case.get("as_of")
    scope = case.get("scope")
    hint = case.get("intent_hint")

    fixture_graph = case.get("_fixture_graph")
    hr = os.path.join(ROOT, "tools", "hybrid_recall.py")
    cmd = [PY, hr, query, "--json", "--k", str(case.get("k", 8))]

    # FAITHFUL neuron = base's memory recall folded in as external-hits (exactly
    # how the live agent feeds memory_search into hybrid_recall) + the extra legs.
    # Without this the memory leg is EMPTY and neuron would lose to base on plain
    # recall — an artifact, not a real result. So neuron is a true superset of base.
    ext_text, _, ext_pairs = retrieve_memory_only(query, case, with_uri=True)
    ext_path = None
    if ext_text:
        import tempfile
        # carry the backing fixture-file uri so the compiler can read scope/valid_at
        # frontmatter and filter these folded memory hits (closes the P2#3 gap).
        hits = [{"content": txt, "score": 1.0 - i * 0.01, "uri": uri}
                for i, (txt, uri) in enumerate(ext_pairs)]
        fd, ext_path = tempfile.mkstemp(prefix="benchext_", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(hits, fh)
        cmd += ["--external-hits", ext_path]
    # Fixture has NO docs/session index (those legs read the LIVE workspace and
    # would inject real-memory noise into a fixture run). So restrict neuron to
    # the fixture-backed legs: memory_external (+graph). 'fact' fires exactly
    # [memory_external, graph_search]; 'relational' flips the order for hops.
    eff_hint = hint if hint in ("fact", "relational") else "fact"
    cmd += ["--intent-hint", eff_hint]
    if as_of:
        cmd += ["--as-of", as_of]
    if scope:
        cmd += ["--scope", scope]

    # Point hybrid_recall's OWN graph leg at the fixture graph via env, so its
    # graph candidates flow THROUGH the compiler (scope+temporal filtered) instead
    # of a raw side-call that would dump unfiltered start-node snippets (a leak).
    if fixture_graph:
        os.environ["HYBRID_GRAPH_PATH"] = fixture_graph
    out, dt = _run(cmd, timeout=case.get("timeout", 60))
    calls += 1
    # NOTE: no raw graph_search append. Earlier that leaked the unfiltered
    # 'Starting node: ...' snippet (e.g. a stale lead) past the compiler. The
    # graph contribution now comes only via hybrid_recall's compiled graph leg.

    if ext_path:
        try:
            os.remove(ext_path)
        except OSError:
            pass
    return out, {"latency_s": round(dt, 3), "calls": calls}


def score_case(text, meta, case):
    scen = case.get("scenario")
    if scen == "latency_cost":
        budget_s = case.get("budget_latency_s", 30)
        budget_calls = case.get("budget_calls", 3)
        ok = meta["latency_s"] <= budget_s and meta["calls"] <= budget_calls
        detail = f"latency={meta['latency_s']}s/{budget_s}s calls={meta['calls']}/{budget_calls}"
        return ok, detail

    low = text.lower()
    missing = [e for e in case.get("expect", []) if e.lower() not in low]
    leaked = [f for f in case.get("forbid", []) if f.lower() in low]
    ok = not missing and not leaked
    parts = []
    if missing:
        parts.append("missing=" + repr(missing))
    if leaked:
        parts.append("LEAKED=" + repr(leaked))
    if ok:
        parts.append("ok")
    return ok, " ".join(parts)


def load_scenarios(only=None):
    cases = []
    for stem in SCENARIOS:
        if only and stem != only:
            continue
        path = os.path.join(SCEN_DIR, f"{stem}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                case = json.loads(ln)
                case.setdefault("scenario", stem)
                cases.append(case)
    return cases


def run(configs, only=None, verbose=False, fixtures=True):
    cases = load_scenarios(only)
    if not cases:
        print("No scenarios found. Populate benchmark/scenarios/*.jsonl", file=sys.stderr)
        return None
    fixture_dir = os.path.join(ROOT, "benchmark", "fixtures", "corpus")
    fixture_graph = os.path.join(fixture_dir, "graph.json")
    fixture_mem = os.path.join(fixture_dir, "memory")
    if fixtures and os.path.exists(fixture_graph):
        for c in cases:
            c["_fixture_graph"] = fixture_graph
            c["_fixture_mem"] = fixture_mem
    results = {c: {"pass": 0, "fail": 0, "skip": 0, "cases": []} for c in configs}
    by_scen = {}
    for case in cases:
        scen = case["scenario"]
        for cfg in configs:
            # structured_query = EXACT/SQL door: config-independent (not a fuzzy leg).
            # Run once (score identical across configs) and auto-skip if data_query absent.
            if scen == "structured_query":
                text, meta, skip = retrieve_structured(case)
                if skip:
                    results[cfg]["skip"] += 1
                    rec = {"id": case.get("id"), "scenario": scen, "ok": None,
                           "detail": f"SKIP: {skip}", **meta}
                    results[cfg]["cases"].append(rec)
                    by_scen.setdefault(scen, {}).setdefault(cfg, {"pass": 0, "fail": 0, "skip": 0})
                    by_scen[scen][cfg]["skip"] += 1
                    if verbose:
                        print(f"[SKIP] {cfg:6} {scen:16} {case.get('id')}  {skip}")
                    continue
                ok, detail = score_case(text, meta, case)
            else:
                text, meta = retrieve(case["query"], cfg, case)
                ok, detail = score_case(text, meta, case)
            results[cfg]["pass" if ok else "fail"] += 1
            rec = {"id": case.get("id"), "scenario": scen, "ok": ok, "detail": detail, **meta}
            results[cfg]["cases"].append(rec)
            by_scen.setdefault(scen, {}).setdefault(cfg, {"pass": 0, "fail": 0, "skip": 0})
            by_scen[scen][cfg]["pass" if ok else "fail"] += 1
            if verbose:
                mark = "PASS" if ok else "FAIL"
                print(f"[{mark}] {cfg:6} {scen:16} {case.get('id')}  {detail}")
    return {"configs": configs, "results": results, "by_scenario": by_scen,
            "n_cases": len(cases), "generated": datetime.now(timezone.utc).isoformat()}


def report_md(summary):
    lines = ["# dinomem benchmark report", "",
             f"generated: {summary['generated']}  |  cases: {summary['n_cases']}", "",
             "## Overall pass-rate by config", "",
             "| config | pass | fail | rate |", "|---|---|---|---|"]
    for cfg in summary["configs"]:
        r = summary["results"][cfg]
        tot = r["pass"] + r["fail"]
        rate = f"{100*r['pass']/tot:.0f}%" if tot else "—"
        lines.append(f"| {cfg} | {r['pass']} | {r['fail']} | {rate} |")
    lines += ["", "## By scenario (pass/total per config)", "",
              "| scenario | " + " | ".join(summary["configs"]) + " |",
              "|---|" + "|".join(["---"] * len(summary["configs"])) + "|"]
    for scen in SCENARIOS:
        if scen not in summary["by_scenario"]:
            continue
        row = [scen]
        for cfg in summary["configs"]:
            d = summary["by_scenario"][scen].get(cfg, {"pass": 0, "fail": 0})
            row.append(f"{d['pass']}/{d['pass']+d['fail']}")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="dinomem benchmark runner")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--scenario", help="run one scenario stem")
    ap.add_argument("--config", choices=CONFIGS, help="run one config (default: both)")
    ap.add_argument("--report", choices=["md", "json"], default="md")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--out", help="explicit output path")
    args = ap.parse_args()

    if not args.all and not args.scenario:
        ap.error("pass --all or --scenario <stem>")
    configs = (args.config,) if args.config else CONFIGS
    only = args.scenario

    summary = run(configs, only=only, verbose=args.verbose)
    if summary is None:
        sys.exit(2)

    os.makedirs(RES_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.out or os.path.join(RES_DIR, f"bench_{ts}.json")
    with open(json_path, "w") as fh:
        json.dump(summary, fh, indent=2)

    if args.report == "md":
        md = report_md(summary)
        md_path = os.path.splitext(json_path)[0] + ".md"
        with open(md_path, "w") as fh:
            fh.write(md)
        print(md)
        print(f"\nwrote {md_path}\n      {json_path}")
    else:
        print(json.dumps(summary, indent=2))
        print(f"\nwrote {json_path}")


if __name__ == "__main__":
    main()
