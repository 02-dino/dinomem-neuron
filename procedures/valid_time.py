#!/usr/bin/env python3
"""
valid_time.py — bitemporal valid-time layer for dinomem memory.

WHY
  Memory files carry TRANSACTION time (mtime / git / _memory_diff = when we
  RECORDED a fact). They do NOT carry VALID time (when the fact was TRUE in the
  world). So "where did Dino live in March?" is unanswerable: recency ranks the
  newest write on top even if it wasn't yet true then. This module adds the
  valid-time axis as OPTIONAL frontmatter, plus supersession (retire-not-delete).

WHAT (this file = the mechanical helper; dedup/cleanup/hybrid_recall patches are
  separate steps that CALL this)
  * parse/write three optional frontmatter fields on any _pin_/_note_ memory file:
      valid_from:    ISO date the fact became true   (absent = unknown/always)
      valid_until:   ISO date it stopped being true  (absent/null = still true)
      subject:       stable key for "same question"  (e.g. dino.location)
    plus supersession bookkeeping: superseded_by / supersedes (file back-pointers).
  * close_interval(): stamp an old fact's valid_until RETROACTIVELY when a newer
    fact supersedes it (valid_until is written by the NEXT fact, never guessed up
    front).
  * retire(): MOVE a superseded file to memory/_history/ (still on disk / indexable,
    ranked lower), never delete.
  * as_of(date) predicate: is this fact valid at a given instant?
  * subject_index(): EFFICIENCY — build a {subject -> [files]} index ONCE so a
    supersession check on write is an O(1) dict lookup by subject key, NOT an
    O(n) rescan of every memory file. Cached to memory/.cache/ keyed on dir mtime.

DESIGN DECISIONS (owner-locked 2026-08-11, see
  _note_bitemporal-valid-time-layer-design.md)
  * DEFAULT-ON upgrade, no flag. Safety = ABSENT fields reproduce today's exact
    behavior; new branches fire only when a supersession is actually detected.
  * Supersession eligibility v1 = EXPLICIT `subject:` key only (deterministic, no
    false-retire). Embedding-inferred subject is a deferred, separate upgrade.
  * valid_until set retroactively by the contradicting fact, never at write time.

GENERALIZED (neuron-shipped, not analyst-specific)
  * Workspace from DINOMEM_WORKSPACE (sed-substituted at install) with a self-locate
    fallback — identical convention to memory_dedup_check.py. No baked operator path.
  * Pure stdlib (re / json / os / shutil / datetime / pathlib). No embedding backend
    needed for v1 (explicit subject key), so it runs on any install unconditionally.

EXIT CODE
  0 on a completed op. Non-zero only on usage error.

USAGE (CLI, for testing / manual ops; the real callers are dedup/cleanup/hybrid_recall)
  python3 valid_time.py --read  <file>                 # dump parsed valid-time fields (JSON)
  python3 valid_time.py --as-of <file> --date <ISO>    # is <file> valid at <date>? (JSON)
  python3 valid_time.py --index [--subject <key>]      # subject index (all, or one key)
  python3 valid_time.py --supersede <old> <new>        # close <old>, retire, link both
  python3 valid_time.py --self-test                    # in-tmpdir end-to-end check
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# --- workspace resolution (generalized: env > install-sed > self-locate) --------
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run (ZIP/manual/dev)
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
BASE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEM_DIR = BASE / "memory"
HISTORY_DIR = MEM_DIR / "_history"
CACHE_DIR = MEM_DIR / ".cache"
INDEX_CACHE = CACHE_DIR / "subject_index.json"

# Fields we manage. All OPTIONAL; absence = today's behavior.
VT_FIELDS = ("valid_from", "valid_until", "subject", "superseded_by", "supersedes")
# Scan only the frontmatter-ish head (cheap; markers live near the top).
_HEAD_LINES = 30
_ISO = "%Y-%m-%d"


def _today() -> str:
    return datetime.now(timezone.utc).strftime(_ISO)


def _parse_date(s):
    """Lenient ISO date parse. Accepts YYYY-MM-DD or full ISO; returns date or None."""
    if not s or str(s).strip().lower() in ("null", "none", ""):
        return None
    s = str(s).strip()
    for fmt in (_ISO, "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # last resort: date part before any 'T'
    try:
        return datetime.strptime(s.split("T")[0], _ISO).date()
    except ValueError:
        return None


# --- frontmatter read/write (stdlib, matches suite convention) ------------------
_FIELD_RE = {f: re.compile(r"^\s*%s\s*:\s*(.*?)\s*$" % f, re.IGNORECASE) for f in VT_FIELDS}


def read_fields(path: Path) -> dict:
    """Parse the OPTIONAL valid-time fields from a memory file's head.
    Returns {field: value_or_None}. Absent field -> None (= today's behavior)."""
    out = {f: None for f in VT_FIELDS}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for line in text.splitlines()[:_HEAD_LINES]:
        for f, rx in _FIELD_RE.items():
            if out[f] is None:
                m = rx.match(line)
                if m:
                    v = m.group(1).strip()
                    out[f] = None if v.lower() in ("null", "none", "") else v
    return out


def write_fields(path: Path, updates: dict) -> None:
    """Set/overwrite valid-time fields in a memory file's head, in place.
    Fields already present (in first _HEAD_LINES) are replaced; missing ones are
    inserted right after the H1 title (or at top if no H1). Only keys in VT_FIELDS
    are honored. A None value writes an explicit 'null' (used to close intervals)."""
    updates = {k: v for k, v in updates.items() if k in VT_FIELDS}
    if not updates:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=False)
    remaining = dict(updates)
    # 1) replace in place any field already in the head
    for i, line in enumerate(lines[:_HEAD_LINES]):
        for f in list(remaining.keys()):
            if _FIELD_RE[f].match(line):
                val = remaining.pop(f)
                lines[i] = "%s: %s" % (f, "null" if val is None else val)
    # 2) insert any not-yet-present fields after the H1 (or at top)
    if remaining:
        insert_at = 0
        for i, line in enumerate(lines[:_HEAD_LINES]):
            if line.strip().startswith("# "):
                insert_at = i + 1
                break
        block = ["%s: %s" % (f, "null" if v is None else v) for f, v in remaining.items()]
        lines[insert_at:insert_at] = block
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")


