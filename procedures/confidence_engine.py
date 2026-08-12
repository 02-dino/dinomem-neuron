#!/usr/bin/env python3
"""
confidence_engine.py — P1 #1 Evidence + confidence CALIBRATION (the "hard to fool"
learning layer of the Best-in-Class memory upgrade proposal).

The problem it fixes
--------------------
Today an insight's `confidence` is LLM-GENERATED: memory_synthesis asks the model
for a number (e.g. 0.72) and the gate trusts it. That is exactly the thing a good
memory system must NOT do — the model's self-assessment is uncalibrated and easy to
fool (a confidently-wrong pattern gets 0.9 just like a truly-solid one).

What this adds (three pieces the proposal names)
------------------------------------------------
  1. EVIDENCE STAMPING. Every insight gets, on the record:
       - independent_cluster_count : how many INDEPENDENT clusters surfaced it
         (synthesis already computes convergence; we surface it as first-class
         evidence, not just a transient gate reason).
       - contradictions[]          : trusted insights this one conflicts with
         (from the canonical contradiction_check.rules_contradict — no new logic).
       - evidence{}                : source_count, distinct_day_count — cheap,
         non-LLM corroboration facts the model cannot fabricate.

  2. OUTCOME LEDGER. Replays the promotion audit log (promotion_log.jsonl, the
     append-only truth of what actually happened to past insights) into a per-
     confidence-BAND tally of HITS vs MISSES:
       HIT   = graduate | reinforce   (the insight kept proving true)
       MISS  = expire | demote        (it stopped recurring / was pulled from behavior)
       (contradiction events, when logged, count as MISS too.)
     This is the system watching its OWN track record — REAL outcomes, not a model's
     opinion. It is the calibration signal.

  3. CALIBRATION. For each insight, its raw (LLM) confidence is multiplied by the
     OBSERVED reliability of its confidence band:
         calibrated = raw * band_hit_rate
     If insights the model scored 0.8-0.9 only held true 55% of the time, this run's
     0.85 insight is deflated toward 0.47 — the system distrusts a band it has been
     wrong in before, and trusts a band it has been right in. Bands with too little
     history fall back to a neutral prior (no over-correction on thin data).

Design guarantees (same rails as the rest of the L3/L4 stack)
-------------------------------------------------------------
  * NO LLM CALLS in the calibration path (the outcome replay + band math are pure
    bookkeeping). The only LLM touch is the REUSED contradiction check, and only
    when there are trusted rules to compare against — same as today.
  * DETERMINISTIC + REVERSIBLE. calibrated_confidence is RECOMPUTED from the log
    every run; it is never a destructive rewrite. Delete the reliability table and
    the next run rebuilds it. Nothing here can lose a fact.
  * FAIL-OPEN. Missing log / empty history / thin bands => neutral prior (1.0
    multiplier) so a cold-start install behaves exactly like today (no regression),
    and the calibration only *tightens* as real outcomes accumulate.
  * ADDITIVE. Writes new fields onto the insight records + a reliability table
    sidecar; does not remove or rename anything memory_synthesis / memory_promote
    already rely on.

Usage:
  python3 confidence_engine.py                # calibrate current_insights.json in place
  python3 confidence_engine.py --dry-run      # show calibration deltas, write nothing
  python3 confidence_engine.py --report       # print the band reliability table
  python3 confidence_engine.py --rebuild-ledger  # force-rebuild reliability from the log
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Workspace resolution (priority): DINOMEM_WORKSPACE env > install-time sed >
#    self-locate. Mirrors contradiction_check.py so the neuron install path is
#    identical and a moved/partial install still resolves.
_WS_DEFAULT = "/root/.openclaw/workspace-analyst"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run (manual/partial copy)
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WS = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))

L3 = WS / "kb/memory_neuron/l3_synthesis"
L4 = WS / "kb/memory_neuron/l4_promoted"
CURRENT_INSIGHTS = L3 / "current_insights.json"
PROMOTION_LOG = L4 / "promotion_log.jsonl"     # append-only outcome truth
RELIABILITY_TABLE = L3 / "confidence_reliability.json"  # per-band hit-rate sidecar
CALIBRATION_LOG = L3 / "calibration_log.jsonl"  # append-only audit of each run

# Confidence bands (lower-inclusive, upper-exclusive except the top which is closed).
# Coarse on purpose: fine bands starve for outcomes and over-fit. 5 bands is enough
# to separate "the model is reliable up here" from "it over-claims down there".
BANDS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]

# Outcome classification from the promotion audit log.
HIT_ACTIONS = {"graduate", "reinforce", "promote"}
MISS_ACTIONS = {"expire", "demote", "contradicted", "blocked_contradiction"}

# A band needs at least this many observed outcomes before its measured hit-rate is
# trusted. Below it, fall back to the neutral prior (no calibration) so thin data
# never swings a score. This is the anti-overfit guard.
MIN_BAND_OUTCOMES = 8
NEUTRAL_PRIOR = 1.0  # multiplier when a band has too little history (no change)

# Laplace smoothing so a band with few misses isn't scored a perfect 1.0 on luck.
SMOOTH_HITS = 1.0
SMOOTH_TOTAL = 2.0


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def band_of(conf):
    """Return the (lo, hi) band a confidence value falls in. Clamps out-of-range."""
    c = max(0.0, min(1.0, float(conf)))
    for lo, hi in BANDS:
        if lo <= c < hi:
            return (lo, hi)
    return BANDS[-1]


def band_key(band):
    return f"{band[0]:.1f}-{band[1]:.1f}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. OUTCOME LEDGER — replay the promotion audit log into per-band hit/miss tallies
# ─────────────────────────────────────────────────────────────────────────────
def _iter_log(path):
    """Yield parsed rows from an append-only jsonl log; skip malformed lines
    (a truncated last write must never crash calibration). Fail-open: missing
    file => empty iterator => neutral prior downstream."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # tolerate a partial/corrupt line, keep replaying


