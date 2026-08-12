#!/usr/bin/env python3
"""
explain_memory_test.py — proves P2 #4 joins all three provenance surfaces
(lifecycle + retrieval + confidence) for one memory, in a throwaway temp corpus
(touches no real memory). No pytest dep. Run: python3 explain_memory_test.py
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "memory").mkdir(parents=True)
        (ws / "kb/retrieval_log").mkdir(parents=True)
        (ws / "kb/memory_neuron/l4_promoted").mkdir(parents=True)

        # a current fact backing a trusted, calibrated insight
        f = ws / "memory" / "_pin_atlas_lead.md"
        f.write_text("---\nsubject: atlas.lead\nvalid_from: 2026-06-01\n---\n# Atlas lead\n\nRui leads Atlas.\n",
                     encoding="utf-8")

        # retrieval log: 2 events that returned this file (abs uri + a name-only uri)
        log = ws / "kb/retrieval_log" / "2026-08-12.jsonl"
        rows = [
            {"ts": "2026-08-12T10:00:00Z", "tool": "hybrid_recall", "query": "atlas lead",
             "n_results": 2, "top_scores": [0.91, 0.4],
             "result_uris": [str(f), str(ws / "memory/_pin_other.md")]},
            {"ts": "2026-08-11T09:00:00Z", "tool": "memory_search", "query": "who runs atlas",
             "n_results": 1, "top_scores": [0.77], "result_uris": ["_pin_atlas_lead.md"]},
        ]
        log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

        # promotion_state: a trusted insight sourced from this file, calibrated + evidence-stamped
        promo = {"atlas-lead-rui": {
            "status": "trusted", "reinforce_count": 4, "sources": ["_pin_atlas_lead.md"],
            "confidence": 0.8, "calibrated_confidence": 0.66, "independent_cluster_count": 3,
            "contradictions": [], "evidence": {"source_count": 3, "distinct_day_count": 2},
            "pattern": "Rui leads Atlas"}}
        (ws / "kb/memory_neuron/l4_promoted/promotion_state.json").write_text(
            json.dumps(promo), encoding="utf-8")

        os.environ["DINOMEM_WORKSPACE"] = str(ws)
        # reload the whole chain so each module re-reads DINOMEM_WORKSPACE:
        # valid_time (temporal fields) -> lifecycle_state (view) -> explain_memory (join).
        try:
            import valid_time as vt
            importlib.reload(vt)
        except Exception:
            pass
        import lifecycle_state as ls
        importlib.reload(ls)
        import explain_memory as em
        importlib.reload(em)

        rep = em.explain("memory/_pin_atlas_lead.md", days=365)
        ok = True

        def check(label, cond):
            nonlocal ok
            ok = ok and cond
            print("  %s %s" % ("PASS" if cond else "FAIL", label))

        check("lifecycle=current", rep["lifecycle"]["state"] == "current")
        check("retrieval joined 2 events", rep["retrieval"]["retrieval_count"] == 2)
        check("newest-first order", rep["retrieval"]["events"][0]["ts"] == "2026-08-12T10:00:00Z")
        check("rank+score surfaced", rep["retrieval"]["events"][0]["rank"] == 1
              and rep["retrieval"]["events"][0]["score"] == 0.91)
        check("name-only uri matched", rep["retrieval"]["events"][1]["tool"] == "memory_search")
        check("by_tool tally", rep["retrieval"]["by_tool"].get("hybrid_recall") == 1)
        cf = rep["confidence"]
        check("confidence tracked", cf.get("tracked") is True)
        check("calibrated surfaced", cf.get("calibrated_confidence") == 0.66)
        check("evidence surfaced", cf.get("evidence", {}).get("source_count") == 3)
        check("status=trusted", cf.get("status") == "trusted")

        # query-rank mode: this file should top the ranking for 'atlas'
        ranked = em.rank_by_query("atlas", days=365, top=5)
        check("query-rank finds file", any("_pin_atlas_lead.md" in r["uri"] for r in ranked))

        # cold-degrade: a file with NO signal anywhere = clean untracked/unknown, no crash
        (ws / "memory" / "_note_plain.md").write_text("# plain\n\nnothing.\n", encoding="utf-8")
        rep2 = em.explain("memory/_note_plain.md", days=365)
        check("cold file: unknown lifecycle", rep2["lifecycle"]["state"] == "unknown")
        check("cold file: 0 retrieval", rep2["retrieval"]["retrieval_count"] == 0)
        check("cold file: untracked conf", rep2["confidence"]["tracked"] is False)

        if not ok:
            print("\nFAIL")
            return 1
    print("\nALL PASS — explain joins lifecycle + retrieval + confidence; degrades clean on cold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