# --- as_of predicate ------------------------------------------------------------
def is_valid_at(fields: dict, when) -> bool:
    """Is a fact (given its parsed fields) valid at instant `when` (date or ISO str)?
    Semantics: valid_from <= when < valid_until, with absent bounds = open.
    Absent BOTH bounds -> always valid (True) = today's behavior."""
    when = when if isinstance(when, date) else _parse_date(when)
    if when is None:
        return True  # unknown query date -> don't filter
    vf = _parse_date(fields.get("valid_from"))
    vu = _parse_date(fields.get("valid_until"))
    if vf is not None and when < vf:
        return False
    if vu is not None and when >= vu:
        return False
    return True


def is_current(fields: dict) -> bool:
    """A fact is CURRENT iff it has not been closed (valid_until absent/null) and
    is not marked superseded. This is the default retrieval visibility."""
    return _parse_date(fields.get("valid_until")) is None and not fields.get("superseded_by")


# --- subject index (EFFICIENCY: O(1) supersession lookup, no full rescan) -------
def _mem_files():
    """Live memory files eligible to carry valid-time (pins + notes, not _history)."""
    if not MEM_DIR.exists():
        return []
    out = []
    for p in sorted(MEM_DIR.glob("_pin_*.md")) + sorted(MEM_DIR.glob("_note_*.md")):
        out.append(p)
    return out


def _dir_signature() -> str:
    """Cheap staleness key for the index cache: count + max mtime of memory files.
    If nothing changed, we reuse the cached index instead of re-reading every file."""
    files = _mem_files()
    if not files:
        return "0:0"
    mx = max(p.stat().st_mtime for p in files)
    return "%d:%d" % (len(files), int(mx))


