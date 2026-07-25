#!/usr/bin/env bash
# check_ideator.sh — zero-LLM pre-gate for the IDEATOR (brainstorm critic).
#
# PURPOSE: mirror of check_improvable.sh, but for the NEW `type: brainstorm` note
# class. The Advancer/Improver/Deleter crons all hard-require `^type: project`
# (or `^verified: true`), so brainstorm notes are invisible to them BY DESIGN —
# a brainstorm is thinking-in-progress, not a buildable task, and must not be
# "advanced toward done" or reaped. The Ideator is the ONE cron that acts on them:
# it pressure-tests the thinking (pokes holes, surfaces unverified assumptions,
# proposes the next QUESTION, flags contradictions) and APPENDS to a
# `## CRON CRITIQUE` section — it NEVER mutates the settled-decisions body and
# NEVER drives the note to completion.
#
# Exit 0  => at least one brainstorm note is status: design, not freshly claimed
#            by another job → the Ideator LLM turn should run.
# Exit 1  => nothing to critique → the Ideator LLM turn must NOT run (no LLM cost).
#
# A note qualifies when ALL of:
#   - has  type: brainstorm
#   - has  status: design
#   - is not FRESHLY claimed by another job (live-session <120m, other <30m). Stale = ok.
#     claimed_at is ALWAYS UTC ISO8601. Windows MUST match Advancer + Improver + Deleter.
#
# Anchored (^field:) grep on the WHOLE FILE (not head -n15): claim lines land past
# the frontmatter head on long notes; these bare tokens only appear at column 0 in
# real frontmatter. Same rationale as check_improvable.sh.
#
# Portable: workspace from $OPENCLAW_WORKSPACE (falls back to analyst home).
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
MEM="$WS/memory"
[ -d "$MEM" ] || exit 1

# Re-fire guard: a brainstorm note RESTS at status:design by design (it never
# self-resolves), so a bare exists->exit 0 gate would wake the Ideator LLM every
# */15 tick forever (96x/day) to re-append the same critique or emit NO_REPLY.
# Guard suppresses the wake unless the note BODY changed (hash excludes claim
# lines) or a daily floor elapsed. Same v2 pattern as check_daily_notes.sh.
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gate_refire_guard.sh
source "$SCRIPTS_DIR/_gate_refire_guard.sh"
STATE_FILE="$SCRIPTS_DIR/.check_ideator.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor

now_epoch=$(date -u +%s)
qualifying=()

for f in "$MEM"/_note_*.md; do
  [ -e "$f" ] || continue

  grep -qE '^type:\s*brainstorm\s*$' "$f" || continue
  grep -qE '^status:\s*design\s*$' "$f"   || continue

  # fresh-claim check: skip if another job is actively working it
  claimed_by=$(grep -E '^claimed_by:' "$f" | tail -n1 | sed -E 's/^claimed_by:\s*//' | tr -d '\r' | xargs || true)
  claimed_at=$(grep -E '^claimed_at:' "$f" | tail -n1 | sed -E 's/^claimed_at:\s*//' | tr -d '\r' | xargs || true)
  if [ -n "$claimed_by" ] && [ -n "$claimed_at" ]; then
    if claim_epoch=$(date -u -d "$claimed_at" +%s 2>/dev/null); then
      age=$(( now_epoch - claim_epoch ))
      case "$claimed_by" in
        live-session*) window=7200 ;;   # 120 min (2h) live-session grace
        *)             window=1800 ;;    # 30 min other-claimant grace
      esac
      [ "$age" -lt "$window" ] && continue
    fi
  fi

  # found a critiquable brainstorm note
  qualifying+=("$f")
done

# No qualifying brainstorm notes -> nothing to do.
[ "${#qualifying[@]}" -gt 0 ] || exit 1

# Qualifying notes exist, but only wake the LLM if body changed or floor elapsed.
if refire_should_fire "$STATE_FILE" "$MIN_INTERVAL_SECS" "${qualifying[@]}"; then
  exit 0
fi
exit 1
