#!/usr/bin/env python3
"""
valid_time_test.py — regression suite for the bitemporal valid-time layer (ships
WITH the feature). Consolidates every step 1-5 check into ONE offline test.

Runs OFFLINE / deterministic: pure stdlib date logic, NO LLM, NO embedding
backend, NO network. Uses tempfile dirs (auto-cleaned), never touches real memory.

Covers:
  V1  valid_time core (18-case self-test: parse, as_of, supersede, retire, backcompat, idempotent).
  V2  dedup supersession branch: same-subject+newer+high-sim -> suggestion=supersede;
      different-subject / no-subject-key -> NOT supersede. (logic-level, embed mocked)
  V3  hybrid_recall as_of filter: past date -> retired fact; current date -> current
      fact; non-memory candidates always pass (fail-open); no --as-of -> all pass.
  V4  contradiction synergy: same-subject disjoint intervals -> supersession (skip LLM);
      overlapping -> None (reach LLM); different-subject / fieldless -> None (no change).

Exit 0 = all pass, 1 = any fail. No real archives / config / model required.
"""
import sys
import tempfile
import shutil
import importlib.util
from pathlib import Path

_PROC = Path(__file__).resolve().parent
_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}")
    return bool(cond)


def _load(mod_name):
    """Import a sibling procedure module by file path (avoids package issues)."""
    spec = importlib.util.spec_from_file_location(mod_name, _PROC / f"{mod_name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = m
    spec.loader.exec_module(m)
    return m


# ── V1: valid_time core self-test ───────────────────────────────────────────
def test_core():
    print("V1 valid_time core (self-test):")
    vt = _load("valid_time")
    rc = vt._self_test()  # prints its own 18 PASS lines + returns 0/1
    check("V1 core self-test exit 0", rc == 0)


# ── V2: dedup supersession branch (logic-level, no embed backend) ────────────
# We test the SUPERSESSION DECISION logic directly using valid_time's field
# helpers + the same rule the dedup gate applies, so no embedding backend is
# needed. (The gate's own embed path is exercised in integration, not here.)
def _supersede_decision(vt, cand_body, target_path):
    """Mirror of the dedup gate's supersession rule (deterministic part only):
    same explicit subject on both sides + candidate is newer + target current."""
    cand = vt.fields_from_text(cand_body)
    cs = cand.get("subject")
    if not cs:
        return False
    tgt = vt.read_fields(target_path)
    ts = tgt.get("subject")
    if not ts or ts != cs:
        return False
    cvf = vt._parse_date(cand.get("valid_from"))
    tvf = vt._parse_date(tgt.get("valid_from"))
    newer = (cvf is not None and tvf is not None and cvf >= tvf) \
        or (cvf is None and tvf is not None) or (cvf is not None and tvf is None)
    return newer and vt.is_current(tgt)


def test_dedup_supersession():
    print("V2 dedup supersession decision:")
    vt = _load("valid_time")
    tmp = Path(tempfile.mkdtemp(prefix="vt_v2_"))
    try:
        jak = tmp / "_pin_jakarta.md"
        jak.write_text("# Jakarta\nsubject: dino.location\nvalid_from: 2024-01-01\n\nx\n")
        check("V2 same-subject newer -> supersede",
              _supersede_decision(vt, "subject: dino.location\nvalid_from: 2026-05-01\n\nBali", jak) is True)
        check("V2 different-subject -> NOT supersede",
              _supersede_decision(vt, "subject: dino.food\nvalid_from: 2026-05-01\n\nrendang", jak) is False)
        check("V2 no-subject-key -> NOT supersede",
              _supersede_decision(vt, "valid_from: 2026-05-01\n\nBali", jak) is False)
        # already-closed target is not current -> no supersede
        clo = tmp / "_pin_closed.md"
        clo.write_text("# Closed\nsubject: dino.location\nvalid_from: 2024-01-01\nvalid_until: 2025-01-01\n\nx\n")
        check("V2 closed target -> NOT supersede",
              _supersede_decision(vt, "subject: dino.location\nvalid_from: 2026-05-01\n\nBali", clo) is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── V3: hybrid_recall as_of filter ───────────────────────────────────────────
def test_as_of_filter():
    print("V3 hybrid_recall as_of filter:")
    # load hybrid_recall from tools/ (sibling of procedures/)
    tools_dir = _PROC.parent / "tools"
    spec = importlib.util.spec_from_file_location("hybrid_recall", tools_dir / "hybrid_recall.py")
    hr = importlib.util.module_from_spec(spec)
    sys.modules["hybrid_recall"] = hr
    try:
        spec.loader.exec_module(hr)
    except Exception as e:
        check(f"V3 hybrid_recall import ({e})", False)
        return
    tmp = Path(tempfile.mkdtemp(prefix="vt_v3_"))
    try:
        (tmp / "memory").mkdir()
        (tmp / "memory" / "_history").mkdir()
        (tmp / "procedures").mkdir()
        shutil.copy(_PROC / "valid_time.py", tmp / "procedures" / "valid_time.py")
        (tmp / "memory" / "_history" / "_pin_jakarta.md").write_text(
            "# Jakarta\nsubject: dino.location\nvalid_from: 2024-01-01\nvalid_until: 2026-05-01\n\nx\n")
        (tmp / "memory" / "_pin_bali.md").write_text(
            "# Bali\nsubject: dino.location\nvalid_from: 2026-05-01\n\nx\n")
        hr.WS = tmp
        cands = [
            {"leg": "memory_search", "content": "Jakarta", "score": 0.9, "uri": "memory/_history/_pin_jakarta.md"},
            {"leg": "memory_search", "content": "Bali", "score": 0.9, "uri": "memory/_pin_bali.md"},
            {"leg": "docs_search", "content": "doc", "score": 0.5, "uri": "Some Doc#Section"},
        ]
        def names(cs):
            return {c["uri"].split("/")[-1] for c in cs}
        none = names(hr.apply_as_of(cands, None))
        mar = names(hr.apply_as_of(cands, "2026-03-01"))
        aug = names(hr.apply_as_of(cands, "2026-08-01"))
        check("V3 no as_of -> all pass", len(none) == 3)
        check("V3 past date -> retired Jakarta kept, Bali dropped",
              "_pin_jakarta.md" in mar and "_pin_bali.md" not in mar)
        check("V3 current date -> Bali kept, Jakarta dropped",
              "_pin_bali.md" in aug and "_pin_jakarta.md" not in aug)
        check("V3 non-memory candidate always passes (fail-open)",
              "Some Doc#Section" in mar and "Some Doc#Section" in aug)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── V4: contradiction synergy ────────────────────────────────────────────────
def test_contradiction_synergy():
    print("V4 contradiction valid-time synergy:")
    cc = _load("contradiction_check")
    r = cc._valid_time_reconciles
    check("V4 disjoint same-subject -> supersession",
          r({"subject": "dino.location", "valid_from": "2024-01-01", "valid_until": "2026-05-01"},
            {"subject": "dino.location", "valid_from": "2026-05-01"}) == "supersession")
    check("V4 overlapping same-subject -> None (real contradiction)",
          r({"subject": "dino.location", "valid_from": "2024-01-01", "valid_until": "2027-01-01"},
            {"subject": "dino.location", "valid_from": "2026-05-01"}) is None)
    check("V4 different-subject -> None",
          r({"subject": "dino.location", "valid_from": "2024-01-01", "valid_until": "2026-05-01"},
            {"subject": "dino.food", "valid_from": "2026-05-01"}) is None)
    check("V4 fieldless insights -> None (zero behavior change)",
          r({"pattern": "BTC bullish"}, {"pattern": "BTC bearish"}) is None)


def main():
    test_core()
    test_dedup_supersession()
    test_as_of_filter()
    test_contradiction_synergy()
    print(f"\nVALID-TIME SUITE: {_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