def subject_index(force: bool = False) -> dict:
    """Return {subject_key: [relative_file, ...]} for live memory files that carry
    an explicit `subject:` field. Cached to memory/.cache/ keyed on _dir_signature
    so a write-time supersession check is an O(1) dict lookup, NOT an O(n) rescan.
    Files WITHOUT an explicit subject are absent from the index by design (v1 =
    explicit-subject-only supersession; deterministic, no false-retire)."""
    sig = _dir_signature()
    if not force and INDEX_CACHE.exists():
        try:
            cached = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
            if cached.get("_sig") == sig:
                return {k: v for k, v in cached.items() if k != "_sig"}
        except Exception:
            pass  # fall through to rebuild (fail-open)
    idx = {}
    for p in _mem_files():
        subj = read_fields(p).get("subject")
        if subj:
            idx.setdefault(subj, []).append(str(p.relative_to(BASE)))
    # write cache (best-effort; a failed cache write must never break callers)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(idx)
        payload["_sig"] = sig
        INDEX_CACHE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
    return idx


def find_supersede_targets(subject, exclude=None):
    """Given a NEW fact's subject key, return the live files that answer the same
    question and are still CURRENT (open valid_until, not already superseded) — the
    candidates a supersession would retire. O(1) index lookup + a bounded re-read of
    only the matched files. Empty subject or no match -> [] (fail-open: caller just
    writes a new fact, retires nothing)."""
    if not subject:
        return []
    idx = subject_index()
    hits = idx.get(subject, [])
    exclude = str(exclude) if exclude else None
    targets = []
    for rel in hits:
        if exclude and rel.endswith(exclude):
            continue
        p = BASE / rel
        if p.exists() and is_current(read_fields(p)):
            targets.append(p)
    return targets


# --- supersede / retire (fail-open; retire = move, never delete) ----------------
def close_interval(old_path: Path, valid_until, new_rel: str = None) -> None:
    """Stamp an old fact's valid_until RETROACTIVELY (written by the newer fact).
    Idempotent: if already closed, leaves the earlier boundary intact."""
    fields = read_fields(old_path)
    if _parse_date(fields.get("valid_until")) is not None:
        return  # already closed; don't move a boundary backward/forward silently
    upd = {"valid_until": valid_until or _today()}
    if new_rel:
        upd["superseded_by"] = new_rel
    write_fields(old_path, upd)


