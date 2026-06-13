#!/usr/bin/env bash
# dinomem-neuron — uninstall script
# Removes cron jobs. Does NOT delete scripts, topics/, or kb/memory_neuron/ data.
#
# Usage: bash scripts/uninstall.sh [--workspace DIR] [--agent-id ID] [--purge]
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WS="$2"; shift 2 ;;
    --agent-id)  shift 2 ;;  # accepted but not needed
    --purge)     PURGE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

echo; hr "dinomem-neuron uninstall"

# Remove cron jobs
hr "Cron jobs"
for pattern in "memory_graph.py" "memory_synthesis.py" "memory_promote.py"; do
  if crontab -l 2>/dev/null | grep -qF "$pattern"; then
    crontab -l 2>/dev/null | grep -v "$pattern" | grep -v "dinomem-neuron" | crontab -
    ok "removed $pattern cron"
  else
    skip "$pattern cron not found"
  fi
done

# Purge scripts (optional)
if [ "$PURGE" = 1 ]; then
  hr "Purge scripts"
  for f in procedures/memory_graph.py procedures/memory_synthesis.py procedures/memory_promote.py procedures/contradiction_check.py procedures/generate_topic_index.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done
  warn "topics/ and kb/memory_neuron/ preserved. Delete manually if needed."
fi

echo; hr "done"
