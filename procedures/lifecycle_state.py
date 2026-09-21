#!/usr/bin/env python3
"""
lifecycle_state.py — P1 #2 (UPGRADE, not rebuild): a thin, READ-ONLY unified view
over the TWO lifecycle machines dinomem already has, under one vocabulary.

Why a VIEW, not a new FSM
-------------------------
dinomem already runs two working lifecycles:
  * memory_promote.py  — INSIGHT/behavior-rule FSM:
        promotable -> provisional -> (3 reinforcing runs) -> trusted -> demoted/expired
        (probation, reinforce-count, category TTLs, decay, LLM stale-rescue,
         contradiction-blocked transitions, full audit log in promotion_log.jsonl)
  * valid_time.py      — FACT temporal lifecycle:
        current -> close_interval(valid_until) -> superseded -> retire() -> memory/_history/
Building a fresh proposal FSM would DUPLICATE both and risk regressing them. Instead
this module gives observability (and P2 #4 "why is this here?") a SINGLE place to ask
"what lifecycle state is memory X in, and why" — mapping both machines onto one
canonical vocabulary WITHOUT owning any state itself.

Canonical vocabulary (union of both machines)
---------------------------------------------
  provisional  insight on probation (promote.py status=provisional)
  trusted      insight graduated to behavior (promote.py status=trusted)
  current      fact with an open valid interval, not superseded (valid_time.is_current)
  stale        past its TTL / not reinforced (promote.py decay window) — soft, pre-demote
  superseded   fact closed by a newer same-subject fact (valid_until set / superseded_by)
  archived     retired to memory/_history/ — non-destructive, still indexable
  unknown      no lifecycle signal (a plain note/pin with no valid-time + not in promo state)

This module READS ONLY. It never transitions, deletes, or writes state. Deterministic,
no LLM. Fail-open: any unreadable source contributes nothing rather than crashing.

Usage:
  python3 lifecycle_state.py --file memory/_pin_x.md   # state+why for one file
  python3 lifecycle_state.py --summary                 # counts per state across the store
  python3 lifecycle_state.py --json                    # machine-readable
"""

import argparse
import json
import os
import sys
from pathlib import Path

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WS = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEM = WS / "memory"
HISTORY = MEM / "_history"
PROMO_STATE = WS / "kb/memory_neuron/l4_promoted/promotion_state.json"

CANON = ("provisional", "trusted", "current", "stale",
         "superseded", "archived", "unknown")


def _load_valid_time():
    """Import valid_time from procedures/ (sibling). None if unavailable (fail-open)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import valid_time as vt
        return vt
    except Exception:
        return None


def _load_promo_state():
    """Return {pattern_or_key: entry} from promotion_state.json, or {} (fail-open)."""
    if not PROMO_STATE.exists():
        return {}
    try:
        data = json.loads(PROMO_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def state_of_file(path: Path, vt=None, promo=None) -> dict:
    """Resolve the canonical lifecycle state + a human 'why' for ONE memory file.
    Precedence (most specific wins):
      archived (in _history/) > superseded (valid_until set) > current (open interval)
      > unknown. Insight promo-state (provisional/trusted/stale) is layered from the
      promotion store when the file/pattern is tracked there.
    """
    vt = vt or _load_valid_time()
    promo = promo if promo is not None else _load_promo_state()
    p = Path(path)
    rel = str(p.relative_to(WS)) if str(p).startswith(str(WS)) else str(p)

    # 1) archived — physically in _history/ (the non-destructive retire target)
    try:
        if HISTORY in p.resolve().parents:
            return {"file": rel, "state": "archived",
                    "why": "retired to memory/_history/ (non-destructive; still indexable)"}
    except Exception:
        pass

    # 2/3) fact temporal state from valid_time frontmatter
    if vt is not None and p.exists():
        try:
            f = vt.read_fields(p)
            if f.get("valid_until") or f.get("superseded_by"):
                by = f.get("superseded_by")
                why = "closed interval (valid_until=%s)" % f.get("valid_until")
                if by:
                    why += " superseded_by %s" % by
                return {"file": rel, "state": "superseded", "why": why}
            if f.get("valid_from") is not None and vt.is_current(f):
                base = {"file": rel, "state": "current",
                        "why": "open valid interval (valid_from=%s), not superseded" % f.get("valid_from")}
                _layer_promo(base, rel, promo)
                return base
        except Exception:
            pass

    # 4) plain note/pin: fall back to promo-state if tracked, else unknown
    base = {"file": rel, "state": "unknown",
            "why": "no valid-time fields; not in promotion state"}
    _layer_promo(base, rel, promo)
    return base


def _layer_promo(base: dict, rel: str, promo: dict):
    """If this file/pattern is tracked in the promotion store, layer the INSIGHT
    lifecycle status (provisional/trusted). Matches by source filename in an
    entry's sources[]/rule_line.

    Precedence rule: a RESOLVED temporal state (current/superseded/archived) is
    PRIMARY and must not be clobbered — a memory can be BOTH a current fact and a
    trusted insight, so the insight status is exposed as an OVERLAY field
    (promo_status/insight_key). Only an unresolved 'unknown' state is upgraded to
    the insight status directly (a plain note tracked purely as an insight)."""
    for key, entry in promo.items():
        if not isinstance(entry, dict):
            continue
        srcs = entry.get("sources", []) or []
        name = Path(rel).name
        if name in srcs or name in (entry.get("rule_line") or "") or key == name:
            st = entry.get("status")
            if st in ("provisional", "trusted"):
                base["insight_key"] = key
                base["promo_status"] = st
                base["reinforce_count"] = entry.get("reinforce_count", 0)
                if base.get("state") == "unknown":
                    # plain note tracked only as an insight -> insight status IS its state
                    base["state"] = st
                    base["why"] = ("insight %s (reinforce_count=%s)"
                                   % (st, entry.get("reinforce_count", 0)))
                else:
                    # live fact that also backs an insight -> keep temporal state, note overlay
                    base["why"] = base.get("why", "") + " | also insight %s" % st
            return


def summary(vt=None, promo=None) -> dict:
    """Count files per canonical state across memory/ + _history/. Read-only."""
    vt = vt or _load_valid_time()
    promo = promo if promo is not None else _load_promo_state()
    counts = {s: 0 for s in CANON}
    files = []
    if MEM.exists():
        files += sorted(MEM.glob("_pin_*.md")) + sorted(MEM.glob("_note_*.md"))
    if HISTORY.exists():
        files += sorted(HISTORY.glob("*.md"))
    per = []
    for p in files:
        r = state_of_file(p, vt=vt, promo=promo)
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        per.append(r)
    return {"counts": counts, "total": len(files), "files": per}


def main():
    ap = argparse.ArgumentParser(description="P1 #2 unified lifecycle VIEW (read-only)")
    ap.add_argument("--file", help="report state+why for one memory file")
    ap.add_argument("--summary", action="store_true", help="counts per state across the store")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.file:
        r = state_of_file(WS / args.file if not os.path.isabs(args.file) else Path(args.file))
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print("%-12s %s\n  why: %s" % (r["state"].upper(), r["file"], r["why"]))
        return 0

    s = summary()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print("lifecycle state summary (%d files across memory/ + _history/):" % s["total"])
        for st in CANON:
            print("  %-12s %d" % (st, s["counts"].get(st, 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
