#!/usr/bin/env python3
"""
memory_dedup_check.py — pre-write dedup gate for auto-pin / auto-note.

WHY
  Auto-pin / auto-note is LLM-driven (the AGENTS.md `memory_pin` policy tells the
  agent to write memory/_pin_<slug>.md or memory/_note_<slug>.md). It is
  fire-and-forget: nothing checks whether an existing pin/note already covers the
  same TOPIC before a new file is created. Slug-matching alone is useless —
  different slugs can describe the same thing (e.g. `notify-script-accountid-bug`
  vs `buyer-delivery-dispatch`). Result over time: pin sprawl and, worse,
  CONTRADICTORY duplicates (a fresh pin that disagrees with an old one).

WHAT
  This is an ADVISORY pre-write gate. Given a candidate pin/note (title + body,
  or --file), it embeds the candidate and every existing _pin_/_note_ in the
  agent's own memory/ dir, computes cosine similarity, and reports the closest
  match. The agent reads the JSON and decides: MERGE into the existing file,
  SKIP (already covered), or CREATE NEW (genuinely novel). This script never
  writes or blocks anything — it only informs the decision.

GENERALIZED (neuron-shipped, not analyst-specific)
  * Workspace comes from DINOMEM_WORKSPACE (sed-substituted at install), with a
    ZIP/skill-dir fallback — identical convention to memory_promote.py. NO
    operator-specific path is baked in; the gate always reads the running
    agent's own workspace.
  * Embeddings come from the neuron's single-source backend tools/_embed.py
    (multilingual e5, e5 query:/passage: prefixing, model-id stamping). No
    reinvented embedding, no second model.
  * GRACEFUL DEGRADE: if the embedding stack isn't installed on this buyer's box
    (semantic_available()==False), the script exits 0 with
    {"match": false, "skipped": "no-embed-backend"} so a memory write is NEVER
    blocked just because dedup can't run.

EXIT CODE
  Always 0 on a completed check (advisory — must not break a memory write).
  Non-zero only on a usage error (bad args).

USAGE
  # candidate from title + body:
  python3 memory_dedup_check.py --kind pin --title "..." --body "..."
  # candidate from an existing draft file:
  python3 memory_dedup_check.py --kind note --file /path/to/draft.md
  # tune threshold / scope:
  python3 memory_dedup_check.py --kind pin --title T --body B --threshold 0.80 --scope pin
  # list what would be compared, no candidate:
  python3 memory_dedup_check.py --list

OUTPUT (JSON on stdout)
  {"match": true, "file": "memory/_pin_x.md", "score": 0.83, "title": "...",
   "suggestion": "merge", "candidates": [{file, score, title}, ...]}
  {"match": false, "candidates": [...top few...]}
  {"match": false, "skipped": "no-embed-backend"}
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── Workspace resolution (same convention as memory_promote.py) ──────────────
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run (ran from ZIP/skill dir)
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
BASE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEMORY_DIR = BASE / "memory"

# ── Import the embedding backend (backend-agnostic: ONE file, two homes) ─────
# tools/ has no __init__.py, so `tools._embed` won't import; we add both repo
# root and tools/ to the path and import bare module names.
# BACKEND ORDER:
#   1. neuron install  -> tools/_embed.py (local HuggingFaceEmbeddings, get_embeddings()).
#   2. live-analyst    -> tools/tei_embeddings.py (TEIEmbeddings, HTTP e5 on :8080).
#   3. neither present -> graceful skip (never blocks a memory write).
# Both backends expose the SAME embed_documents(list)/embed_query(str) interface
# and the SAME e5 model, so the scoring below is identical regardless of which
# one is active. This keeps a single source of truth for the dedup logic.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))              # repo root
sys.path.insert(0, str(_REPO_ROOT / "tools"))    # so bare `import _embed` resolves


class SemanticUnavailable(RuntimeError):
    """Raised/returned when no embedding backend is importable."""


_get_embeddings_impl = None
_backend_name = None


def _load_backend():
    """Resolve an embeddings provider from whichever backend this workspace has.
    Returns a zero-arg callable that yields an object with
    embed_documents(list)/embed_query(str), or None if none available."""
    global _get_embeddings_impl, _backend_name
    if _get_embeddings_impl is not None:
        return _get_embeddings_impl
    # 1) neuron backend
    try:
        from _embed import get_embeddings as _ge  # type: ignore
        _get_embeddings_impl, _backend_name = _ge, "_embed"
        return _get_embeddings_impl
    except Exception:
        pass
    # 2) live-analyst TEI backend
    try:
        from tei_embeddings import TEIEmbeddings  # type: ignore
        _base = os.environ.get("DINOMEM_EMBED_URL", "http://localhost:8080/v1")

        def _ge_tei():
            return TEIEmbeddings(base_url=_base, api_key=os.environ.get("DINOMEM_EMBED_KEY", "dummy"))

        _get_embeddings_impl, _backend_name = _ge_tei, "tei_embeddings"
        return _get_embeddings_impl
    except Exception:
        return None


def semantic_available():
    """True if any embedding backend is importable (no model load / no HTTP call)."""
    return _load_backend() is not None


def get_embeddings():
    impl = _load_backend()
    if impl is None:
        raise SemanticUnavailable("no embed backend (_embed or tei_embeddings)")
    return impl()

# CALIBRATION NOTE (measured on e5-small, 2026-07-11):
# Absolute cosine thresholds DO NOT WORK for e5-small. Its embeddings are highly
# anisotropic — everything clusters in a narrow high band:
#   unrelated candidate (coffee vs code pins):   max ~0.82
#   true duplicate (accountId pin vs its cluster): max ~0.87
#   different-but-related real pins:               up to ~0.96
# The true-dup signal is only ~0.05 above the unrelated floor, and different
# real topics can score HIGHER than a true dup. So a single fixed floor either
# flags everything (0.78) or misses dups (0.88) depending on corpus.
#
# FIX: score RELATIVE to the corpus's own baseline. We compute the mean+std of
# candidate-vs-all-pins similarity, and flag a hit only when the top match is a
# statistical OUTLIER above that baseline (z-score) AND clears a soft absolute
# floor. This adapts per-corpus and per-buyer automatically — no magic number
# that only works on our workspace.
# Measured separators (e5-small, our 16-pin corpus):
#   unrelated (coffee):  best abs 0.830, z 1.67
#   true duplicate:      best abs 0.867, z 2.41
# So floor 0.85 + z>=2.0 cleanly passes the dup and rejects the unrelated one,
# with margin on both. Both are env/flag tunable per buyer/corpus.
DEFAULT_Z = float(os.environ.get("DINOMEM_DEDUP_Z", "2.0"))           # outlier strength
ABS_FLOOR = float(os.environ.get("DINOMEM_DEDUP_ABS_FLOOR", "0.85"))  # soft sanity floor
# Legacy absolute threshold still honored if caller passes --threshold explicitly.
DEFAULT_THRESHOLD = os.environ.get("DINOMEM_DEDUP_THRESHOLD")  # None unless set

# How much of each file to embed. Title carries most topic signal; a bounded
# body slice keeps embedding cheap and avoids one huge pin dominating.
_BODY_CHARS = 1200


def _read_title_body(path: Path):
    """Extract a (title, body) pair from a _pin_/_note_ markdown file.
    Title = first markdown H1 if present, else the filename slug. Body = the
    rest, frontmatter-ish lines kept (they carry topic words too)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return path.stem, ""
    title = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            break
    if not title:
        title = path.stem.replace("_pin_", "").replace("_note_", "").replace("-", " ")
    return title, text


