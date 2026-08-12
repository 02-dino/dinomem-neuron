#!/usr/bin/env python3
"""entity_resolver_test.py — offline/deterministic/zero-LLM regression for P1 entity resolution.
Run: python3 procedures/entity_resolver_test.py   (exit 0 = all pass)
Mirrors the fact_query_test.py convention: no network/embed/LLM, pure logic assertions.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from entity_resolver import (  # noqa: E402
    normalize_surface, resolve_entities, build_alias_map, canonicalize,
)

_fail = 0
def check(name, cond):
    global _fail
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1

# ── TIER 1: normalization ──────────────────────────────────────────────────
check("T1 strips backticks", normalize_surface("`project advancer`") == "project advancer")
check("T1 drops glob wildcard", normalize_surface("memory/_note_*.md") == "memory/_note_.md")
check("T1 collapses whitespace", normalize_surface("  a   b  ") == "a b")
check("T1 strips quotes", normalize_surface('"verified: true"') == "verified: true")
check("T1 idempotent", normalize_surface(normalize_surface("`X *Y*`")) == normalize_surface("`X *Y*`"))
check("T1 empty-safe", normalize_surface("") == "")

# ── TIER 1 merge: cosmetic variants collapse ───────────────────────────────
r = resolve_entities([
    {"name": "project advancer", "type": "project", "mentions": 273},
    {"name": "`project advancer`", "type": "project", "mentions": 1},
])
check("cosmetic variants -> 1 canonical", len(r) == 1)
check("canonical = higher-mention form", r[0]["name"] == "project advancer")
check("alias captured", "`project advancer`" in r[0]["aliases"])
check("mentions summed", r[0]["mentions"] == 274)

# ── CROSS-TYPE guard: same name, different type -> NEVER merge ──────────────
r = resolve_entities([
    {"name": "Dino", "type": "person", "mentions": 30},
    {"name": "Dino", "type": "tool", "mentions": 2},
])
check("cross-type same-name NOT merged", len(r) == 2)

# ── TIER 2 subset: high-similarity token-subset merges (same type) ─────────
r = resolve_entities([
    {"name": "check_projects.sh", "type": "tool", "mentions": 25},
    {"name": "`check_projects.sh`", "type": "tool", "mentions": 9},
])
check("backtick tool variant merges", len(r) == 1 and r[0]["mentions"] == 34)

# ── TIER 2 conservatism: loosely-related short tokens DON'T over-merge ──────
r = resolve_entities([
    {"name": "neuron", "type": "project", "mentions": 12},
    {"name": "dinomem-neuron", "type": "project", "mentions": 40},
])
check("loose subset below ratio stays separate (precision)", len(r) == 2)

# ── alias map + canonicalize ───────────────────────────────────────────────
r = resolve_entities([
    {"name": "Daily Note Review", "type": "project", "mentions": 17},
    {"name": "`daily note review`", "type": "project", "mentions": 1},
])
amap = build_alias_map(r)
check("build_alias_map maps variant -> canonical",
      canonicalize("`daily note review`", amap) == "Daily Note Review")
check("canonicalize passes through unknown term",
      canonicalize("totally unknown entity", amap) == "totally unknown entity")
check("canonicalize normalized-key hit",
      canonicalize("daily note review", amap) == "Daily Note Review")

# ── determinism: same input -> same output order ───────────────────────────
inp = [
    {"name": "B", "type": "tool", "mentions": 5},
    {"name": "A", "type": "tool", "mentions": 5},
]
check("deterministic tie-break (shortest/lexis stable)",
      resolve_entities(inp) == resolve_entities(list(reversed(inp))))

print()
if _fail:
    print(f"{_fail} FAILURE(S)")
    sys.exit(1)
print("ALL PASS")
