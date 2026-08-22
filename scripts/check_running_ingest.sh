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
# USAGE:
#   check_running_ingest.sh               # exit 0 if ANY known ingest process is running, 1 if none
#   check_running_ingest.sh --list        # print matching process lines (for diagnostics), same exit codes
#
# PATTERN LIST (extend as new ingest/rebuild scripts are added):
#   session_ingest.py, docs_ingest.py, ingest_docs_data.py,
#   ingest_docs_procedures.py, reindex_all.sh, migrate_embeddings.sh,
#   memory reindex (openclaw memory index --force)
#
# Zero external deps beyond pgrep (procps). Safe to call from cron or a live
# agent. Read-only — never kills or signals anything.
set -euo pipefail

PATTERN='session_ingest\.py|docs_ingest\.py|ingest_docs_data\.py|ingest_docs_procedures\.py|reindex_all\.sh|migrate_embeddings\.sh|memory (index|reindex).*--force'

mode="${1:-}"

matches="$(pgrep -af "$PATTERN" 2>/dev/null | grep -v "check_running_ingest.sh" || true)"

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
