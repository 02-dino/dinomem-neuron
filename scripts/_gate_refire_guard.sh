#!/usr/bin/env bash
# _gate_refire_guard.sh — shared re-fire guard for zero-LLM check_*.sh gates.
#
# PROBLEM this solves: a gate that fires (exit 0) whenever a QUALIFYING note
# EXISTS wakes the LLM worker every */15 tick for a note that never self-resolves
# (e.g. a brainstorm note resting at status:design, or a project paused at a
# safety gate). That's 96 LLM wakes/day just to re-confirm "still stuck" and emit
# NO_REPLY. Pure waste.
#
# FIX (same pattern already proven in check_daily_notes.sh v2): before the gate
# returns 0, run the set of qualifying notes through this guard. It suppresses the
# wake unless EITHER:
#   (A) the aggregate CONTENT of the qualifying notes changed since the last real
#       run — hash EXCLUDES the volatile claimed_by/claimed_at lines, so a pure
#       claim refresh does NOT count as a change (that was the v1 self-perpetuating
#       loop bug); OR
#   (b) a min-interval FLOOR has elapsed (so anything time-based still gets a
#       periodic pass — default daily).
#
# USAGE (source it, then call refire_should_fire):
#   source "$(dirname "$0")/_gate_refire_guard.sh"
#   ...build array `qualifying=(f1 f2 ...)` of note paths that passed the gate...
#   if refire_should_fire "<state_file>" "<min_interval_secs>" "${qualifying[@]}"; then
#     exit 0   # real work / floor elapsed -> wake LLM
#   fi
#   exit 1     # unchanged & floor not elapsed -> stay quiet, $0
#
# refire_should_fire writes the new "<epoch> <hash>" stamp to <state_file> ONLY
# when it decides to fire (returns 0), so cadence is measured from actual runs.
# If NO qualifying notes are passed, it returns 1 (nothing to do) and does NOT
# stamp — the caller should normally have exited 1 already in that case.

# content hash of a note EXCLUDING volatile claim lines.
refire__note_body_hash() {
  grep -vE '^claimed_(by|at):' "$1" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1
}

# refire_should_fire <state_file> <min_interval_secs> <note>...
# returns 0 (fire) / 1 (suppress). Stamps state_file only on fire.
refire_should_fire() {
  local state_file="$1"; shift
  local min_interval="$1"; shift
  local -a notes=("$@")
  local now_epoch; now_epoch=$(date -u +%s)

  [ "${#notes[@]}" -gt 0 ] || return 1

  # aggregate content hash over qualifying notes (order-stable via sort).
  local agg_hash
  agg_hash=$(
    for f in "${notes[@]}"; do
      printf '%s %s\n' "$(basename "$f")" "$(refire__note_body_hash "$f")"
    done | sort | sha256sum | cut -d' ' -f1
  )

  local last_run=0 last_hash=""
  if [ -f "$state_file" ]; then
    read -r last_run last_hash < "$state_file" 2>/dev/null || true
    case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
  fi

  # (A) content changed since last real run?
  local changed=1
  if [ "$last_run" -gt 0 ] && [ -n "$last_hash" ]; then
    [ "$agg_hash" != "$last_hash" ] && changed=0
  else
    changed=0   # never run before -> first run fires
  fi

  # (b) min-interval floor elapsed?
  local age=$(( now_epoch - last_run ))
  local due=1
  [ "$age" -ge "$min_interval" ] && due=0

  if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ]; then
    printf '%s %s\n' "$now_epoch" "$agg_hash" > "$state_file" 2>/dev/null || true
    return 0
  fi
  return 1
}
