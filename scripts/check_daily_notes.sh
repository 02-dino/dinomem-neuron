#!/usr/bin/env bash
# check_daily_notes.sh — pre-check before Daily Note Review LLM call
# Exits 0 if there are memory notes worth reviewing (LLM should run)
# Exits 1 if nothing new to review (skip LLM)
#
# Worth reviewing = any memory/*.md file modified since yesterday's review
# window (06:00 UTC previous day → now). Also triggers if any _note_ files
# exist with in_progress/pending status (active work in progress).
#
# Cost: pure filesystem scan, zero LLM, zero network.

set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
MEMORY_DIR="$WS/memory"

# Gate 1: Check for any _note_ files with active status (same as check_projects.sh logic)
if [ -d "$MEMORY_DIR" ]; then
  for f in "$MEMORY_DIR"/_note_*.md; do
    [ -f "$f" ] || continue
    fm="$(awk 'NF==0{exit} /^[[:space:]]*#/{next} {print}' "$f")"
    printf '%s\n' "$fm" | grep -qE "^type: project" || continue
    printf '%s\n' "$fm" | grep -qE "^status: (in_progress|pending)" && exit 0
  done
fi

# Gate 2: Check for memory files modified in the last 24 hours
# The Daily Note Review runs at 06:00 UTC — review if anything changed since yesterday
YESTERDAY_EPOCH=$(date -d "24 hours ago" +%s 2>/dev/null || date -v-24H +%s 2>/dev/null)
if [ -n "$YESTERDAY_EPOCH" ] && [ -d "$MEMORY_DIR" ]; then
  found_new=false
  for f in "$MEMORY_DIR"/*.md; do
    [ -f "$f" ] || continue
    # Skip _note_ files (already handled above) and MEMORY.md (index file)
    basename_f="$(basename "$f")"
    [ "$basename_f" = "MEMORY.md" ] && continue
    # Check modification time
    mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)
    if [ -n "$mtime" ] && [ "$mtime" -ge "$YESTERDAY_EPOCH" ]; then
      found_new=true
      break
    fi
  done
  if [ "$found_new" = true ]; then
    exit 0
  fi
fi

# Nothing worth reviewing
exit 1
