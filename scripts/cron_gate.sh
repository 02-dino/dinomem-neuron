#!/usr/bin/env bash
# cron_gate.sh — zero-LLM dispatcher for the project-note cron trio.
#
# WHY: Project Advancer / Improver / Verified-Note-Deleter are agentTurn crons.
# An agentTurn fire spins up a full LLM session BEFORE its own "Step 0" gate
# script runs — so the old design paid for an LLM turn every schedule tick
# (~96 + 48 + 48 per day) just to run a bash check that exits 1 ~95% of the time.
#
# This script is registered as a COMMAND cron (payload.kind = shell), which runs
# in the Gateway process with ZERO LLM cost. It runs each cheap check script and
# ONLY when a check reports work (exit 0) does it `openclaw cron run <jobid>` to
# force-enqueue that job's real LLM turn. The three LLM jobs have their own
# schedules DISABLED so they never self-fire; they only ever run when triggered
# here. `openclaw cron run` executes disabled jobs (verified 2026-07-10).
#
# Net effect: idle ticks cost $0 LLM; the LLM only spins up when there is
# genuinely a project note to advance / improve / retire.
#
# Portable: workspace from $OPENCLAW_WORKSPACE; job IDs from env (install.sh fills
# them). If a job-id env var is empty, that lane is skipped silently.
#
# Output contract: prints a short line per lane it TRIGGERED. If it triggered
# nothing, prints exactly NO_REPLY so the command-cron delivery layer suppresses
# any announce. Always exits 0 (a gate that itself errors must not spam failure
# alerts; per-lane failures are logged, not fatal).
set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
SCRIPTS="$WS/scripts"

# Job IDs (env-injected; install.sh substitutes real values). Empty => skip lane.
ADVANCER_ID="${GATE_ADVANCER_ID:-}"
IMPROVER_ID="${GATE_IMPROVER_ID:-}"
DELETER_ID="${GATE_DELETER_ID:-}"

# Locate the openclaw CLI (cron command jobs run with the Gateway's PATH, but be safe).
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"

triggered=()

trigger() {
  # $1 = human lane name, $2 = job id
  local name="$1" jobid="$2"
  if [ -z "$jobid" ] || [ -z "$OPENCLAW_BIN" ]; then
    return 0
  fi
  if "$OPENCLAW_BIN" cron run "$jobid" >/dev/null 2>&1; then
    triggered+=("$name")
  else
    # non-fatal: log to stderr (captured in cron history) but keep going
    echo "cron_gate: failed to trigger $name ($jobid)" >&2
  fi
}

# Lane 1 — Project Advancer: run when an in_progress project note exists.
# (checks invoked via `bash <script>`, so file-exists -f is enough; no +x needed)
if [ -n "$ADVANCER_ID" ] && [ -f "$SCRIPTS/check_projects.sh" ]; then
  if bash "$SCRIPTS/check_projects.sh"; then
    trigger "advancer" "$ADVANCER_ID"
  fi
fi

# Lane 2 — Project Improver: run when a done-but-unverified project note exists.
if [ -n "$IMPROVER_ID" ] && [ -f "$SCRIPTS/check_improvable.sh" ]; then
  if bash "$SCRIPTS/check_improvable.sh"; then
    trigger "improver" "$IMPROVER_ID"
  fi
fi

# Lane 3 — Verified Note Deleter: run when a verified:true note exists.
if [ -n "$DELETER_ID" ] && [ -f "$SCRIPTS/check_deletable.sh" ]; then
  if bash "$SCRIPTS/check_deletable.sh"; then
    trigger "deleter" "$DELETER_ID"
  fi
fi

if [ "${#triggered[@]}" -eq 0 ]; then
  echo "NO_REPLY"
else
  echo "cron_gate: triggered ${triggered[*]}"
fi
exit 0
