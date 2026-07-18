#!/usr/bin/env bash
# check_projects.sh — pre-check before Project Advancer LLM call
# Exits 0 if there are open (in_progress|pending) project notes (LLM should run)
# Exits 1 if nothing to do (skip LLM)
# Only checks frontmatter (first 10 lines) to avoid false positives from body text.
# NOTE: match BOTH in_progress AND pending. A freshly-created project note starts
# at `pending` and nothing moves it to in_progress on its own, so gating on
# in_progress-only left brand-new project notes stalled forever (the
# dinomem-open-notes hook already surfaces both statuses; this aligns the gate).
WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"

for f in "$WS"/memory/_note_*.md; do
  [ -f "$f" ] || continue
  head -10 "$f" | grep -q "^type: project" || continue
  head -10 "$f" | grep -qE "^status: (in_progress|pending)" && exit 0
done
exit 1
