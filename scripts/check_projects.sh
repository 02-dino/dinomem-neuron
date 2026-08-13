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

# Re-fire guard: an ACTIVELY advancing project rewrites resume_state/current_step
# every step, so its content hash changes and this still fires immediately (zero
# added latency). But a project PAUSED at a safety gate (awaiting approval) or
# stalled/malformed sits with a FROZEN body -- without this guard that wakes the
# Advancer LLM every */15 tick forever just to re-read the note and emit NO_REPLY
# (the worker already throttles its own MESSAGE to 1/24h, but still burns an LLM
# turn every 15 min to decide that). Guard suppresses the wake unless the
# aggregate body changed (hash excludes claim lines) or a daily floor elapsed.
# Same v2 pattern as check_daily_notes.sh / check_ideator.sh.
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
  head -10 "$f" | grep -q "^type: project" || continue
  head -10 "$f" | grep -qE "^status: (in_progress|pending)" && qualifying+=("$f")
done

[ "${#qualifying[@]}" -gt 0 ] || exit 1

if refire_should_fire "$STATE_FILE" "$MIN_INTERVAL_SECS" "${qualifying[@]}"; then
  # CPU backpressure (first live gate_lib Layer-B/C consumer): the Advancer can
  # drive a HEAVY build step (multi-file edits, spawns, git). Waking it while the
  # box is already saturated piles latency on real user work. If load is over the
  # ceiling, DEFER this tick -- skip the LLM wake, try again next */15.
  # NO STARVATION: refire_should_fire ABOVE already stamped STATE_FILE, so the
  # daily MIN_INTERVAL floor still elapses regardless of how many ticks we defer
  # -> a permanently-busy box still eventually advances (spec Layer-C contract).
  # fail-open: if defer_if_busy is undefined (legacy shim) or the sensor is
  # unknown, we FALL THROUGH and fire as before -- backpressure never blocks a
  # real signal, it only smooths it.
  if declare -F defer_if_busy >/dev/null 2>&1; then
    ceiling="${DINOMEM_ADVANCER_LOAD_CEILING:-2.0}"
    if ! defer_if_busy "$(sensor_load_ratio)" "$ceiling"; then
      exit 1   # box busy -> defer to next tick (interval floor guarantees eventual run)
    fi
  fi
  exit 0
fi
exit 1
