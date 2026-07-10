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
# Portable: workspace from $OPENCLAW_WORKSPACE (falls back to analyst home).
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
MEM="$WS/memory"
[ -d "$MEM" ] || exit 1

for f in "$MEM"/_note_*.md; do
  [ -e "$f" ] || continue
  # Scan the anchored field (no leading whitespace = real top-level frontmatter
  # line) across the WHOLE FILE, not a fixed head -n 15. On notes with long
  # steps:/resume_state: blocks the verified: line can land well past line 15
  # -- same bug class fixed in note-claim-refresh 2026-07-10. Anchored (^field:)
  # grep is safe: prose/steps never start a raw line with this exact token.
  grep -qE '^verified:\s*true\s*$' "$f" && exit 0
done

exit 1