def build_reliability(log_path=PROMOTION_LOG):
    """Replay the promotion log into per-confidence-band {hits,total,rate}.

    An outcome is attributed to the band of the confidence the insight CARRIED at
    the moment of the event (the row's 'confidence' if present, else 'confidence_raw',
    else skip — we cannot band an outcome with no confidence). HIT/MISS from the
    action taxonomy. This is the REAL track record: 'when the model said 0.85, how
    often did that insight actually hold?'.
    """
    tally = {band_key(b): {"hits": 0, "total": 0} for b in BANDS}
    for row in _iter_log(log_path):
        action = row.get("action")
        if action not in HIT_ACTIONS and action not in MISS_ACTIONS:
            continue  # 'promote' into probation etc. — only terminal-signal actions score
        conf = row.get("confidence", row.get("confidence_raw"))
        if conf is None:
            continue  # cannot attribute an outcome with no confidence to a band
        bk = band_key(band_of(conf))
        tally[bk]["total"] += 1
        if action in HIT_ACTIONS:
            tally[bk]["hits"] += 1

    # Compute a trusted rate per band with Laplace smoothing + a min-outcome floor.
    table = {}
    for bk, t in tally.items():
        hits, total = t["hits"], t["total"]
        if total < MIN_BAND_OUTCOMES:
            rate = None  # thin data -> neutral prior downstream (no calibration)
        else:
            rate = (hits + SMOOTH_HITS) / (total + SMOOTH_TOTAL)
        table[bk] = {"hits": hits, "total": total, "rate": rate}
    return table


