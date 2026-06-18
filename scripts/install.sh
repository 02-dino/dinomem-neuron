#!/usr/bin/env bash
# dinomem-neuron — install script
# Extends dinomem with L2 graph, L3 synthesis, L4 promotion, contradiction check, topic index.
# Requires dinomem base installed first.
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-cron] [--force] [--agree]
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
    --agree)     LICENSE_AGREED=1; shift ;;
    -h|--help)   grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

LICENSE_AGREED="${LICENSE_AGREED:-0}"

# ── License agreement ───────────────────────────────────────────────────────────────
if [ "$LICENSE_AGREED" != "1" ]; then
  echo ""
  echo "  ────────────────────────────────────────────────────────────"
  echo "  LICENSE — dinomem-neuron"
  echo "  ────────────────────────────────────────────────────────────"
  echo ""
  cat "$SKILL_DIR/LICENSE"
  echo ""
  echo "  ────────────────────────────────────────────────────────────"
  read -p "  Type 'agree' to accept the license and continue: " USER_AGREE
  if [ "$USER_AGREE" != "agree" ]; then
    echo "  License not accepted. Exiting."
    echo "  To skip this prompt (e.g. for AI agent installs): bash install.sh --agree --workspace ..."
    exit 1
  fi
fi

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

# ── Pre-flight compatibility checks ─────────────────────────────────────────
hr "Pre-flight checks"
# dinomem base must be installed + version check
DINOMEM_MIN_VERSION="1.0.0"
if [ ! -f "$WS/procedures/extract_memory.py" ]; then
  warn "dinomem base not detected (extract_memory.py missing) — install dinomem first."
  warn "  bash dinomem/scripts/install.sh --workspace $WS --agent-id $AGENT_ID"
else
  ok "dinomem base detected"
  # Check VERSION file
  DINOMEM_VERSION_FILE="$(dirname "$WS")/dinomem/VERSION"
  if [ ! -f "$DINOMEM_VERSION_FILE" ]; then
    # Try SKILL_DIR sibling
    DINOMEM_VERSION_FILE="$(dirname "$SKILL_DIR")/dinomem/VERSION"
  fi
  if [ -f "$DINOMEM_VERSION_FILE" ]; then
    DINOMEM_VERSION=$(cat "$DINOMEM_VERSION_FILE" | tr -d '[:space:]')
    # Simple semver compare (major.minor.patch)
    version_gte() {
      printf '%s\n%s' "$1" "$2" | sort -V -C
    }
    if version_gte "$DINOMEM_VERSION" "$DINOMEM_MIN_VERSION"; then
      ok "dinomem version $DINOMEM_VERSION (>= $DINOMEM_MIN_VERSION required)"
    else
      warn "dinomem version $DINOMEM_VERSION is below minimum required $DINOMEM_MIN_VERSION — update dinomem first."
      warn "  bash dinomem/scripts/update.sh --workspace $WS --agent-id $AGENT_ID"
    fi
  else
    warn "dinomem VERSION file not found — cannot verify version. Ensure dinomem >= $DINOMEM_MIN_VERSION is installed."
  fi
fi
# Existing neuron data
if [ -d "$WS/kb/memory_neuron" ] && [ "$(ls -A "$WS/kb/memory_neuron" 2>/dev/null)" ]; then
  warn "kb/memory_neuron/ already exists — existing neuron data will be preserved (not overwritten)"
else
  ok "kb/memory_neuron/ clear"
fi
# Existing RAG vector DB
if [ -d "$WS/kb/vector_db_docs" ] && [ "$(ls -A "$WS/kb/vector_db_docs" 2>/dev/null)" ]; then
  warn "kb/vector_db_docs/ already exists — dinomem-neuron RAG will write to this path. Back up if needed."
else
  ok "kb/vector_db_docs/ clear"
