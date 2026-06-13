#!/usr/bin/env bash
# dinomem-neuron — uninstall script
# Removes: cron jobs added by neuron.
# Does NOT touch dinomem base config or memory data.
#
# Usage:
#   bash scripts/uninstall.sh --workspace DIR
#   bash scripts/uninstall.sh --workspace DIR --purge         # also remove scripts
#   bash scripts/uninstall.sh --workspace DIR --purge-data    # also remove kb/, topics/, docs/
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
PURGE=0
PURGE_DATA=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)   WS="$2"; shift 2 ;;
    --agent-id)    shift 2 ;;  # accepted, not needed
    --purge)       PURGE=1; shift ;;
    --purge-data)  PURGE_DATA=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

echo; hr "dinomem-neuron uninstall -> $WS"

# ── Cron jobs ─────────────────────────────────────────────────────────────────
hr "Cron jobs"
CRON_PATTERNS=(
  "memory_graph.py"
  "memory_synthesis.py"
  "contradiction_check.py"
  "memory_promote.py"
  "generate_topic_index.py"
  "docs_ingest.py"
  "calendar_sync.py"
  "dinomem-neuron:"
)
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CURRENT_CRON"
for pattern in "${CRON_PATTERNS[@]}"; do
  if echo "$NEW_CRON" | grep -qF "$pattern"; then
    NEW_CRON="$(echo "$NEW_CRON" | grep -vF "$pattern")"
    ok "removed cron: $pattern"
  else
    skip "cron not found: $pattern"
  fi
done
echo "$NEW_CRON" | crontab - 2>/dev/null || true

# ── Purge scripts (optional) ──────────────────────────────────────────────────
if [ "$PURGE" = 1 ]; then
  hr "Purge scripts"
  for f in \
    procedures/memory_graph.py \
    procedures/memory_synthesis.py \
    procedures/memory_promote.py \
    procedures/contradiction_check.py \
    procedures/generate_topic_index.py \
    procedures/docs_ingest.py \
    procedures/calendar_sync.py \
    tools/docs_search.py \
    tools/cache_utils.py \
    tools/calendar_tool.py \
    scripts/calendar_oauth_setup.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done
fi

# ── Purge data (optional, explicit) ───────────────────────────────────────────
if [ "$PURGE_DATA" = 1 ]; then
  hr "Purge data"
  warn "This will permanently delete neuron knowledge data."
  read -r -p "  Type 'yes' to confirm: " confirm
  if [ "$confirm" = "yes" ]; then
    [ -d "$WS/kb/memory_neuron" ] && rm -rf "$WS/kb/memory_neuron" && ok "removed kb/memory_neuron/"
    [ -d "$WS/topics" ]           && rm -rf "$WS/topics"           && ok "removed topics/"
    [ -d "$WS/kb/vector_db_docs" ] && rm -rf "$WS/kb/vector_db_docs" && ok "removed kb/vector_db_docs/"
    [ -d "$WS/docs" ]             && warn "docs/ preserved — delete manually if needed"
  else
    skip "purge-data cancelled"
  fi
fi

echo; hr "done"
echo "  dinomem base remains installed. Run 'openclaw gateway restart' if needed."