def load_or_build_reliability(rebuild=False):
    """Return the band reliability table, rebuilding from the log when asked or
    when the sidecar is absent/stale-shaped. The table is a CACHE of a pure replay,
    so rebuilding is always safe and never loses information."""
    if not rebuild and RELIABILITY_TABLE.exists():
        try:
            cached = json.loads(RELIABILITY_TABLE.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "bands" in cached:
                return cached["bands"]
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # fall through to rebuild
    bands = build_reliability()
    RELIABILITY_TABLE.parent.mkdir(parents=True, exist_ok=True)
    RELIABILITY_TABLE.write_text(
        json.dumps({"generated": now_iso(), "min_band_outcomes": MIN_BAND_OUTCOMES,
                    "bands": bands}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return bands


def band_multiplier(raw_conf, bands):
    """The calibration multiplier for a raw confidence: the observed hit-rate of its
    band, or the neutral prior when that band lacks enough outcomes. Bounded to
    [0.1, 1.0] — calibration can only DEFLATE an over-claim toward its real track
    record, never INFLATE beyond the model's own number (we never manufacture
    confidence the model didn't assert)."""
    bk = band_key(band_of(raw_conf))
    entry = bands.get(bk, {})
    rate = entry.get("rate")
    if rate is None:
        return NEUTRAL_PRIOR
    return max(0.1, min(1.0, rate))


# ─────────────────────────────────────────────────────────────────────────────
# 1. EVIDENCE STAMPING — surface cheap, non-LLM corroboration facts per insight
# ─────────────────────────────────────────────────────────────────────────────
def stamp_evidence(insight):
    """Attach first-class EVIDENCE the model cannot fabricate:
      - independent_cluster_count : convergence (how many independent clusters
        surfaced this pattern). memory_synthesis records it as a gate_reason string
        ('convergence N<2'); we parse it back to an int and promote it to a real
        field. Falls back to 'occurrences' if no convergence reason is present.
      - evidence{source_count, distinct_day_count} : from the insight's sources[]
        (filenames are dated 'YYYY-MM-DD_...'), a corroboration-breadth signal.
    Mutates + returns the insight dict. Idempotent."""
    # independent_cluster_count from the convergence gate reason
    conv = None
    for r in insight.get("gate_reasons", []) or []:
        if isinstance(r, str) and r.startswith("convergence"):
            # format: 'convergence N<2' or 'convergence N>=2'
            for tok in r.replace("<", " ").replace(">", " ").replace("=", " ").split():
                if tok.isdigit():
                    conv = int(tok)
                    break
        if conv is not None:
            break
    if conv is None:
        conv = int(insight.get("occurrences", 1) or 1)
    insight["independent_cluster_count"] = conv

    # evidence breadth from sources
    sources = insight.get("sources", []) or []
    days = set()
    for s in sources:
        # source filenames start 'YYYY-MM-DD_'
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            days.add(s[:10])
    insight["evidence"] = {
        "source_count": len(sources),
        "distinct_day_count": len(days),
    }
    return insight


def stamp_contradictions(insight, trusted_rules):
    """Attach contradictions[] using the CANONICAL contradiction check (no new
    logic, no drift). Only runs when there are trusted rules to compare against;
    otherwise stamps an empty list. LLM touch is inherited from the shared
    function — same cost profile as today's L4 gate. Fail-open: any error =>
    empty contradictions (never blocks calibration)."""
    if not trusted_rules:
        insight["contradictions"] = []
        return insight
    hits = []
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from contradiction_check import rules_contradict  # canonical impl
        pattern = insight.get("pattern", "")
        contradicts, reason = rules_contradict(pattern, trusted_rules)
        if contradicts:
            hits.append({"reason": reason})
    except Exception:
        pass  # fail-open: no contradiction data rather than a crash
    insight["contradictions"] = hits
    return insight


def load_trusted_rules():
    """Trusted rules to check contradictions against = the promotion state's
    trusted entries (same source contradiction_check uses). Fail-open to []."""
    state = L4 / "promotion_state.json"
    if not state.exists():
        return []
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return []
    rules = []
    for entry in (data.values() if isinstance(data, dict) else []):
        if isinstance(entry, dict) and entry.get("status") == "trusted":
            p = entry.get("pattern")
            if p:
                rules.append(p)
    return rules


# ─────────────────────────────────────────────────────────────────────────────
# 3. CALIBRATION DRIVER — stamp evidence + calibrate confidence on every insight
# ─────────────────────────────────────────────────────────────────────────────
def calibrate(dry_run=False, rebuild=False):
    if not CURRENT_INSIGHTS.exists():
        print(f"no current_insights.json at {CURRENT_INSIGHTS} — nothing to calibrate")
        return 0
    doc = json.loads(CURRENT_INSIGHTS.read_text(encoding="utf-8"))
    insights = doc.get("insights", [])
    bands = load_or_build_reliability(rebuild=rebuild)
    trusted = load_trusted_rules()

    deltas = []
    for ins in insights:
        stamp_evidence(ins)
        stamp_contradictions(ins, trusted)
        raw = ins.get("confidence_raw", ins.get("confidence", 0.0))
        mult = band_multiplier(raw, bands)
        # a live contradiction is a hard reliability hit: halve on top of the band
        if ins.get("contradictions"):
            mult *= 0.5
        calibrated = round(max(0.0, min(1.0, raw * mult)), 4)
        ins["confidence_raw"] = raw            # preserve the model's original number
        ins["calibrated_confidence"] = calibrated
        ins["calibration"] = {
            "band": band_key(band_of(raw)),
            "band_multiplier": round(mult, 4),
            "raw": raw,
        }
        # the GATE reads `confidence` — feed it the calibrated number so the whole
        # promotion pipeline inherits calibration with zero downstream changes.
        ins["confidence"] = calibrated
        deltas.append((ins.get("pattern", "")[:60], raw, calibrated, mult))

    if dry_run:
        print(f"[dry-run] {len(insights)} insight(s) — no write")
    else:
        doc["calibrated_at"] = now_iso()
        CURRENT_INSIGHTS.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        with CALIBRATION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), "count": len(insights),
                                "deltas": [{"raw": r, "cal": c, "mult": m}
                                           for _, r, c, m in deltas]},
                               ensure_ascii=False) + "\n")
        print(f"calibrated {len(insights)} insight(s) -> {CURRENT_INSIGHTS.name}")

    for patt, raw, cal, mult in deltas:
        arrow = "=" if abs(raw - cal) < 1e-9 else ("v" if cal < raw else "^")
        print(f"  [{arrow}] {raw:.2f} -> {cal:.2f} (x{mult:.2f})  {patt}")
    return 0


def report():
    bands = load_or_build_reliability(rebuild=False)
    print("confidence band reliability (from promotion_log.jsonl):")
    print(f"  (band needs >={MIN_BAND_OUTCOMES} outcomes to calibrate; else neutral prior x1.0)")
    for lo, hi in BANDS:
        bk = band_key((lo, hi))
        e = bands.get(bk, {})
        rate = e.get("rate")
        rate_s = f"{rate:.3f}" if rate is not None else "n/a (thin)"
        print(f"  {bk}:  hits={e.get('hits',0):>4}  total={e.get('total',0):>4}  rate={rate_s}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="P1 #1 evidence + confidence calibration")
    ap.add_argument("--dry-run", action="store_true", help="show deltas, write nothing")
    ap.add_argument("--report", action="store_true", help="print band reliability table")
    ap.add_argument("--rebuild-ledger", action="store_true", help="force-rebuild reliability from the log")
    args = ap.parse_args()
    if args.report:
        return report()
    return calibrate(dry_run=args.dry_run, rebuild=args.rebuild_ledger)


if __name__ == "__main__":
    sys.exit(main())
