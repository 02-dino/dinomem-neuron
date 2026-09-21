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

WS="${OPENCLAW_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SCRIPTS="$WS/scripts"

# git drift-context helper. Best-effort; sourcing is guarded so a missing/broken
# helper can never break the $0 dispatcher (fail-open: workers just find no ctx).
GIT_CTX_ENABLED=0
if [ -f "$SCRIPTS/_gate_git_ctx.sh" ]; then
  # shellcheck source=_gate_git_ctx.sh
  if source "$SCRIPTS/_gate_git_ctx.sh" 2>/dev/null && declare -F write_git_ctx >/dev/null 2>&1; then
    GIT_CTX_ENABLED=1
  fi
fi
# Repo whose recent changes the workers care about (memory notes + artifacts live here).
GIT_CTX_REPO="${GIT_CTX_REPO:-/root/.openclaw}"

# Job IDs (env-injected; install.sh substitutes real values). Empty => skip lane.
ADVANCER_ID="${GATE_ADVANCER_ID:-}"
IMPROVER_ID="${GATE_IMPROVER_ID:-}"
DELETER_ID="${GATE_DELETER_ID:-}"
IDEATOR_ID="${GATE_IDEATOR_ID:-}"
DAILY_NOTE_REVIEW_ID="${GATE_DAILY_NOTE_REVIEW_ID:-}"

# Locate the openclaw CLI (cron command jobs run with the Gateway's PATH, but be safe).
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"

triggered=()

trigger() {
  # $1 = human lane name, $2 = job id
  local name="$1" jobid="$2"
  if [ -z "$jobid" ] || [ -z "$OPENCLAW_BIN" ]; then
    return 0
  fi
  # Fire path ONLY: capture git drift context for this lane before enqueueing the
  # LLM turn. Runs git (hardware only, zero LLM) and is reached solely because a
  # gate already returned exit 0 (genuine work). Idle ticks never get here, so no
  # git and no cost on empty runs. Fully fail-open inside write_git_ctx.
  if [ "$GIT_CTX_ENABLED" -eq 1 ]; then
    write_git_ctx "$name" "$GIT_CTX_REPO" || true
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

# Lane 3b — Brainstorm Ideator: run when a type:brainstorm/status:design note exists.
# Distinct from the project trio: it pressure-tests THINKING (pokes holes, surfaces
# unverified assumptions, proposes the next QUESTION, flags contradictions) and
# APPENDS to a `## CRON CRITIQUE` section — it never advances a note to done and
# never reaps it. check_ideator.sh gates it $0 like the others.
if [ -n "$IDEATOR_ID" ] && [ -f "$SCRIPTS/check_ideator.sh" ]; then
  if bash "$SCRIPTS/check_ideator.sh"; then
    trigger "ideator" "$IDEATOR_ID"
  fi
fi

# Lane 4 — Daily Note Review: run when memory/ has new or active notes.
if [ -n "$DAILY_NOTE_REVIEW_ID" ] && [ -f "$SCRIPTS/check_daily_notes.sh" ]; then
  if bash "$SCRIPTS/check_daily_notes.sh"; then
    trigger "daily-note-review" "$DAILY_NOTE_REVIEW_ID"
  fi
fi

if [ "${#triggered[@]}" -eq 0 ]; then
  echo "NO_REPLY"
else
  echo "cron_gate: triggered ${triggered[*]}"
fi
exit 0