def _embed_text(title: str, body: str) -> str:
    """Compose the string we embed for a memory item. Title is weighted by
    repetition (it's the strongest topic signal), followed by a bounded body."""
    body = (body or "").strip()
    if len(body) > _BODY_CHARS:
        body = body[:_BODY_CHARS]
    return f"{title}\n{title}\n{body}".strip()


def _cosine(a, b):
    # Vectors from _embed are normalized (normalize_embeddings=True), so a plain
    # dot product IS cosine similarity. Keep a manual norm as belt-and-suspenders.
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _collect_targets(scope: str):
    """List existing memory files to compare against, honoring scope."""
    if not MEMORY_DIR.exists():
        return []
    globs = []
    if scope in ("all", "pin"):
        globs.append("_pin_*.md")
    if scope in ("all", "note"):
        globs.append("_note_*.md")
    files = []
    for g in globs:
        files.extend(sorted(MEMORY_DIR.glob(g)))
    return files


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(BASE))
    except ValueError:
        return str(path)


def main():
    ap = argparse.ArgumentParser(description="Pre-write dedup gate for auto-pin/auto-note.")
    ap.add_argument("--kind", choices=["pin", "note"], default="pin",
                    help="What the candidate is (informs default scope).")
    ap.add_argument("--title", default="", help="Candidate title.")
    ap.add_argument("--body", default="", help="Candidate body text.")
    ap.add_argument("--file", default="", help="Read candidate title+body from this file instead.")
    ap.add_argument("--threshold", type=float, default=None,
                    help="ABSOLUTE cosine floor override. Omit to use adaptive "
                         "z-score scoring (recommended; absolute floors don't "
                         "generalize across e5 corpora).")
    ap.add_argument("--z", type=float, default=DEFAULT_Z,
                    help=f"Outlier strength: flag when top match is this many std "
                         f"devs above the corpus baseline (default {DEFAULT_Z}).")
    ap.add_argument("--abs-floor", type=float, default=ABS_FLOOR,
                    help=f"Soft absolute sanity floor for adaptive mode (default {ABS_FLOOR}).")
    ap.add_argument("--scope", choices=["all", "pin", "note"], default="all",
                    help="Which existing files to compare against (default all).")
    ap.add_argument("--top", type=int, default=3, help="How many nearest matches to report.")
    ap.add_argument("--list", action="store_true",
                    help="Just list the files that would be compared, then exit.")
    args = ap.parse_args()

    if args.list:
        targets = _collect_targets(args.scope)
        print(json.dumps({
            "memory_dir": _rel(MEMORY_DIR),
            "scope": args.scope,
            "count": len(targets),
            "files": [_rel(t) for t in targets],
        }, ensure_ascii=False, indent=2))
        return 0

    # Resolve candidate title/body.
    cand_title, cand_body = args.title.strip(), args.body.strip()
    if args.file:
        fp = Path(args.file)
        if not fp.exists():
            print(json.dumps({"match": False, "error": f"candidate file not found: {args.file}"}))
            return 2
        cand_title, cand_body = _read_title_body(fp)
    if not cand_title and not cand_body:
        print(json.dumps({"match": False, "error": "no candidate: pass --title/--body or --file"}))
        return 2

    # Graceful degrade: no embedding backend -> never block a memory write.
    if not semantic_available():
        print(json.dumps({"match": False, "skipped": "no-embed-backend"}, ensure_ascii=False))
        return 0

    targets = _collect_targets(args.scope)
    if not targets:
        print(json.dumps({"match": False, "candidates": [],
                          "note": "no existing pins/notes to compare"}, ensure_ascii=False))
        return 0

    try:
        emb = get_embeddings()
        cand_vec = emb.embed_query(_embed_text(cand_title, cand_body))
        scored = []
        for t in targets:
            t_title, t_body = _read_title_body(t)
            t_vec = emb.embed_documents([_embed_text(t_title, t_body)])[0]
            scored.append((_cosine(cand_vec, t_vec), t, t_title))
    except SemanticUnavailable:
        print(json.dumps({"match": False, "skipped": "no-embed-backend"}, ensure_ascii=False))
        return 0
    except Exception as e:
        # Any embed failure -> advisory skip, never break the write.
        print(json.dumps({"match": False, "skipped": f"embed-error: {e}"}, ensure_ascii=False))
        return 0

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: max(1, args.top)]
    cand_list = [{"file": _rel(t), "score": round(s, 4), "title": tt} for s, t, tt in top]
    best_score, best_file, best_title = scored[0]
    all_scores = [s for s, _, _ in scored]

    # ── Decision ────────────────────────────────────────────────────────────
    # Absolute mode: only if the caller explicitly passed --threshold (or the
    # env override is set). Otherwise use adaptive z-score scoring, which is the
    # only thing that generalizes across e5 corpora / different buyers.
    abs_threshold = args.threshold
    if abs_threshold is None and DEFAULT_THRESHOLD is not None:
        try:
            abs_threshold = float(DEFAULT_THRESHOLD)
        except ValueError:
            abs_threshold = None

    if abs_threshold is not None:
        is_match = best_score >= abs_threshold
        mode = "absolute"
        gap = round(best_score - abs_threshold, 4)
        suggestion = "merge" if best_score >= (abs_threshold + 0.08) else "review"
        stats = {"mode": mode, "threshold": abs_threshold, "gap": gap}
    else:
        # Adaptive: baseline = mean/std of candidate-vs-all-pins similarity.
        n = len(all_scores)
        mean = sum(all_scores) / n
        var = sum((s - mean) ** 2 for s in all_scores) / n if n > 1 else 0.0
        std = var ** 0.5
        # z of the TOP match against the rest of the corpus. With one dominant
        # outlier, exclude it from the baseline so it doesn't inflate its own std.
        if n > 2:
            rest = all_scores[1:]
            rmean = sum(rest) / len(rest)
            rstd = (sum((s - rmean) ** 2 for s in rest) / len(rest)) ** 0.5
        else:
            rmean, rstd = mean, std
        z = (best_score - rmean) / rstd if rstd > 1e-9 else 0.0
        # Match = statistical outlier above the pack AND clears the soft floor.
        is_match = (z >= args.z) and (best_score >= args.abs_floor)
        mode = "adaptive-z"
        # merge vs review: a strong outlier that clearly dominates -> merge.
        suggestion = "merge" if (z >= args.z + 1.0 and best_score >= args.abs_floor + 0.02) else "review"
        stats = {
            "mode": mode,
            "z": round(z, 3),
            "z_threshold": args.z,
            "baseline_mean": round(rmean, 4),
            "baseline_std": round(rstd, 4),
            "abs_floor": args.abs_floor,
        }

    if is_match:
        out = {
            "match": True,
            "file": _rel(best_file),
            "score": round(best_score, 4),
            "title": best_title,
            "suggestion": suggestion,
            "scoring": stats,
            "candidates": cand_list,
        }
    else:
        out = {
            "match": False,
            "best_score": round(best_score, 4),
            "scoring": stats,
            "candidates": cand_list,
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
