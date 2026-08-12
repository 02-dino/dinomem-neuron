#!/usr/bin/env python3
"""entity_resolver.py — P1 entity resolution for the dinomem-neuron memory graph.

PROBLEM (measured on the live graph, 2026-08-12):
  The same entity appears under many SURFACE FORMS across memory/*.md — `project advancer`
  vs `` `project advancer` ``, `memory/_note_*.md` vs `` `memory/_note_*.md` ``, `neuron` vs
  `dinomem-neuron`. Both extract_entity_nodes() and extract_relation_edges() key on raw
  lowercased text, so these fragment into SEPARATE nodes/edge-endpoints. That splits what
  should be one entity's neighbourhood across N nodes -> weaker traversal, worse recall.

DESIGN (deterministic / T1 / $0 — NO LLM, NO embed, NO network):
  Two conservative tiers, each reversible and same-type-guarded to avoid false merges:
    TIER 1 (cosmetic, zero-risk): NORMALIZE surface form — strip backticks/quotes, collapse
            whitespace, drop trailing glob wildcards (`_note_*.md` -> `_note_.md`), lowercase.
            Two forms with the SAME normal key are unconditionally the same entity.
    TIER 2 (semantic alias, guarded): after normalization, merge A into B when they share a
            type AND (a) one normal key is a token-subset of the other with SequenceMatcher
            ratio >= SUBSET_RATIO, or (b) full-string ratio >= FULL_RATIO. Never cross-type.

  Canonical form = the surface form with the MOST mentions in its merge cluster (ties -> the
  shortest, then lexicographically first — stable/deterministic). All others become aliases[].

HARMONY: this is a pure helper. It does not duplicate neuron's existing dedup (that keys on
  exact lowercase); it ADDS the normalization+alias layer on top and is import-safe (fail-open:
  a caller that can't import it still works with raw surface forms).

Public API:
  normalize_surface(s)            -> str        # TIER-1 normal key
  resolve_entities(entities)      -> list[dict] # cluster [{name,type,mentions}] -> canonical+aliases
  build_alias_map(resolved)       -> dict       # surface_lower -> canonical name (for edge canonicalization)
  canonicalize(term, alias_map)   -> str        # map a raw relation subject/object to its canonical
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Tunables (conservative by default — favour PRECISION over merge recall; a missed merge is
# cheap, a wrong merge corrupts the graph). Overridable by callers/tests if ever needed.
SUBSET_RATIO = 0.90   # token-subset clusters need very high similarity
FULL_RATIO = 0.94     # full-string near-duplicates (typo/spacing) need even higher
_MERGE_MIN_LEN = 4    # don't alias-merge ultra-short tokens (too collision-prone)

_BACKTICK_QUOTE = re.compile(r"[`'\"]+")
_GLOB = re.compile(r"\*+")
_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s.,;:]+$")


def normalize_surface(s: str) -> str:
    """TIER-1 normal key: lowercase, strip backticks/quotes, collapse whitespace, drop glob
    wildcards and trailing punctuation. Deterministic and idempotent."""
    if not s:
        return ""
    t = s.strip().lower()
    t = _BACKTICK_QUOTE.sub("", t)
    t = _GLOB.sub("", t)            # `_note_*.md` -> `_note_.md`
    t = _WS.sub(" ", t)
    t = _TRAILING_PUNCT.sub("", t)
    return t.strip()


def _tokens(norm: str) -> set:
    return set(t for t in re.split(r"[\s/_\-.]+", norm) if t)


def _is_subset_alias(a_norm: str, b_norm: str) -> bool:
    """True if a_norm and b_norm are the same entity via token-subset + high similarity.
    Symmetric-safe (caller passes both orders is unnecessary; we test both here)."""
    if a_norm == b_norm:
        return True
    if len(a_norm) < _MERGE_MIN_LEN or len(b_norm) < _MERGE_MIN_LEN:
        return False
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if ta and tb and (ta <= tb or tb <= ta):
        # one is a token-subset of the other (e.g. {neuron} <= {dinomem, neuron})
        return SequenceMatcher(None, a_norm, b_norm).ratio() >= SUBSET_RATIO
    return SequenceMatcher(None, a_norm, b_norm).ratio() >= FULL_RATIO


def resolve_entities(entities: list[dict]) -> list[dict]:
    """Cluster surface-form entities into canonical entities.

    Input:  [{"name": str, "type": str, "mentions": int}, ...]  (extract_entity_nodes output)
    Output: [{"name": canonical, "type": str, "mentions": summed,
              "aliases": [other surface names...], "surface_forms": [all incl canonical]}, ...]
            sorted by mentions desc. Backward-compatible superset (name/type/mentions preserved).
    """
    # Group by type first — NEVER merge across types.
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e.get("type", "?"), []).append(dict(e))

    resolved: list[dict] = []
    for etype, ents in by_type.items():
        # Precompute normal keys.
        for e in ents:
            e["_norm"] = normalize_surface(e["name"])
        # Union-find over ents within this type.
        parent = list(range(len(ents)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        n = len(ents)
        for i in range(n):
            for j in range(i + 1, n):
                if _is_subset_alias(ents[i]["_norm"], ents[j]["_norm"]):
                    union(i, j)

        clusters: dict[int, list[int]] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(i)

        for members in clusters.values():
            forms = [ents[i] for i in members]
            # Canonical = most mentions; tie -> shortest name -> lexicographically first.
            canon = sorted(
                forms,
                key=lambda e: (-e.get("mentions", 0), len(e["name"]), e["name"]),
            )[0]
            aliases = [f["name"] for f in forms if f["name"] != canon["name"]]
            resolved.append({
                "name": canon["name"],
                "type": etype,
                "mentions": sum(f.get("mentions", 0) for f in forms),
                "aliases": sorted(set(aliases)),
                "surface_forms": sorted(set(f["name"] for f in forms)),
            })

    # Deterministic total order: mentions desc, then name asc (stable regardless of input order).
    return sorted(resolved, key=lambda x: (-x["mentions"], x["name"]))


def build_alias_map(resolved: list[dict]) -> dict:
    """surface_lower -> canonical name, for canonicalizing relation subject/object endpoints.
    Includes both normalized keys and raw-lower keys so lookups hit regardless of caller form."""
    amap: dict[str, str] = {}
    for ent in resolved:
        canon = ent["name"]
        for sf in ent.get("surface_forms", [ent["name"]]):
            amap[sf.lower()] = canon
            amap[normalize_surface(sf)] = canon
    return amap


def canonicalize(term: str, alias_map: dict) -> str:
    """Map a raw relation subject/object term to its canonical surface form (or return term
    unchanged if unknown). Tries raw-lower then normalized key. Fail-open."""
    if not term or not alias_map:
        return term
    low = term.strip().lower()
    if low in alias_map:
        return alias_map[low]
    nk = normalize_surface(term)
    if nk in alias_map:
        return alias_map[nk]
    return term


if __name__ == "__main__":
    # Tiny self-demo (not the test suite; see entity_resolver_test.py).
    demo = [
        {"name": "project advancer", "type": "project", "mentions": 273},
        {"name": "`project advancer`", "type": "project", "mentions": 1},
        {"name": "dinomem-neuron", "type": "project", "mentions": 40},
        {"name": "neuron", "type": "project", "mentions": 12},
        {"name": "Dino", "type": "person", "mentions": 30},
        {"name": "Dino", "type": "tool", "mentions": 2},  # same name, DIFFERENT type -> NOT merged
    ]
    import json
    print(json.dumps(resolve_entities(demo), indent=2, ensure_ascii=False))
