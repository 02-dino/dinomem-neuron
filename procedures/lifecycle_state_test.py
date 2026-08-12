#!/usr/bin/env python3
"""
lifecycle_state_test.py — proves the unified lifecycle VIEW maps both existing
machines (valid_time facts + promotion insights) onto the canonical vocabulary,
in a throwaway temp corpus (touches no real memory). No pytest dep.
Run: python3 lifecycle_state_test.py   (exit 0 = all pass)
"""
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
        mem = ws / "memory"
        hist = mem / "_history"
        promo_dir = ws / "kb/memory_neuron/l4_promoted"
        mem.mkdir(parents=True)
        hist.mkdir(parents=True)
        promo_dir.mkdir(parents=True)

        # current fact (open interval)
        (mem / "_pin_loc_current.md").write_text(
            "# loc\nsubject: dino.loc\nvalid_from: 2026-01-01\n\nLives in Bali.\n", encoding="utf-8")
        # superseded fact (closed interval + back-pointer)
        (mem / "_pin_loc_old.md").write_text(
            "# loc old\nsubject: dino.loc\nvalid_from: 2024-01-01\nvalid_until: 2026-01-01\n"
            "superseded_by: memory/_pin_loc_current.md\n\nLived in Jakarta.\n", encoding="utf-8")
        # archived (physically in _history/)
        (hist / "_pin_loc_ancient.md").write_text(
            "# loc ancient\nsubject: dino.loc\nvalid_from: 2020-01-01\nvalid_until: 2024-01-01\n\nLived in Bandung.\n",
            encoding="utf-8")
        # plain note, tracked as a TRUSTED insight in promotion state
        (mem / "_note_pref_x.md").write_text("# pref\n\nreports as bullets.\n", encoding="utf-8")
        # plain note, no signal anywhere -> unknown
        (mem / "_note_plain.md").write_text("# plain\n\nnothing.\n", encoding="utf-8")

        # promotion_state: one trusted insight whose sources include _note_pref_x.md
        promo = {
            "pref-bullets": {"status": "trusted", "reinforce_count": 5,
                             "sources": ["_note_pref_x.md"], "pattern": "reports as bullets"},
        }
        (promo_dir / "promotion_state.json").write_text(json.dumps(promo), encoding="utf-8")

        os.environ["DINOMEM_WORKSPACE"] = str(ws)
        import importlib
        import lifecycle_state as ls
        importlib.reload(ls)

        def st(name):
            return ls.state_of_file(mem / name if (mem / name).exists() else hist / name)["state"]

        checks = {
            "current fact":     (st("_pin_loc_current.md"), "current"),
            "superseded fact":  (st("_pin_loc_old.md"), "superseded"),
            "archived fact":    (ls.state_of_file(hist / "_pin_loc_ancient.md")["state"], "archived"),
            "trusted insight":  (st("_note_pref_x.md"), "trusted"),
            "unknown plain":    (st("_note_plain.md"), "unknown"),
        }
        ok = True
        for label, (got, want) in checks.items():
            good = got == want
            ok = ok and good
            print("  %s %-18s got=%s want=%s" % ("PASS" if good else "FAIL", label, got, want))

        s = ls.summary()
        assert s["counts"]["current"] == 1, s["counts"]
        assert s["counts"]["superseded"] == 1, s["counts"]
        assert s["counts"]["archived"] == 1, s["counts"]
        assert s["counts"]["trusted"] == 1, s["counts"]
        assert s["counts"]["unknown"] == 1, s["counts"]
        print("  PASS summary counts (1 each: current/superseded/archived/trusted/unknown)")

        if not ok:
            print("\nFAIL")
            return 1
    print("\nALL PASS — unified view maps valid_time + promotion onto one vocabulary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
