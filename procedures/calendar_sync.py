#!/usr/bin/env python3
"""
calendar_sync.py — Daily cron: auto-create + auto-resolve calendar events from _note_*.md files.

Two passes:
  1. AUTO-CREATE: scan _note_*.md files with a 'date:' field not yet linked to a calendar event
     → create the event, log to .calendar_created.json to prevent duplicates
  2. AUTO-RESOLVE: check Google Calendar for past events linked to _note_ files
     → delete the note if the event has passed (resolved)

Cron: 0 7 * * * cd <workspace> && python3 procedures/calendar_sync.py >> logs/calendar_sync.log 2>&1

Usage:
  python3 procedures/calendar_sync.py
  python3 procedures/calendar_sync.py --dry-run
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent.parent
MEMORY_DIR = WORKSPACE / "memory"
LOG_FILE = WORKSPACE / "logs/calendar_sync.log"
CREATED_LOG = MEMORY_DIR / ".calendar_created.json"  # dedup log: {note_slug: event_id}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(LOG_FILE, "a", encoding="utf-8")

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    _log_fh.write(line + "\n")
    _log_fh.flush()

def load_created_log():
    """Load dedup log of already-created calendar events."""
    if CREATED_LOG.exists():
        import json
        try:
            return json.loads(CREATED_LOG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_created_log(data):
    import json
    CREATED_LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")

def parse_note_date(note_path):
    """Extract date: and time: fields from a _note_*.md file. Returns (date_str, time_str) or (None, None)."""
    try:
        content = note_path.read_text(encoding="utf-8")
        date_val = None
        time_val = None
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("date:"):
                val = line.split(":", 1)[1].strip()
                if val and val != "YYYY-MM-DD":
                    date_val = val
            elif line.startswith("time:"):
                val = line.split(":", 1)[1].strip()
                if val and val != "HH:MM":
                    time_val = val
        return date_val, time_val
    except Exception:
        return None, None

def auto_create_events(calendar_tool, dry_run):
    """Pass 1: scan _note_*.md with date: field, create calendar events for unlinked ones."""
    created_log = load_created_log()
    created = 0
    skipped = 0

    note_files = sorted(MEMORY_DIR.glob("_note_*.md"))
    for note_path in note_files:
        slug = note_path.name
        if slug in created_log:
            log(f"SKIP (already created): {slug}")
            skipped += 1
            continue

        date_str, time_str = parse_note_date(note_path)
        if not date_str:
            continue  # no date field, skip

        # Read title from first line (# Title)
        try:
            first_line = note_path.read_text(encoding="utf-8").splitlines()[0]
            title = first_line.lstrip("# ").strip() or slug
        except Exception:
            title = slug

        log(f"CREATE: {slug} → '{title}' on {date_str}{' at ' + time_str if time_str else ''}")
        if not dry_run:
            try:
                result = calendar_tool.create_event(
                    title=title,
                    date=date_str,
                    time=time_str,
                    note=slug
                )
                event_id = result.get("id", "unknown")
                created_log[slug] = {"event_id": event_id, "date": date_str, "created_at": datetime.now().strftime("%Y-%m-%d")}
                save_created_log(created_log)
                created += 1
                log(f"  ✓ Created event id={event_id}")
            except Exception as e:
                log(f"  ERROR creating event for {slug}: {e}")
        else:
            created += 1

    log(f"Auto-create done — created: {created} | skipped: {skipped}")
    return created

def main():
    parser = argparse.ArgumentParser(description="Auto-create + auto-resolve calendar events from _note_*.md")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, write nothing")
    parser.add_argument("--days-back", type=int, default=1, help="How many days back to check for resolve (default: 1)")
    args = parser.parse_args()

    log("=" * 50)
    log(f"calendar_sync.py starting (dry_run={args.dry_run})")

    sys.path.insert(0, str(WORKSPACE))
    try:
        from tools import calendar_tool
    except Exception as e:
        log(f"ERROR: could not import calendar_tool: {e}")
        sys.exit(1)

    # Pass 1: auto-create events from notes with date: field
    log("--- Pass 1: auto-create ---")
    auto_create_events(calendar_tool, args.dry_run)

    # Pass 2: auto-resolve past events
    log("--- Pass 2: auto-resolve ---")
    try:
        past_events = calendar_tool.get_past_events(days_back=args.days_back)
    except Exception as e:
        log(f"ERROR: get_past_events failed: {e}")
        past_events = []

    if not past_events:
        log("No past events linked to _note_ files.")
    else:
        resolved = 0
        skipped = 0
        created_log = load_created_log()

        for event in past_events:
            note_file = event["note_file"]
            note_path = MEMORY_DIR / note_file

            if not note_path.exists():
                log(f"SKIP: {note_file} (already deleted or not found)")
                skipped += 1
                continue

            log(f"RESOLVE: {note_file} (event: '{event['title']}' on {event['date']})")
            if not args.dry_run:
                note_path.unlink()
                # Remove from created_log too
                if note_file in created_log:
                    del created_log[note_file]
                    save_created_log(created_log)
                resolved += 1
            else:
                resolved += 1

        log(f"Auto-resolve done — resolved: {resolved} | skipped: {skipped}")

    if args.dry_run:
        log("DRY RUN — no files written or deleted")

if __name__ == "__main__":
    main()
