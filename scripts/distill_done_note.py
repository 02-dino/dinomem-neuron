#!/usr/bin/env python3
"""distill_done_note.py — NEURON-only done-note distiller (Phase 2).

When a _note_ reaches status:done + verified:true and is about to be retired by
resolve_done_notes.py, its knowledge should NOT just vanish. This distiller runs
FIRST (as a separate cheap no-reasoning step), turning the note body into proper
schema memory entries so the build/history is retrievable later.

DESIGN (see _note_done-note-retire-distiller-brainstorm.md — DECISIONS, all
resolved + dino-confirmed 2026-07-08):
  1. REUSE extract_memory.extract_from_text() (cheap no-reasoning LLM) rather
     than a bespoke template. The output must be the REAL memory schema
     (topics/relations/entities/classification) so the benang graph consumes it.
     A deterministic template cannot infer relations/entity-types — it would emit
     semantically-empty entries that review culls. So distillation IS LLM work.
  2. SEPARATE step from Project Improver (verify-only) and from
     resolve_done_notes.py (which stays ZERO-LLM: delete only). This script is
     that separate step, invoked by resolve_done_notes.py right before delete.
  3. Distilled entries go to REGULAR dated memory (default home). They enter the
     normal review pipeline and FREEZE on merit if they keep scoring [valid], or
     get culled if they stop being useful. No auto-pin. (promote:pin notes are
     handled by resolve_done_notes.py's existing hatch and are NOT distilled here
     — they're kept whole.)
  4. SEED each distilled entry `verdict: valid` + today's date. A fresh entry has
     no review verdict until its 7d bucket; the deterministic deleter treats
     no-verdict as not-valid. The note was JUST verified true = legitimately
     valid, so seeding protects it until review re-judges it.

FAIL-SAFE: if distillation fails (LLM error, empty), we return non-fatal. The
caller (resolve_done_notes.py) proceeds to delete the note anyway = same as
today's behavior. Distillation is a best-effort enrichment, never a delete gate.

Usage:
  distill_done_note.py <path-to-_note_.md>            # distill one note
  distill_done_note.py <path> --dry-run               # show what would be written
Exit codes: 0 = distilled (or nothing memorable), 2 = LLM failed, 1 = bad args.
"""
import sys
from pathlib import Path

# Make the neuron procedures/ importable (extract_memory lives there).
_THIS = Path(__file__).resolve()
_PROC_DIR = _THIS.parent.parent / "procedures"
sys.path.insert(0, str(_PROC_DIR))

try:
    import extract_memory as em
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"[distill] cannot import extract_memory: {e}\n")
    sys.exit(1)


def strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---) if present.
    The distiller wants the note BODY (the actual knowledge), not the note's
    own scaffolding metadata (type/status/done_when/etc.)."""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).strip()
    # No frontmatter block; also drop obvious scaffolding key: value lines at top
    # (type:, status:, done_when:, stale_after:, current_step:, resume_state:).
    return text.strip()


def distill(note_path: str, dry_run: bool = False) -> int:
    p = Path(note_path)
    if not p.exists():
        sys.stderr.write(f"[distill] note not found: {note_path}\n")
        return 1
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"[distill] read failed: {e}\n")
        return 1

    body = strip_frontmatter(raw)
    if not body.strip():
        em.log(f"[distill] {p.name}: empty body, nothing to distill")
        return 0

    label = p.stem.replace("_note_", "").replace("-", " ")[:40] or "done note"
    summary = em.extract_from_text(body, source_label=f"distill:{label}")

    if summary == "LLM_FAILED":
        em.log(f"[distill] {p.name}: LLM failed — note will delete without distill (non-fatal)")
        return 2
    if not summary:
        em.log(f"[distill] {p.name}: nothing memorable — no entries written")
        return 0

    if dry_run:
        import json as _json
        em.log(f"[distill] DRY-RUN {p.name}: would write entries:")
        print(_json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    # Write to regular dated memory, seeded verdict: valid (Q4).
    em.write_memory_file(summary, seed_verdict="valid")
    em.log(f"[distill] {p.name}: distilled -> regular memory (verdict:valid seeded)")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        sys.stderr.write("usage: distill_done_note.py <path-to-_note_.md> [--dry-run]\n")
        return 1
    return distill(args[0], dry_run=dry)


if __name__ == "__main__":
    sys.exit(main())