fi
# Existing MEMORY.md
if [ -f "$WS/MEMORY.md" ] && [ -s "$WS/MEMORY.md" ]; then
  warn "MEMORY.md already exists and has content."
  warn "dinomem-neuron's generate_topic_index.py will REWRITE it on first weekly run (Sunday 4:00 UTC)."
  warn "Your existing index will be replaced with neuron-curated content. Raw memory/*.md files are untouched."
fi

# Existing AGENTS.md neuron block
if [ -f "$WS/AGENTS.md" ] && grep -qF "rag_long_docs" "$WS/AGENTS.md" 2>/dev/null; then
  warn "AGENTS.md already has a rag_long_docs section — neuron block will be appended. Check for duplicates after install."
fi
# Root files size check
ROOT_FILES="AGENTS.md SOUL.md IDENTITY.md TOOLS.md USER.md"
TOTAL_CHARS=0
for rf in $ROOT_FILES; do
  if [ -f "$WS/$rf" ]; then
    RF_SIZE=$(wc -c < "$WS/$rf")
    TOTAL_CHARS=$((TOTAL_CHARS + RF_SIZE))
    if [ "$RF_SIZE" -gt 15000 ]; then
      warn "$rf is ${RF_SIZE} chars — exceeds 15k. Trim now: remove outdated or redundant sections."
    elif [ "$RF_SIZE" -gt 10000 ]; then
      warn "$rf is ${RF_SIZE} chars — approaching 15k limit. Consider trimming soon."
    fi
  fi
done
if [ "$TOTAL_CHARS" -gt 60000 ]; then
  warn "Total root files: ${TOTAL_CHARS} chars — exceeds maxBootstrapTotalChars (60000). Some files won't be fully injected."
elif [ "$TOTAL_CHARS" -gt 50000 ]; then
  warn "Total root files: ${TOTAL_CHARS} chars — approaching maxBootstrapTotalChars (60000). Consider trimming soon."
else
  ok "Root files: ${TOTAL_CHARS} chars total — within limits"
