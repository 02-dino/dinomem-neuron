#!/usr/bin/env bash
# check_projects.sh — pre-check before Project Advancer LLM call
# Exits 0 if there are open (in_progress|pending) project notes (LLM should run)
# Exits 1 if nothing to do (skip LLM)
# Only checks frontmatter (first 10 lines) to avoid false positives from body text.
# NOTE: canonical project birth state is `in_progress` (see memory-pinning skill:
# projects are born in_progress -> done; `pending | done` is the task_bound/
# time_bound schema for todos/reminders, NOT projects). We STILL match `pending`
# here as a defensive SAFETY NET: if a project note is ever mistakenly born
# `pending` (author copied the todo schema), it must not silently stall forever.
# So the gate wakes the Advancer for BOTH statuses; costs nothing, prevents orphans.
# (The dinomem-open-notes hook likewise surfaces both statuses.)
# IMPORTANT: this gate only ever handles `type: project` notes — digital/verifiable
# work the Advancer CAN drive. Real-world / physical / non-verifiable tasks ("pick
# flowers", anything off-machine) are `type: task_bound` (inbound), NEVER project,
# and are intentionally invisible to this gate — the Advancer cannot do them.
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"

for f in "$WS"/memory/_note_*.md; do
  [ -f "$f" ] || continue
  head -10 "$f" | grep -q "^type: project" || continue
  head -10 "$f" | grep -qE "^status: (in_progress|pending)" && exit 0
done
exit 1
