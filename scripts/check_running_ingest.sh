#!/usr/bin/env bash
# check_running_ingest.sh — Advancer running-ingest seatbelt (fix B).
#
# WHY: a live-session claim can go stale mid-rebuild (>120min) even with the
# auto-refresh hook (fix A) in place if the hook itself is down, misconfigured,
# or the rebuild simply outlives the freshness window. If Advancer verifies a
# store/note while a real ingest/rebuild process is still writing to it, it can
# mark a HALF-BUILT store as done (proven risk, see
# memory/_note_auto-refresh-claim-hook.md WHY section). This is the seatbelt:
# regardless of claim freshness, if a matching ingest/rebuild process is
# currently running, treat the associated note/store as NOT safe to verify yet.
#
# SCOPE — THIS AGENT ONLY (bugfix 2026-11-05):
#   The seatbelt protects THIS agent's store. It must therefore match only
#   ingest processes belonging to THIS workspace.
#   Previously the pgrep was GLOBAL: on a shared box it matched any agent's
#   ingest and blocked this agent's Advancer for reasons that had nothing to do
#   with this agent's store.
#   MEASURED 2026-11-05 on a 13-agent box: session_ingest is staggered roughly
#   every 15 min PER AGENT, so across 13 agents some ingest is almost always
#   running -> 12/12 probes over 60s returned BLOCK, and the Advancer had not
#   fired in 7.4h. A seatbelt that is always on is not a seatbelt, it is a stall.
#   Attribution ladder (first match wins, deterministic, no guessing):
#     1. process cmdline contains this workspace path   -> OURS
#     2. /proc/<pid>/cwd resolves to this workspace      -> OURS
#     3. neither                                         -> SOMEONE ELSE's, ignored
#   Rule 2 exists because a bare `python3 procedures/docs_ingest.py` carries no
#   path in argv; its cwd is still definitive. Unattributable == not ours: an
#   ingest we cannot tie to this workspace cannot be writing this workspace's
#   store, and defaulting to "block" is what produced the box-wide stall.
#
# USAGE:
#   check_running_ingest.sh               # exit 0 if an ingest for THIS workspace is running, 1 if none
#   check_running_ingest.sh --list        # print matching process lines (for diagnostics), same exit codes
#   check_running_ingest.sh --why         # like --list but also prints the scope being enforced
#
# Zero external deps beyond pgrep (procps). Safe to call from cron or a live
# agent. Read-only — never kills or signals anything.
set -euo pipefail

PATTERN='session_ingest\.py|docs_ingest\.py|ingest_docs_data\.py|ingest_docs_procedures\.py|reindex_all\.sh|migrate_embeddings\.sh|memory (index|reindex).*--force'

mode="${1:-}"

# Workspace identity: explicit env wins, else derive from this script's location
# (scripts/ lives directly under the workspace root).
WS="${OPENCLAW_WORKSPACE:-}"
if [ -z "$WS" ]; then
  WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || echo "")"
fi
# Strip a trailing slash so substring matching cannot mis-compare.
WS="${WS%/}"

raw="$(pgrep -af "$PATTERN" 2>/dev/null | grep -v "check_running_ingest.sh" || true)"

matches=""
if [ -n "$raw" ] && [ -n "$WS" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pid="${line%% *}"
    case "$pid" in (''|*[!0-9]*) continue ;; esac

    # 1) workspace path present in the command line -> ours
    case "$line" in
      *"$WS"*) matches="${matches}${line}"$'\n'; continue ;;
    esac

    # 2) no path in argv -> cwd is still definitive
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ -n "$cwd" ] && [ "${cwd%/}" = "$WS" ]; then
      matches="${matches}${line}"$'\n'
    fi

    # 3) otherwise: another agent's ingest -> not our concern
  done <<< "$raw"
fi

matches="$(printf '%s' "$matches" | sed '/^$/d')"

if [ "$mode" = "--why" ]; then
  echo "scope: $WS"
  echo "pattern-matched (box-wide): $(printf '%s' "$raw" | sed '/^$/d' | wc -l)"
  echo "attributed to this workspace: $(printf '%s' "$matches" | sed '/^$/d' | wc -l)"
  [ -n "$matches" ] && printf '%s\n' "$matches"
  [ -n "$matches" ] && exit 0 || exit 1
fi

if [ "$mode" = "--list" ]; then
  if [ -n "$matches" ]; then
    printf '%s\n' "$matches"
    exit 0
  else
    exit 1
  fi
fi

if [ -n "$matches" ]; then
  exit 0
else
  exit 1
fi
