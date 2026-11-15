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

# INGEST SEATBELT (portable, fail-open): if a known ingest/rebuild process is
# actively running, skip this run entirely rather than risk the Advancer
# verifying/advancing a note against a HALF-BUILT store. check_running_ingest.sh
# is read-only (pgrep patterns only, no host-specific paths) so it ships as-is.
# GUARD is [ -x ]: an install that lacks the helper simply no-ops -> gate
# behaviour unchanged (fail-open). No env/placeholder needed.
INGEST_GATE="$(dirname "$0")/check_running_ingest.sh"
if [ -x "$INGEST_GATE" ] && "$INGEST_GATE"; then
  exit 1
fi

# Re-fire guard: an ACTIVELY advancing project rewrites resume_state/current_step
# every step, so its content hash changes and this still fires immediately (zero
# added latency). But a project PAUSED at a safety gate (awaiting approval) or
# stalled/malformed sits with a FROZEN body -- without this guard that wakes the
# Advancer LLM every */15 tick forever just to re-read the note and emit NO_REPLY
# (the worker already throttles its own MESSAGE to 1/24h, but still burns an LLM
# turn every 15 min to decide that). Guard suppresses the wake unless a note's
# body changed (hash excludes claim lines) or its own daily floor elapsed.
# PER-NOTE (anti-starvation): each qualifying note is tracked INDEPENDENTLY, so a
# busy note that keeps changing can no longer consume a co-qualifying quiet
# note's fire (the aggregate-hash starvation flaw). Fires if ANY note qualifies
# on its own terms; each note's floor elapses independently. Falls back to the
# aggregate guard if the per-note primitive is absent (old base lib).
# Same v2 pattern as check_daily_notes.sh / check_ideator.sh.
# KNOWN BLIND SPOT, BY DESIGN (2026-11-16): a note frozen because its spawned
# `worker:` subagent DIED looks identical to an intentional pause here (measured:
# a ~6h overnight stall). That case is NOT fixed in this guard on purpose -- it is
# owned by the Stall Rescuer lane (check_stalls.sh -> stall_watch.py), which reads
# the session store, releases the dead claim and respawns ONE worker with the
# lesson. Waking the Advancer on the same note as well would put two workers on
# one note (the exact clobber the one-worker-one-note rule forbids).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_gate_refire_guard.sh
source "$SCRIPTS_DIR/_gate_refire_guard.sh"
# _gate_refire_guard.sh is now a thin shim over base's gate_lib.sh, so sensor_*/
# defer_if_busy are available here too. (If a legacy shim without them is present,
# defer_if_busy stays undefined -> the guarded block below no-ops, gate unchanged.)
STATE_FILE="$SCRIPTS_DIR/.check_projects.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor for frozen/stuck notes

qualifying=()
for f in "$WS"/memory/_note_*.md; do
  [ -f "$f" ] || continue
  # Read frontmatter = everything before the first empty line; drop comment lines.
  # More correct than head -N: matches the ACTUAL frontmatter block regardless of
  # its length, and prose/comments below it can never false-match a status line.
  fm="$(awk 'NF==0{exit} /^[[:space:]]*#/{next} {print}' "$f")"
  printf '%s\n' "$fm" | grep -q "^type: project" || continue
  printf '%s\n' "$fm" | grep -qE "^status: (in_progress|pending)" && qualifying+=("$f")
done

[ "${#qualifying[@]}" -gt 0 ] || exit 1

if refire_should_fire "$STATE_FILE" "$MIN_INTERVAL_SECS" "${qualifying[@]}"; then
  # CPU backpressure: the Advancer can drive a HEAVY build (multi-file edits,
  # spawns, git); waking it on an already-saturated box piles latency on real
  # user work. gate_fire_or_defer returns 1 (defer this tick) only when load is
  # over the ceiling. STARVATION-SAFE: refire_should_fire already stamped
  # STATE_FILE, so the daily MIN_INTERVAL floor still elapses -> a permanently
  # busy box eventually advances. fail-open: undefined helper (legacy shim) or
  # unknown sensor -> fires as before. (Shared impl in gate_lib.sh, one place.)
  if declare -F gate_fire_or_defer >/dev/null 2>&1; then
    gate_fire_or_defer "${DINOMEM_ADVANCER_LOAD_CEILING:-2.0}" || exit 1
  fi
  exit 0
fi
exit 1
