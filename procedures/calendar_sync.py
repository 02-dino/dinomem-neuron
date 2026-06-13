#!/usr/bin/env python3
"""
calendar_sync.py — Daily cron: auto-resolve _note_*.md files linked to past calendar events.

Runs daily. Checks Google Calendar for events that have passed and are linked
to a memory/_note_*.md file. If the linked note exists, deletes it (resolved).

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

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(LOG_FILE, "a", encoding="utf-8")

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    _log_fh.write(line + "\n")
    _log_fh.flush()

def main():
    parser = argparse.ArgumentParser(description="Auto-resolve _note_*.md from past calendar events")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted, write nothing")
    parser.add_argument("--days-back", type=int, default=1, help="How many days back to check (default: 1)")
    args = parser.parse_args()

    log("=" * 50)
    log(f"calendar_sync.py starting (dry_run={args.dry_run})")

    # Import calendar tool
    sys.path.insert(0, str(WORKSPACE))
    try:
        from tools.calendar_tool import get_past_events
    except Exception as e:
        log(f"ERROR: could not import calendar_tool: {e}")
        sys.exit(1)

    # Get past events linked to _note_ files
    try:
        past_events = get_past_events(days_back=args.days_back)
    except Exception as e:
        log(f"ERROR: get_past_events failed: {e}")
        sys.exit(1)

    if not past_events:
        log("No past events linked to _note_ files.")
        return

    resolved = 0
    skipped = 0

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
            resolved += 1
        else:
            resolved += 1  # count for dry-run reporting

    log(f"Done — resolved: {resolved} | skipped: {skipped}")
    if args.dry_run:
        log("DRY RUN — no files deleted")

if __name__ == "__main__":
    main()
