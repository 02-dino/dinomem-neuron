#!/usr/bin/env python3
"""
confidence_engine_test.py — self-contained proof that P1 #1 calibration is
"hard to fool": it deflates a confidence band the model has been WRONG in, and
leaves a band it has been RIGHT in nearly untouched — using REAL outcomes from
the promotion log, not the model's opinion.

No pytest dependency (matches the other *_test.py in procedures/). Run:
    python3 confidence_engine_test.py
Exit 0 = all assertions pass.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _seed(ws: Path):
    l3 = ws / "kb/memory_neuron/l3_synthesis"
    l4 = ws / "kb/memory_neuron/l4_promoted"
    l3.mkdir(parents=True, exist_ok=True)
    l4.mkdir(parents=True, exist_ok=True)

    rows = []
    # 0.8-1.0 band: OVER-CLAIMED — 12 outcomes, only 4 held (rate ~0.36)
    for i in range(12):
        rows.append({"action": "graduate" if i < 4 else "expire",
                     "confidence": 0.85, "pattern": f"high{i}"})
    # 0.6-0.8 band: RELIABLE — 10 outcomes, 9 held (rate ~0.83)
    for i in range(10):
        rows.append({"action": "reinforce" if i < 9 else "demote",
                     "confidence": 0.65, "pattern": f"mid{i}"})
    (l4 / "promotion_log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    ci = {"insights": [
        {"pattern": "over-claimed high-confidence pattern", "confidence": 0.85,
         "confidence_raw": 0.85, "gate_reasons": ["convergence 3>=2"],
         "sources": ["2026-08-01_x.md", "2026-08-02_y.md"]},
        {"pattern": "reliable mid-confidence pattern", "confidence": 0.65,
         "confidence_raw": 0.65, "gate_reasons": ["convergence 2>=2"],
         "sources": ["2026-08-01_z.md"]},
    ]}
    (l3 / "current_insights.json").write_text(json.dumps(ci), encoding="utf-8")


def main():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _seed(ws)
        os.environ["DINOMEM_WORKSPACE"] = str(ws)
        # import AFTER env is set so module-level WS resolves to the temp corpus
        import importlib
        import confidence_engine as ce
        importlib.reload(ce)

        bands = ce.load_or_build_reliability(rebuild=True)
        hi = bands[ce.band_key((0.8, 1.01))]
        mid = bands[ce.band_key((0.6, 0.8))]

        assert hi["total"] == 12 and hi["hits"] == 4, f"hi band tally wrong: {hi}"
        assert mid["total"] == 10 and mid["hits"] == 9, f"mid band tally wrong: {mid}"
        assert hi["rate"] is not None and hi["rate"] < 0.45, f"over-claimed band not low: {hi}"
        assert mid["rate"] is not None and mid["rate"] > 0.75, f"reliable band not high: {mid}"

        ce.calibrate(dry_run=False, rebuild=True)
        doc = json.loads((ws / "kb/memory_neuron/l3_synthesis/current_insights.json").read_text())
        by = {i["pattern"]: i for i in doc["insights"]}
        over = by["over-claimed high-confidence pattern"]
        reli = by["reliable mid-confidence pattern"]

        # over-claimed band gets DEFLATED hard (0.85 -> well under 0.5)
        assert over["calibrated_confidence"] < 0.5, f"over-claim not deflated: {over}"
        # reliable band barely moves (0.65 -> still >= 0.5)
        assert reli["calibrated_confidence"] >= 0.5, f"reliable band over-corrected: {reli}"
        # raw is PRESERVED (never destroyed)
        assert over["confidence_raw"] == 0.85 and reli["confidence_raw"] == 0.65
        # evidence stamped
        assert over["independent_cluster_count"] == 3, f"cluster count wrong: {over}"
        assert reli["evidence"]["source_count"] == 1, f"evidence wrong: {reli}"
        # gate-facing confidence == calibrated (pipeline inherits calibration)
        assert over["confidence"] == over["calibrated_confidence"]

        print("[PASS] over-claimed band 0.85 -> "
              f"{over['calibrated_confidence']} (x{over['calibration']['band_multiplier']})")
        print("[PASS] reliable band   0.65 -> "
              f"{reli['calibrated_confidence']} (x{reli['calibration']['band_multiplier']})")
        print("[PASS] raw preserved, evidence stamped, gate inherits calibration")

    # cold-start: no log => neutral prior => calibration is a strict no-op
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        l3 = ws / "kb/memory_neuron/l3_synthesis"
        l3.mkdir(parents=True, exist_ok=True)
        (l3 / "current_insights.json").write_text(json.dumps({"insights": [
            {"pattern": "cold", "confidence": 0.9, "confidence_raw": 0.9,
             "gate_reasons": [], "sources": []}]}), encoding="utf-8")
        os.environ["DINOMEM_WORKSPACE"] = str(ws)
        import importlib, confidence_engine as ce
        importlib.reload(ce)
        ce.calibrate(dry_run=False, rebuild=True)
        doc = json.loads((l3 / "current_insights.json").read_text())
        cold = doc["insights"][0]
        assert cold["calibrated_confidence"] == 0.9, f"cold-start not no-op: {cold}"
        print("[PASS] cold-start (no log) = strict no-op x1.0 (no regression)")

    print("\nALL PASS — calibration deflates wrong bands, spares right ones, "
          "no-ops on cold start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
