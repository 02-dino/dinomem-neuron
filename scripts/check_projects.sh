#!/usr/bin/env bash
# check_projects.sh — pre-check before Project Advancer LLM call
# Exits 0 if there are in_progress project notes (LLM should run)
# Exits 1 if nothing to do (skip LLM)
# Only checks frontmatter (first 10 lines) to avoid false positives from body text.
WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"

for f in "$WS"/memory/_note_*.md; do
  [ -f "$f" ] || continue
  head -10 "$f" | grep -q "^type: project" || continue
  head -10 "$f" | grep -q "^status: in_progress" && exit 0
done
exit 1