fi

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
for f in procedures/memory_graph.py procedures/memory_synthesis.py procedures/memory_promote.py procedures/contradiction_check.py procedures/generate_topic_index.py procedures/docs_ingest.py procedures/session_ingest.py procedures/calendar_sync.py tools/docs_search.py tools/session_search.py tools/cache_utils.py tools/calendar_tool.py scripts/calendar_oauth_setup.py; do
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
# upsert_cron: add or update a cron entry by script keyword
upsert_cron() {
  local keyword="$1" comment="$2" cron_line="$3" label="$4"
  local existing
  existing=$(crontab -l 2>/dev/null | grep "$keyword" || true)
  if [ "$existing" = "$cron_line" ]; then
    skip "$label (exists, up to date)"
  elif [ -n "$existing" ]; then
    { crontab -l 2>/dev/null | grep -v "$keyword"; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (updated)"
  else
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (registered)"
  fi
}

if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # memory_graph — daily at 3:00 UTC (L2)
  GRAPH_CRON="0 3 * * * cd $WS && python3 procedures/memory_graph.py >> logs/memory_graph.log 2>&1"
  upsert_cron "memory_graph.py" "dinomem-neuron: L2 graph linking" "$GRAPH_CRON" "memory_graph cron (daily 3:00 UTC)"

  # memory_synthesis + contradiction_check — daily at 3:30 UTC (L3)
  SYNTH_CRON="30 3 * * * cd $WS && python3 procedures/memory_synthesis.py >> logs/memory_synthesis.log 2>&1 && python3 procedures/contradiction_check.py >> logs/contradiction_check.log 2>&1"
  upsert_cron "memory_synthesis.py" "dinomem-neuron: L3 synthesis + contradiction check" "$SYNTH_CRON" "memory_synthesis + contradiction_check cron (daily 3:30 UTC)"

  # memory_promote + generate_topic_index — weekly Sunday at 4:00 UTC (L4)
  PROMOTE_CRON="0 4 * * 0 cd $WS && python3 procedures/memory_promote.py >> logs/memory_promote.log 2>&1 && python3 procedures/generate_topic_index.py >> logs/topic_index.log 2>&1"
  upsert_cron "memory_promote.py" "dinomem-neuron: L4 promotion + topic index (weekly)" "$PROMOTE_CRON" "memory_promote + generate_topic_index cron (weekly Sunday 4:00 UTC)"

  # docs_ingest — daily at 4:00 UTC (RAG long docs)
  DOCS_CRON="0 4 * * * cd $WS && python3 procedures/docs_ingest.py >> logs/docs_ingest.log 2>&1"
  upsert_cron "docs_ingest.py" "dinomem-neuron: RAG docs re-ingest (daily)" "$DOCS_CRON" "docs_ingest cron (daily 4:00 UTC)"
  # session_ingest is triggered automatically by auto_session_reset.py (every 15 min)
  # No separate cron needed.

  # calendar_sync — daily at 7:00 UTC (auto-resolve _note_ from past calendar events)
  CAL_CRON="0 7 * * * cd $WS && python3 procedures/calendar_sync.py >> logs/calendar_sync.log 2>&1"
  upsert_cron "calendar_sync.py" "dinomem-neuron: calendar sync auto-resolve _note_ (daily)" "$CAL_CRON" "calendar_sync cron (daily 7:00 UTC)"
fi

# ── Wire AGENTS.md ───────────────────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
if [ -f "$AGENTS" ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$AGENTS" >/dev/null 2>&1 && ok "AGENTS.md backed up" || warn "AGENTS.md backup failed — continuing"
fi
if [ -f "$WS/MEMORY.md" ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$WS/MEMORY.md" >/dev/null 2>&1 && ok "MEMORY.md backed up" || warn "MEMORY.md backup failed — continuing"
fi
BEGIN="<!-- BEGIN:dinomem-neuron (managed) -->"
END="<!-- END:dinomem-neuron -->"
BLOCK="$BEGIN
## rag_long_docs
  when: long doc shared | save/remember doc | content >2000 chars
  action: save docs/<slug>.md → python3 procedures/docs_ingest.py --file docs/<slug>.md → confirm
  note: auto-re-ingested daily 4:00 UTC; never save long docs to memory/

## calendar_integration
  trigger: explicit date/time | deadline | appointment
  workflow: save _note_<slug>.md → extract date/time → ask if missing → python3 tools/calendar_tool.py create "<title>" --date <YYYY-MM-DD> [--time HH:MM] --note _note_<slug>.md [--reminder popup:30] [--attendee email] → confirm
  skip_if: no date/time implied | user says no calendar

## session_deep_recall
  trigger: memory_search returns thin/uncertain result (few hits, [uncertain] tag, or low confidence) AND memory file date < 7 days old AND user intent requires detail
  action: python3 tools/session_search.py "<query>" --k 5
  skip: do not call session_search every turn — only when memory_search result is clearly insufficient
$END"

touch "$AGENTS"
if grep -qF "$BEGIN" "$AGENTS" 2>/dev/null; then
  skip "AGENTS.md already wired (use --force to refresh)"
else
  printf '\n%s\n' "$BLOCK" >> "$AGENTS"
  ok "AGENTS.md wired"
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
echo "  ⚠️  Minimum memory requirement"
echo "  Neuron needs at least a few memory/*.md files to be meaningful."
echo "  If you just installed dinomem, run a few sessions first before expecting synthesis results."
echo ""
echo "  ⚠️  First run takes time"
echo "  memory_graph.py and memory_synthesis.py can take several minutes on first run"
echo "  depending on how many memory files exist. This is normal."
echo ""
echo "  ⚠️  Calendar integration (optional)"
echo "  To link _note_ reminders to Google Calendar, OAuth setup is required."
echo "  Guide: see '## Calendar setup' in README"
echo ""
echo "  License: Proprietary — unauthorized redistribution prohibited. https://t.me/dinotlgrm"
echo "  Undo: bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
