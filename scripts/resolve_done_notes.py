#!/usr/bin/env python3
"""resolve_done_notes.py — COMPONENT 1: deterministic done-note deleter.

Design (authoritative per project note _note_resolve-done-notes-hardened-closer):
  - Deletes ONLY notes carrying `verified: true` in their frontmatter.
    This is a SECOND marker beyond `status: done`. No done_when re-check here;
    Component 2 (the LLM improver) is responsible for stamping verified: true
    after it has improved + validated the project. C1 is dumb on purpose.
  - ZERO-LLM PRE-GATE: if NO `status: done` note exists anywhere, exit 0 fast.
    (Guards the whole pipeline incl. C2 from needless cost.)
  - Snapshot every note before deleting it (safety / recoverable).
  - flock the memory dir so C1 never races Advancer / C2 on _note_*.md.
  - Log every action.

Portable: resolves workspace from $OPENCLAW_WORKSPACE so the same file works in
the analyst home and in the neuron buyer copy.

Usage:
  resolve_done_notes.py            # execute deletions
  resolve_done_notes.py --dry-run  # report only, delete nothing
"""
import os
import re
import sys
import shutil
import fcntl
import datetime

WS = os.environ.get("OPENCLAW_WORKSPACE", "/root/.openclaw/workspace-analyst")
MEM_DIR = os.path.join(WS, "memory")
SNAP_DIR = os.path.join(WS, "backups", "resolved_notes")
LOG_FILE = os.path.join(WS, "logs", "resolve_done_notes.log")
LOCK_FILE = os.path.join(MEM_DIR, ".notes.lock")

FRONTMATTER_LINES = 15  # scan a few lines of frontmatter for markers


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def frontmatter(path: str):
    """Return the first FRONTMATTER_LINES lines (lowercased-key view helper)."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, ln in enumerate(fh):
                if i >= FRONTMATTER_LINES:
                    break
                out.append(ln.rstrip("\n"))
    except OSError:
        pass
    return out


def has_marker(lines, key: str, value: str) -> bool:
    """True if a frontmatter line is exactly `key: value` (case-insensitive, trimmed)."""
    target = f"{key}: {value}".lower()
    for ln in lines:
        if ln.strip().lower() == target:
            return True
    return False

def field_value(lines, key: str):
    """Return the raw value of a `key: value` frontmatter line, or None."""
    for ln in lines:
        m = re.match(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

# Claim freshness windows (MUST match Advancer + Component 2 exactly, or split-brain).
# claimed_at is ALWAYS UTC ISO8601 (e.g. 2026-07-07T00:55:00Z).
LIVE_SESSION_WINDOW_MIN = 120.0   # human working in-session; give a wide 2h grace
OTHER_CLAIMANT_WINDOW_MIN = 30.0  # advancer / improver instance; short enough to recover a dead run

def is_actively_claimed(lines) -> bool:
    """True if the note carries a FRESH claim (another job / live human is mid-edit).
    Never delete such a note, even if verified. Windows match the Advancer:
    live-session = 120 min, any other claimant = 30 min. Stale claims are ignored."""
    claimed_by = field_value(lines, "claimed_by")
    claimed_at = field_value(lines, "claimed_at")
    if not claimed_by or not claimed_at:
        return False
    try:
        ts = claimed_at.strip().replace("Z", "+00:00")
        when = datetime.datetime.fromisoformat(ts)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return False  # unparseable timestamp -> treat as not-claimed (stale/garbage)
    now = datetime.datetime.now(datetime.timezone.utc)
    age_min = (now - when).total_seconds() / 60.0
    window = LIVE_SESSION_WINDOW_MIN if claimed_by.lower().startswith("live-session") else OTHER_CLAIMANT_WINDOW_MIN
    return age_min < window


def note_files():
    if not os.path.isdir(MEM_DIR):
        return []
    return sorted(
        os.path.join(MEM_DIR, f)
        for f in os.listdir(MEM_DIR)
        if f.startswith("_note_") and f.endswith(".md")
    )


def snapshot(path: str) -> str:
    os.makedirs(SNAP_DIR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    dest = os.path.join(SNAP_DIR, f"{ts}__{base}")
    shutil.copy2(path, dest)
    return dest


def main() -> int:
    dry = "--dry-run" in sys.argv

    files = note_files()
    if not files:
        log("no _note_ files present; nothing to do")
        return 0

    # ---- ZERO-LLM PRE-GATE: require at least one status: done note ----
    any_done = any(has_marker(frontmatter(f), "status", "done") for f in files)
    if not any_done:
        log("pre-gate: no status:done note exists -> exit (no work, no LLM cost)")
        return 0

    # ---- lock the memory dir for the mutation window (NON-BLOCKING) ----
    # Non-blocking on purpose: this runs as a cron. If the Advancer / Component 2
    # holds the lock, we must NOT hang waiting (crons would pile up) — exit clean
    # and let the next tick retry.
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("lock busy (another notes job running); exiting clean, will retry next run")
        lock_fh.close()
        return 0
    except OSError as e:
        log(f"could not acquire lock: {e}; aborting")
        lock_fh.close()
        return 1

    deleted = 0
    try:
        for f in note_files():  # re-list under lock
            fm = frontmatter(f)
            if not has_marker(fm, "verified", "true"):
                continue  # C1 deletes ONLY verified:true notes
            if is_actively_claimed(fm):
                # A fresh claim means another job / live human is mid-edit. Never
                # delete out from under them, even if verified — wait for release.
                log(f"skip (actively claimed): {os.path.basename(f)}")
                continue
            base = os.path.basename(f)
            # Promote to _pin_ if the note asks to be kept as a durable insight.
            promote = False
            try:
                with open(f, "r", encoding="utf-8") as pfh:
                    promote = "promote: pin" in pfh.read().lower()
            except OSError:
                pass
            if dry:
                log(f"DRY-RUN would delete verified note: {base}"
                    + (" (+promote to _pin_)" if promote else ""))
                deleted += 1
                continue
            snap = snapshot(f)
            if promote:
                pin = os.path.join(MEM_DIR, base.replace("_note_", "_pin_", 1))
                try:
                    shutil.copy2(f, pin)
                    log(f"promoted {base} -> {os.path.basename(pin)}")
                except OSError as e:
                    log(f"promote failed for {base}: {e}")
            os.remove(f)
            deleted += 1
            log(f"deleted verified note: {base} (snapshot: {snap})")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    verb = "would delete" if dry else "deleted"
    log(f"done: {verb} {deleted} verified note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