def retire(old_path: Path) -> Path:
    """MOVE a superseded file to memory/_history/ (never delete). Returns new path.
    Name-collision safe. Fail-open: on any error, leaves the file in place and
    returns the original path (a failed retire must not lose data)."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        dest = HISTORY_DIR / old_path.name
        n = 1
        while dest.exists():
            dest = HISTORY_DIR / ("%s.%d%s" % (old_path.stem, n, old_path.suffix))
            n += 1
        shutil.move(str(old_path), str(dest))
        return dest
    except Exception:
        return old_path


def supersede(old_path: Path, new_path: Path, valid_from=None) -> dict:
    """Full supersession: close old interval at new fact's valid_from (or today),
    link both directions, retire old to _history. Returns a small result dict.
    This is the deterministic executor the dedup patch calls when it has a
    same-subject + different-value + later-time collision. FAIL-OPEN throughout."""
    new_rel = str(new_path.relative_to(BASE)) if str(new_path).startswith(str(BASE)) else new_path.name
    old_rel = str(old_path.relative_to(BASE)) if str(old_path).startswith(str(BASE)) else old_path.name
    boundary = valid_from or read_fields(new_path).get("valid_from") or _today()
    close_interval(old_path, boundary, new_rel)
    # record the forward pointer on the NEW fact too (supersedes: old)
    try:
        write_fields(new_path, {"supersedes": old_rel})
    except Exception:
        pass
    retired = retire(old_path)
    return {
        "superseded": old_rel,
        "by": new_rel,
        "valid_until": boundary,
        "retired_to": str(retired.relative_to(BASE)) if str(retired).startswith(str(BASE)) else str(retired),
    }


# --- self-test (in a throwaway tmpdir; touches no real memory) -------------------
def _self_test() -> int:
    import tempfile
    global BASE, MEM_DIR, HISTORY_DIR, CACHE_DIR, INDEX_CACHE
    tmp = Path(tempfile.mkdtemp(prefix="vt_selftest_"))
    BASE = tmp
    MEM_DIR = tmp / "memory"
    HISTORY_DIR = MEM_DIR / "_history"
    CACHE_DIR = MEM_DIR / ".cache"
    INDEX_CACHE = CACHE_DIR / "subject_index.json"
    MEM_DIR.mkdir(parents=True)
    ok = True
    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print("  %s %s" % ("PASS" if cond else "FAIL", name))
    # 1) old fact, explicit subject, open interval
    old = MEM_DIR / "_pin_dino-location-jakarta.md"
    old.write_text("# Dino location Jakarta\nsubject: dino.location\nvalid_from: 2024-01-01\n\nDino lives in Jakarta.\n", encoding="utf-8")
    of = read_fields(old)
    check("read explicit fields", of["subject"] == "dino.location" and of["valid_from"] == "2024-01-01" and of["valid_until"] is None)
    check("old is current", is_current(of))
    check("old valid at 2025-06-01", is_valid_at(of, "2025-06-01"))
    # 2) backward-compat: a fact with NO valid-time fields behaves as always-valid
    plain = MEM_DIR / "_note_plain.md"
    plain.write_text("# Plain note\n\nno fields here.\n", encoding="utf-8")
    pf = read_fields(plain)
    check("no-fields = always valid (backcompat)", is_valid_at(pf, "1999-01-01") and is_current(pf))
    # 3) subject index finds the target O(1)
    tgts = find_supersede_targets("dino.location")
    check("subject index finds 1 target", len(tgts) == 1 and tgts[0].name == old.name)
    check("unknown subject -> no target (fail-open)", find_supersede_targets("nope.subject") == [])
    check("empty subject -> no target", find_supersede_targets("") == [])
    # 4) new fact supersedes old
    new = MEM_DIR / "_pin_dino-location-bali.md"
    new.write_text("# Dino location Bali\nsubject: dino.location\nvalid_from: 2026-05-01\n\nDino lives in Bali.\n", encoding="utf-8")
    res = supersede(old, new)
    check("supersede returns boundary 2026-05-01", res["valid_until"] == "2026-05-01")
    check("old retired to _history", res["retired_to"].startswith("memory/_history/") and (HISTORY_DIR / old.name).exists())
    check("old file no longer in live memory", not old.exists())
    # 5) retired fact is CLOSED and answers past-time correctly
    rf = read_fields(HISTORY_DIR / old.name)
    check("retired old closed at 2026-05-01", rf["valid_until"] == "2026-05-01")
    check("retired old NOT current", not is_current(rf))
    check("retired old still valid_at 2025-06 (past-time query)", is_valid_at(rf, "2025-06-01"))
    check("retired old NOT valid_at 2026-08 (after supersession)", not is_valid_at(rf, "2026-08-01"))
    # 6) new fact is the current one
    nf = read_fields(new)
    check("new fact current", is_current(nf))
    check("new fact valid_at 2026-08", is_valid_at(nf, "2026-08-01"))
    check("new fact links supersedes old", nf["supersedes"] and nf["supersedes"].endswith(old.name))
    # 7) idempotent close: re-closing doesn't move boundary
    close_interval(HISTORY_DIR / old.name, "2099-01-01")
    check("close_interval idempotent", read_fields(HISTORY_DIR / old.name)["valid_until"] == "2026-05-01")
    shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST:", "OK" if ok else "FAILED")
    return 0 if ok else 1


# --- CLI ------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="bitemporal valid-time helper for dinomem memory")
    ap.add_argument("--read", metavar="FILE")
    ap.add_argument("--as-of", metavar="FILE")
    ap.add_argument("--date", metavar="ISO")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--subject", metavar="KEY")
    ap.add_argument("--supersede", nargs=2, metavar=("OLD", "NEW"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(_self_test())
    if a.read:
        print(json.dumps(read_fields(Path(a.read)), indent=2)); return
    if a.as_of:
        f = read_fields(Path(a.as_of))
        print(json.dumps({"file": a.as_of, "date": a.date, "valid_at": is_valid_at(f, a.date), "current": is_current(f)}, indent=2)); return
    if a.index:
        idx = subject_index(force=True)
        print(json.dumps(idx.get(a.subject, []) if a.subject else idx, indent=2)); return
    if a.supersede:
        print(json.dumps(supersede(Path(a.supersede[0]), Path(a.supersede[1])), indent=2)); return
    ap.print_help()


if __name__ == "__main__":
    main()
