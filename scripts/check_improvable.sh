#!/usr/bin/env bash
# check_improvable.sh — zero-LLM pre-gate for COMPONENT 2 (the project improver).
#
# Exit 0  => at least one project note is status: done, NOT verified, and not freshly
#            claimed by another job → Component 2 has work; the LLM turn should run.
# Exit 1  => nothing to improve → Component 2 must stop immediately, no LLM cost.
#
# A note qualifies when ALL of:
#   - has  type: project
#   - has  status: done
#   - does NOT have  verified: true   (C2 stamps this once it's improved+validated)
#   - is not FRESHLY claimed by another job (live-session <120m, other <30m). Stale = ok.
#     claimed_at is ALWAYS UTC ISO8601. Windows MUST match Advancer + C1 + C2 exactly.
#
# Portable: workspace from $OPENCLAW_WORKSPACE (falls back to analyst home).
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
MEM="$WS/memory"
[ -d "$MEM" ] || exit 1

now_epoch=$(date -u +%s)

for f in "$MEM"/_note_*.md; do
  [ -e "$f" ] || continue
  head_block=$(head -n 15 "$f")

  echo "$head_block" | grep -qiE '^\s*type:\s*project\s*$'   || continue
  echo "$head_block" | grep -qiE '^\s*status:\s*done\s*$'    || continue
  # already verified → not improvable (Component 1 will delete it)
  echo "$head_block" | grep -qiE '^\s*verified:\s*true\s*$'  && continue

  # fresh-claim check: skip if another job is actively working it
  claimed_by=$(echo "$head_block" | grep -iE '^\s*claimed_by:' | head -n1 | sed -E 's/^\s*claimed_by:\s*//I' | tr -d '\r' | xargs || true)
  claimed_at=$(echo "$head_block" | grep -iE '^\s*claimed_at:' | head -n1 | sed -E 's/^\s*claimed_at:\s*//I' | tr -d '\r' | xargs || true)
  if [ -n "$claimed_by" ] && [ -n "$claimed_at" ]; then
    if claim_epoch=$(date -u -d "$claimed_at" +%s 2>/dev/null); then
      age=$(( now_epoch - claim_epoch ))
      case "$claimed_by" in
        live-session*) window=7200 ;;   # 120 min (2h) live-session grace
        *)             window=1800 ;;    # 30 min other-claimant grace
      esac
      # fresh claim → another job owns it now → not our work this run
      [ "$age" -lt "$window" ] && continue
    fi
  fi

  # found an improvable note
  exit 0
done

exit 1
