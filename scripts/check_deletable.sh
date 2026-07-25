#!/usr/bin/env bash
# check_deletable.sh — zero-LLM pre-gate for COMPONENT 1 (the verified-note deleter).
#
# Exit 0  => at least one note carries  verified: true  → the deleter has work.
# Exit 1  => nothing to delete → the deleter LLM turn should not run.
#
# The deleter's LLM prompt is a thin wrapper around resolve_done_notes.py, which
# itself self-gates and only deletes notes with verified: true. This mirror gate
# lets the command-cron avoid spending an LLM turn when there is provably nothing
# to retire. resolve_done_notes.py remains the authority on WHAT gets deleted; this
# only answers "is there anything at all?".
#
# Portable: workspace from $OPENCLAW_WORKSPACE (install.sh sets it per-installer;
# generic fallback for any host).
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
MEM="$WS/memory"
[ -d "$MEM" ] || exit 1

# Re-fire guard: normally a verified:true note is deleted on the FIRST run
# (self-resolving -- note disappears, gate stops finding it). This guard only
# matters for the edge case where deletion itself is blocked (e.g. resolve_done_
# notes.py's lock is busy, or the note is freshly claimed) and the note lingers
# with an unchanged body across ticks -- without this it would burn an LLM turn
# every */15 tick re-attempting the same blocked delete. Same v2 pattern as
# check_daily_notes.sh / check_ideator.sh / check_projects.sh / check_improvable.sh.
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gate_refire_guard.sh
source "$SCRIPTS_DIR/_gate_refire_guard.sh"
STATE_FILE="$SCRIPTS_DIR/.check_deletable.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor for a stuck/blocked delete

qualifying=()
for f in "$MEM"/_note_*.md; do
  [ -e "$f" ] || continue
  # Scan the anchored field (no leading whitespace = real top-level frontmatter
  # line) across the WHOLE FILE, not a fixed head -n 15. On notes with long
  # steps:/resume_state: blocks the verified: line can land well past line 15
  # -- same bug class fixed in note-claim-refresh 2026-07-10. Anchored (^field:)
  # grep is safe: prose/steps never start a raw line with this exact token.
  grep -qE '^verified:\s*true\s*$' "$f" && qualifying+=("$f")
done

[ "${#qualifying[@]}" -gt 0 ] || exit 1

if refire_should_fire "$STATE_FILE" "$MIN_INTERVAL_SECS" "${qualifying[@]}"; then
  exit 0
fi
exit 1
