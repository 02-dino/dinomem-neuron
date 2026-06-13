#!/usr/bin/env bash
# dinomem-neuron — install script
# Extends dinomem with L2 graph, L3 synthesis, L4 promotion, contradiction check, topic index.
# Requires dinomem base installed first.
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-cron] [--force]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
DO_CRON=1
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WS="$2"; shift 2 ;;
    --agent-id)  AGENT_ID="$2"; shift 2 ;;
    --no-cron)   DO_CRON=0; shift ;;
    --force)     FORCE=1; shift ;;
    -h|--help)   grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[fail]\033[0m %s\n' "$*"; exit 1; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

[ -d "$WS" ] || fail "Workspace not found: $WS  (pass --workspace DIR)"

# Check dinomem is installed
[ -f "$WS/procedures/session_reset.py" ] || fail "dinomem base not found in $WS. Install dinomem first: https://github.com/02-dino/dinomem"

if [ -z "$AGENT_ID" ]; then
  AGENT_ID="$(basename "$WS")"
  AGENT_ID="${AGENT_ID#workspace-}"
fi

OPENCLAW_DIR="$(dirname "$WS")"
OPENCLAW_MEMORY_DIR="$OPENCLAW_DIR/memory"
SQLITE_PATH="$OPENCLAW_MEMORY_DIR/$AGENT_ID.sqlite"

echo
hr "dinomem-neuron -> $WS (agent: $AGENT_ID)"

# ── 0) Python dependencies ───────────────────────────────────────────────────
hr "Python dependencies"
python3 -c "import langchain_chroma" 2>/dev/null && skip "langchain-chroma (installed)" || (
  pip install -q langchain-chroma && ok "langchain-chroma" || warn "langchain-chroma install failed"
)
python3 -c "import langchain_huggingface" 2>/dev/null && skip "langchain-huggingface (installed)" || (
  pip install -q langchain-huggingface && ok "langchain-huggingface" || warn "langchain-huggingface install failed"
)
python3 -c "import langchain_core" 2>/dev/null && skip "langchain-core (installed)" || (
  pip install -q langchain-core && ok "langchain-core" || warn "langchain-core install failed"
)
python3 -c "import pandas" 2>/dev/null && skip "pandas (installed)" || (
  pip install -q pandas && ok "pandas" || warn "pandas install failed"
)
python3 -c "import yaml" 2>/dev/null && skip "pyyaml (installed)" || (
  pip install -q pyyaml && ok "pyyaml" || warn "pyyaml install failed"
)
python3 -c "import googleapiclient" 2>/dev/null && skip "google-api-python-client (installed)" || (
  pip install -q google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 && ok "google-api-python-client" || warn "google-api-python-client install failed"
)

# ── 1) Create directories ─────────────────────────────────────────────────────
hr "Directories"
for d in topics kb/memory_neuron/l2_graph kb/memory_neuron/l3_synthesis kb/memory_neuron/l4_promoted; do
  if [ -d "$WS/$d" ]; then skip "$d/ (exists)"; else mkdir -p "$WS/$d"; ok "$d/"; fi
done

# Scaffold topics files
for f in "topics/PERSISTENT.md" "topics/PERSISTENT_AUTO.md"; do
  if [ ! -f "$WS/$f" ]; then
    fname=$(basename "$f" .md)
    printf "# %s\n\n_Auto-managed by dinomem-neuron. Do not edit PERSISTENT_AUTO.md manually._\n" "$fname" > "$WS/$f"
    ok "$f"
  else
    skip "$f (exists)"
  fi
done

# Scaffold memory/_permanent.md (promoted by memory_promote.py)
PERM="$WS/memory/_permanent.md"
if [ ! -f "$PERM" ]; then
  cat > "$PERM" << 'EOF'
# Permanent Lessons & Patterns (auto-pinned) [permanent]

_Transferable lessons/patterns proven durable, promoted by memory_promote.py (L4).
Exempt from cleanup. Searchable via memory_search._

EOF
  ok "memory/_permanent.md"
else
  skip "memory/_permanent.md (exists)"
fi

# Scaffold docs/ folder (for long-form RAG documents)
[ -d "$WS/docs" ] && skip "docs/ (exists)" || (mkdir -p "$WS/docs"; ok "docs/")

# ── 2) Copy scripts ───────────────────────────────────────────────────────────
hr "Copying scripts"
for f in procedures/memory_graph.py procedures/memory_synthesis.py procedures/memory_promote.py procedures/contradiction_check.py procedures/generate_topic_index.py procedures/docs_ingest.py procedures/calendar_sync.py tools/docs_search.py tools/cache_utils.py tools/calendar_tool.py scripts/calendar_oauth_setup.py; do
  dst="$WS/$f"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$f (exists, use --force to overwrite)"
  else
    mkdir -p "$(dirname "$dst")"
    cp "$SKILL_DIR/$f" "$dst"
    sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$dst"
    sed -i "s|DINOMEM_SQLITE_PLACEHOLDER|$SQLITE_PATH|g" "$dst"
    sed -i "s|DINOMEM_OPENCLAW_MEMORY_PLACEHOLDER|$OPENCLAW_MEMORY_DIR|g" "$dst"
    sed -i "s|DINOMEM_HF_CACHE_PLACEHOLDER|$HOME/.cache/huggingface|g" "$dst"
    sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$dst"
    ok "$f"
  fi
done

# ── 3) Register cron jobs ─────────────────────────────────────────────────────
if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # memory_graph — daily at 3:00 UTC (L2)
  GRAPH_CRON="0 3 * * * cd $WS && python3 procedures/memory_graph.py >> logs/memory_graph.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "memory_graph.py"; then
    skip "memory_graph cron (exists)"
  else
    (crontab -l 2>/dev/null; echo "# dinomem-neuron: L2 graph linking"; echo "$GRAPH_CRON") | crontab -
    ok "memory_graph cron (daily 3:00 UTC)"
  fi

  # memory_synthesis + contradiction_check — daily at 3:30 UTC (L3)
  SYNTH_CRON="30 3 * * * cd $WS && python3 procedures/memory_synthesis.py >> logs/memory_synthesis.log 2>&1 && python3 procedures/contradiction_check.py >> logs/contradiction_check.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "memory_synthesis.py"; then
    skip "memory_synthesis cron (exists)"
  else
    (crontab -l 2>/dev/null; echo "# dinomem-neuron: L3 synthesis + contradiction check"; echo "$SYNTH_CRON") | crontab -
    ok "memory_synthesis + contradiction_check cron (daily 3:30 UTC)"
  fi

  # memory_promote + generate_topic_index — weekly Sunday at 4:00 UTC (L4)
  PROMOTE_CRON="0 4 * * 0 cd $WS && python3 procedures/memory_promote.py >> logs/memory_promote.log 2>&1 && python3 procedures/generate_topic_index.py >> logs/topic_index.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "memory_promote.py"; then
    skip "memory_promote cron (exists)"
  else
    (crontab -l 2>/dev/null; echo "# dinomem-neuron: L4 promotion + topic index (weekly)"; echo "$PROMOTE_CRON") | crontab -
    ok "memory_promote + generate_topic_index cron (weekly Sunday 4:00 UTC)"
  fi

  # docs_ingest — daily at 4:00 UTC (RAG long docs)
  DOCS_CRON="0 4 * * * cd $WS && python3 procedures/docs_ingest.py >> logs/docs_ingest.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "docs_ingest.py"; then
    skip "docs_ingest cron (exists)"
  else
    (crontab -l 2>/dev/null; echo "# dinomem-neuron: RAG docs re-ingest (daily)"; echo "$DOCS_CRON") | crontab -
    ok "docs_ingest cron (daily 4:00 UTC)"
  fi

  # calendar_sync — daily at 7:00 UTC (auto-resolve _note_ from past calendar events)
  CAL_CRON="0 7 * * * cd $WS && python3 procedures/calendar_sync.py >> logs/calendar_sync.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "calendar_sync.py"; then
    skip "calendar_sync cron (exists)"
  else
    (crontab -l 2>/dev/null; echo "# dinomem-neuron: calendar sync auto-resolve _note_ (daily)"; echo "$CAL_CRON") | crontab -
    ok "calendar_sync cron (daily 7:00 UTC)"
  fi
fi

echo
hr "done"
echo "  dinomem-neuron installed for agent: $AGENT_ID"
echo "  workspace: $WS"
echo ""
echo "  Next steps:"
echo "  1. Restart OpenClaw:       openclaw gateway restart"
echo "  2. Run first graph build:  python3 $WS/procedures/memory_graph.py"
echo "  3. Run first synthesis:    python3 $WS/procedures/memory_synthesis.py"
echo ""
echo "  Undo: bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
