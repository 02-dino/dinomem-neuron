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
    --workspace)      WS="$2"; shift 2 ;;
    --agent-id)       AGENT_ID="$2"; shift 2 ;;
    --no-cron)        DO_CRON=0; shift ;;
    --force)          FORCE=1; shift ;;
    --agree)          LICENSE_AGREED=1; shift ;;
    -h|--help)        grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
  # If stdin is not a TTY (piped / CI / AI-agent install), do not block on read.
  # Fail fast with the exact flag to pass instead of hanging forever.
  if [ ! -t 0 ]; then
    echo "  License not accepted and no terminal to prompt on." >&2
    echo "  ACTION[agent]: ask the OWNER to accept the dinomem-neuron license, then re-run with --agree (or set LICENSE_AGREED=1). License acceptance is the human's decision — do not auto-agree." >&2
    exit 1
  fi
  echo "  This is a SINGLE-BUYER license. This copy is watermarked and traceable to you."
  echo "  Redistribution or resale is prohibited and forfeits updates + support."
  echo ""
  read -p "  Type 'agree' to accept the license and continue: " USER_AGREE
  if [ "$USER_AGREE" != "agree" ]; then
    echo "  License not accepted. Exiting."
    echo "  To skip this prompt (e.g. for AI agent installs): bash install.sh --agree --workspace ..."
    exit 1
  fi
fi

# ── License expiry check (time-limited builds only) ──────────────────────────
if [ -f "$SKILL_DIR/scripts/license_check.sh" ]; then
  bash "$SKILL_DIR/scripts/license_check.sh" || exit 1
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
# Agent sessions dir — where session_reset.py archives *.archived.*.jsonl in
# place. session_ingest.py reads from here (same path the base dinomem installer
# uses). Must match, or the session-search index never refreshes.
SESSIONS_DIR="$OPENCLAW_DIR/agents/$AGENT_ID/sessions"
SQLITE_PATH="$OPENCLAW_MEMORY_DIR/$AGENT_ID.sqlite"

echo
hr "dinomem-neuron -> $WS (agent: $AGENT_ID)"

# ── Pre-flight compatibility checks ─────────────────────────────────────────
hr "Pre-flight checks"
# Python version check
if ! command -v python3 &>/dev/null; then
  warn "python3 not found — attempting install..."
  if command -v brew &>/dev/null; then
    brew install python3 && ok "python3 installed (brew)" || warn "python3 install failed — install manually: https://python.org"
  elif command -v apt-get &>/dev/null; then
    apt-get install -y software-properties-common 2>/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    apt-get update -q && apt-get install -y python3.12 python3.12-venv python3-pip \
      && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
      && ok "python3.12 installed (deadsnakes)" \
      || warn "python3 install failed — install manually: https://python.org"
  elif command -v curl &>/dev/null; then
    curl https://pyenv.run | bash \
      && export PATH="$HOME/.pyenv/bin:$PATH" \
      && pyenv install 3.12 && pyenv global 3.12 \
      && ok "python3.12 installed (pyenv)" \
      || warn "pyenv install failed — install python3 manually: https://python.org"
  else
    warn "No package manager found — install python3 manually: https://python.org"
  fi
fi
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
  warn "Python $PY_VERSION detected — dinomem-neuron requires Python 3.8+. Upgrade before continuing."
else
  ok "Python $PY_VERSION"
fi
# Workspace writable check
if [ ! -d "$WS" ]; then
  warn "Workspace '$WS' does not exist — create it first or pass correct --workspace path."
elif [ ! -w "$WS" ]; then
  warn "Workspace '$WS' is not writable — fix permissions before installing."
else
  ok "Workspace writable: $WS"
fi
# dinomem base must be installed + version check.
# Floor is 1.2.0: that's where base's call_llm gained reasoning/thinking routing
# (--thinking + --model). neuron's reasoning scripts (synthesis/contradiction/
# promote) pass reasoning=True; on older base the flag is accepted but ignored
# (silently degraded — no thinking budget). Enforce the floor so install order
# (base first, often an old base) can't quietly weaken reasoning quality.
DINOMEM_MIN_VERSION="1.2.0"
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
# RAG document-extraction tools (optional — enable PDF/Office ingest).
# pdftotext (poppler-utils) handles PDF; pandoc handles docx/rtf/odt/html/epub.
# Missing → plain text/markdown ingest still works; only PDF/Office is gated.
# Mirrors the python3 auto-install pattern above: detect PM → install → warn-fallback.
# Never hard-fails (warn, not fail) — the feature degrades, it does not block install.
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo &>/dev/null && SUDO="sudo"
pm_install() { # $@ = packages; prints command, runs via detected PM, returns nonzero on failure
  if   command -v apt-get &>/dev/null; then $SUDO apt-get update -q && $SUDO apt-get install -y "$@"
  elif command -v dnf     &>/dev/null; then $SUDO dnf install -y "$@"
  elif command -v yum     &>/dev/null; then $SUDO yum install -y "$@"
  elif command -v pacman  &>/dev/null; then $SUDO pacman -S --noconfirm "$@"
  elif command -v apk     &>/dev/null; then $SUDO apk add "$@"
  elif command -v brew    &>/dev/null; then brew install "$@"
  else return 127; fi
}
if command -v pdftotext &>/dev/null; then ok "pdftotext present — PDF ingest enabled"; else
  warn "pdftotext missing — PDF ingest needs it. Attempting install (poppler-utils)..."
  # poppler-utils on apt/dnf/yum/apk; 'poppler' on pacman/brew
  if   command -v pacman &>/dev/null || command -v brew &>/dev/null; then pm_install poppler
  else pm_install poppler-utils; fi \
    && command -v pdftotext &>/dev/null && ok "pdftotext installed" \
    || warn "  could not auto-install — PDF ingest disabled until you install poppler-utils/poppler. Text/Markdown ingest still works."
fi
if command -v pandoc &>/dev/null; then ok "pandoc present — docx/rtf/odt/html/epub ingest enabled"; else
  warn "pandoc missing — Office/HTML ingest needs it. Attempting install..."
  pm_install pandoc \
    && command -v pandoc &>/dev/null && ok "pandoc installed" \
    || warn "  could not auto-install — docx/Office ingest disabled until you install pandoc (https://pandoc.org/installing.html). Text/Markdown + PDF ingest still work."
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
    if [ "$RF_SIZE" -gt 20000 ]; then
      warn "$rf is ${RF_SIZE} chars — exceeds maxBootstrapFileChars (20000). Content beyond limit won't be injected."
      warn "  Trim $rf: remove outdated or redundant sections to keep it lightweight."
    elif [ "$RF_SIZE" -gt 15000 ]; then
      warn "$rf is ${RF_SIZE} chars — getting large. Consider trimming soon."
    elif [ "$RF_SIZE" -gt 10000 ]; then
      warn "$rf is ${RF_SIZE} chars — approaching 15k. Keep an eye on size."
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
  # license_check.py must sit beside procedures so the runtime guard can import it.
  true
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
    sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$dst"
    ok "$f"
  fi
done

# license_check.py -> workspace procedures/ (runtime guard import target)
cp "$SKILL_DIR/lib/license_check.py" "$WS/procedures/license_check.py"
sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$WS/procedures/license_check.py"
ok "procedures/license_check.py (runtime license guard)"

# .license_expiry -> workspace root (time-limited builds only; perpetual builds omit it)
if [ -f "$SKILL_DIR/.license_expiry" ]; then
  cp "$SKILL_DIR/.license_expiry" "$WS/.license_expiry"
  ok ".license_expiry (license term)"
fi

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

  # project_advancer — OpenClaw agentTurn cron (NOT shell crontab): auto-advance type:project in_progress notes
  # one step per run, unattended, announces progress. Premium: only neuron installs this.
  PROJ_ADV_CHECK=$(python3 -c "
import subprocess, json, sys
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    exists = any('project' in j.get('name','').lower() and 'advancer' in j.get('name','').lower() for j in (jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])))
    print('exists' if exists else 'missing')
except: print('skip')
" 2>/dev/null)
  if [ "$PROJ_ADV_CHECK" = "exists" ]; then
    skip "project_advancer OpenClaw cron (exists)"
  elif [ "$PROJ_ADV_CHECK" = "skip" ]; then
    warn "Could not check OpenClaw cron — add Project Advancer cron manually via OpenClaw"
  else
    python3 - <<PYEOF
import subprocess, json
job = {
    "name": "Project Advancer",
    "schedule": {"kind": "cron", "expr": "*/15 * * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        "message": "Project Advancer. Auto-advance step-by-step project notes unattended, announce progress.\n\nStep 0: PRE-CHECK (zero LLM cost gate). Run: bash $WS/scripts/check_projects.sh
If exit code != 0 → no in_progress project notes exist. Stop immediately, no announce.

Step 1: ls memory/_note_*.md in $WS/memory/. Read each. Select only notes with type: project AND status: in_progress.\n\nStep 1b — BUDGET CHECK (pre-task estimation): Before advancing any project, call session_status to get current context%. For each candidate project, estimate token cost: (number of files to read × 500) + (remaining steps × 1000). If estimated cost + current context% > 80% of budget, skip that project this run and announce it was skipped due to context budget. Only advance projects that fit within budget.\n\nStep 2 — CLAIM CHECK (atomic task claiming): For each in_progress note, read claimed_by and claimed_at fields. If claimed_by is set AND claimed_at is less than 10 minutes ago, SKIP this note entirely (another Advancer instance is working it). If claimed_at is older than 10 minutes, the claim is stale — clear claimed_by and claimed_at, then proceed. If claimed_by is absent, proceed.\n\nStep 3 — CLAIM WRITE: Before executing any step, write claimed_by: <this run's cron run_id or a short uuid> and claimed_at: <current ISO timestamp> to the note. Re-read the note immediately after writing to verify the claim was written correctly. If the re-read shows a different claimed_by (race condition), abort this note and move on.\n\nStep 4: For each claimed in_progress project note, pick the lowest unchecked step at current_step. Resume from resume_state ONLY (the note is self-sufficient; do not rely on chat history).\n\nStep 5: SAFETY GATE before executing the step. If the step is destructive or external (deletes data, git push, sends external messages, irreversible writes outside workspace, spends money, touches credentials), DO NOT execute it. Leave it pending, announce the project is paused awaiting user approval at step n/m — tag the owner in the announcement so they are notified (use platform-appropriate mention format), move to the next project.\n\nStep 6: Execute the one current step. After it: verify the step done_when. If met, mark [x], advance current_step, REWRITE resume_state with what the next step needs (paths, decisions, assumptions). If not met, leave pending and record why in resume_state.\n\nStep 7: Context guard. Check session_status context%. If over 60%, checkpoint (state already written), CLEAR claimed_by and claimed_at from the note, and stop advancing this run. Otherwise continue to the next step of the same project until done or context pressure.\n\nStep 8 — CLAIM RELEASE: After completing or checkpointing a project (step done, context limit hit, or safety gate pause), clear claimed_by and claimed_at from the note before moving to the next project.\n\nStep 9: When a project done_when (all steps [x] / final artifact) is met, flip status to done, clear claimed_by and claimed_at. Daily Note Review will delete/promote it.\n\nStep 10: ANNOUNCE only on state change. Rules:\n- Step advanced or completed → announce with progress bar\n- First time hitting safety gate → announce once, tag owner (use platform-appropriate mention)\n- Already paused (resume_state contains 'awaiting approval') → silent\n- Malformed or stalled → announce only if last_flagged not set in resume_state OR last_flagged > 24h ago; write last_flagged: <ISO timestamp> to resume_state after announcing\n- No state change → silent\nProgress bar: [████░░░░░░] 4/10 40% — project name, step n/m. Short, group-chat style.",
        "timeoutSeconds": 600
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
r = subprocess.run(['openclaw', 'cron', 'add', '--json', json.dumps(job)], capture_output=True, text=True, timeout=15)
if r.returncode == 0:
    print("  \033[32m[ok]\033[0m   project_advancer OpenClaw cron registered (every 4h)")
else:
    print(f"  \033[33m[warn]\033[0m Could not register project_advancer cron: {r.stderr[:100]}")
PYEOF
  fi
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
# Body is a literal quoted heredoc: embedded double-quotes ("<title>", "<query>")
# and <angle-bracket> placeholders must NOT be shell-evaluated. Markers wrapped after.
DINOMEM_BODY=$(cat <<'DINOMEM_NEURON_AGENTS_BODY'
## rag_long_docs
  when: long doc shared | doc file uploaded (pdf/docx/rtf/odt/html/epub/md/txt) | save/remember doc | content >2000 chars
  action:
    uploaded_file: python3 procedures/docs_ingest.py --file <media path> → confirm  # script self-extracts pdf/docx/etc → docs/<slug>.md
    pasted_text: save docs/<slug>.md → python3 procedures/docs_ingest.py --file docs/<slug>.md → confirm
  media_path: inbound uploads at ~/.openclaw/media/inbound/<uuid> (from <media:...> placeholder); pass that path straight to --file
  note: ingest auto-extracts via pdftotext+pandoc (no manual convert); re-ingested daily 4:00 UTC; ready for docs_search after; never save long docs to memory/

## calendar_integration
  trigger: explicit date/time | deadline | appointment
  workflow: save _note_<slug>.md → extract date/time → ask if missing → python3 tools/calendar_tool.py create "<title>" --date <YYYY-MM-DD> [--time HH:MM] --note _note_<slug>.md [--reminder popup:30] [--attendee email] → confirm
  skip_if: no date/time implied | user says no calendar

## memory_tools
  memory_search: simple recall — facts, preferences, decisions, context; default for most queries
  session_search: python3 tools/session_search.py; when memory_search thin/uncertain AND session <7d old AND transcript detail needed
  graph_search: tools/graph_search.py; when complex relational query; fallback: memory_search; skip when relation_edges not yet populated

## auto_note
  triggers:
    - self_commitment: own output implies deferred work → auto-write _note_
    - completion_followup: task done but follow-up remains → auto-write _note_
    - project_estimate: request trips project gate → auto-write project _note_ before starting
  guardrail: only if concrete done_when is writable; personal/ambiguous → ask first
  upgrade: steps listed → type: project; never task_bound when steps exist

## investigate_before_act
  triggers: bug_report | fix_request | refactor | cross_entity_claim | any assertion about file/git/version/config
  rule:
    inspect: read real artifact / run command before answering; memory is not a source
    reproduce: get failure locally before fixing
    verify: re-run after fix; assert symptom gone
    cross_entity: shared lineage != shared bug; check target directly
    stale_claim: prior "it's fixed" is not evidence; re-verify
  enforce: mutable facts need live check every turn

## project_note
  purpose: context-bounded step-by-step executor; durable plan on disk survives context limits + new sessions
  type: project (extends base _note_ schema)
  format: '# Project <name>\ntype: project\nstatus: in_progress | done\ndate: YYYY-MM-DD\ndone_when: <final artifact / all steps [x]>\nstale_after: YYYY-MM-DD\ncurrent_step: <n>\nclaimed_by: <run_id or blank>\nclaimed_at: <ISO timestamp or blank>\nsteps:\n  - [ ] 1. <step> -- done_when: <check>\n  - [ ] 2. ...\nresume_state: <paths/decisions/assumptions next turn needs to resume blind>'
  claimed_by: set by Project Advancer on claim; cleared on step complete, checkpoint, or safety-gate pause; stale if claimed_at > 10min ago
  claimed_at: ISO timestamp of claim write; used to detect stale claims (>10min = auto-release)
  estimate:
    when: at request time, before starting
    gate: '>3 files OR >5 sequential tool-steps OR research-then-build OR user says project/build'
    method: heuristic gate floor + LLM override; trip gate -> make project note, else normal one-turn
  execute:
    loop: do current_step -> check session_status context%
    under_60: mark step [x], update resume_state, advance current_step, continue next step same turn
    over_60: checkpoint (write resume_state), stop cleanly, tell user step n/m done + context%; resume next turn/cron from current_step
    spawn: if over_60 AND remaining steps > 2 AND sessions_spawn available -> spawn subagent with task=remaining steps + resume_state; subagent works in clean context; parent waits for completion event
    resume: new session reads note, jumps to current_step, resumes from resume_state ONLY (no chat-history reliance)
  mandatory: after every step rewrite resume_state so note is self-sufficient; reliability linchpin
  driver: hybrid -- user may say continue; else Project Advancer cron auto-advances in_progress notes (unattended, announces progress, pauses on destructive/external steps)
  resolve: done_when met -> Daily Note Review (base) deletes/promotes via normal pipeline
  awaiting_approval_reverify: safety-gated final step (push/external) → at cold start re-verify artifact exists (git -C <repo> rev-parse HEAD == @{u}); if done, close note instead of re-asking.
  done_when_verifiability: every done_when must be locally checkable (file exists / grep / exit 0 / git rev-parse HEAD==@{u}). Narrative done_when never auto-closes.
DINOMEM_NEURON_AGENTS_BODY
)
BLOCK="$BEGIN
$DINOMEM_BODY
$END"

touch "$AGENTS"
if grep -qF "$BEGIN" "$AGENTS" 2>/dev/null; then
  skip "AGENTS.md already wired (use --force to refresh)"
else
  printf '\n%s\n' "$BLOCK" >> "$AGENTS"
  ok "AGENTS.md wired"
fi

# ── Wire TOOLS.md ────────────────────────────────────────────────────────────
# AGENTS.md carries the WHEN/policy (session_deep_recall). TOOLS.md carries the
# HOW/capability spec — the CLI surface + output schema the agent consults to
# construct a non-trivial call. Inject the matching tool reference block so the
# tools neuron adds aren't orphaned (registered only in a policy block).
hr "TOOLS.md"
TOOLS="$WS/TOOLS.md"
if [ -f "$TOOLS" ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$TOOLS" >/dev/null 2>&1 && ok "TOOLS.md backed up" || warn "TOOLS.md backup failed — continuing"
fi
TOOLS_BODY=$(cat <<'DINOMEM_NEURON_TOOLS_BODY'
  session_search:
    path: tools/session_search.py
    type: exec
    db: kb/vector_db_sessions
    capabilities:
      - session_deep_recall
      - semantic_search_archived_sessions
      - recency_weighted_reranking
      - list_indexed_sessions
    when_to_use: |
      Deep recall over recent CONVERSATION transcripts (not memory notes, not
      long-doc KB). Use when memory_search is thin/uncertain AND the relevant
      session is <7 days old AND transcript-level detail is needed. See AGENTS.md
      session_deep_recall for the WHEN/policy; this is the HOW/capability spec.
    scope: |
      Indexes only archived session .jsonl within the last 7 days (MAX_AGE_DAYS),
      ingested by procedures/session_ingest.py (cron via auto_session_reset.py,
      every 15 min). If the DB is missing, the tool errors with a build hint.
    inputs:
      query:
        type: string
        required: false
        note: "Positional. Required unless --list. Use natural language, not code identifiers."
      k:
        type: integer
        required: false
        default: 5
        note: "Number of results after recency-weighted rerank."
      session:
        type: string
        required: false
        note: "Filter by session name substring (--session)."
      list:
        type: boolean
        required: false
        note: "List all indexed sessions; no query needed (--list)."
      json:
        type: boolean
        required: false
        default: false
        note: "Output raw JSON instead of formatted text (--json)."
    output:
      type: list
      structure: chunks_with_metadata
      fields:
        - content: "Chunk text (consecutive user+assistant exchanges)."
        - score: "Relevance score (rounded). Reranked: large score gap => relevance wins; near-tie => recency decides."
        - session_name: "Source session identifier."
        - archived_date: "Date the session was archived (YYYY-MM-DD)."
        - chunk_index: "Position of chunk within the session."
        - source_file: "Archived .jsonl filename."
    constraints:
      mode: read_only
    libraries:
      - langchain_chroma
      - langchain_huggingface
      - sentence-transformers (all-MiniLM-L6-v2, local)

  docs_search:
    path: tools/docs_search.py
    type: exec
    db: kb/vector_db_docs
    capabilities:
      - semantic_search_long_docs
      - structure_aware_retrieval
      - filter_by_document
      - filter_by_section
      - list_indexed_documents
    when_to_use: |
      Semantic search over long-form documents previously ingested via
      docs_ingest.py (legal text, contracts, manuals, books). See AGENTS.md
      rag_long_docs for the WHEN/policy. For conversation transcripts use
      session_search; for market/memory KB use the agent's memory tools.
    scope: |
      Searches the structure-aware document store (kb/vector_db_docs), populated
      by procedures/docs_ingest.py (re-ingested daily ~4:00 UTC via cron). ingest
      auto-extracts pdf/docx/rtf/odt/html/epub (via pdftotext+pandoc) and accepts
      uploaded file paths (e.g. ~/.openclaw/media/inbound/<uuid>) directly — no
      manual conversion. If the DB is missing, the tool errors with a build hint.
    inputs:
      query:
        type: string
        required: false
        note: "Positional. Required unless --list. Natural language."
      k:
        type: integer
        required: false
        default: 5
        note: "Number of results (--k)."
      doc:
        type: string
        required: false
        note: "Filter by document title/filename substring (--doc)."
      section:
        type: string
        required: false
        note: "Filter by section title substring (--section)."
      max_chars:
        type: integer
        required: false
        default: 1500
        note: "Truncate chunk content (--max-chars)."
      list:
        type: boolean
        required: false
        note: "List all indexed documents; no query needed (--list)."
      json:
        type: boolean
        required: false
        default: false
        note: "Output raw JSON instead of formatted text (--json)."
    output:
      type: list
      structure: chunks_with_metadata
      fields:
        - content: "Chunk text (section-aware)."
        - score: "Relevance score."
        - doc_title: "Source document title/filename."
        - section_title: "Section the chunk belongs to."
        - chunk_index: "Position of chunk within the document."
    constraints:
      mode: read_only
    libraries:
      - langchain_chroma
      - langchain_huggingface
      - sentence-transformers (all-MiniLM-L6-v2, local)

  calendar_tool:
    path: tools/calendar_tool.py
    type: exec
    capabilities:
      - google_calendar_create_event
      - list_upcoming_events
      - delete_event
      - link_note_file
    when_to_use: |
      Create/list/delete Google Calendar events. See AGENTS.md
      calendar_integration for the WHEN/policy (explicit date/time, deadline,
      appointment). Skip when no date/time is implied or the user declines.
    requirements:
      auth: "Google Calendar OAuth (run scripts/calendar_oauth_setup.py once)."
    subcommands:
      create:
        usage: "calendar_tool.py create <title> --date YYYY-MM-DD [--time HH:MM] [--desc TEXT] [--note _note_*.md] [--reminder METHOD:MINUTES] [--attendee EMAIL]"
        inputs:
          title:    { type: string,  required: true,  note: "Positional event title." }
          date:     { type: string,  required: true,  note: "--date YYYY-MM-DD." }
          time:     { type: string,  required: false, note: "--time HH:MM (24h); omit for all-day." }
          desc:     { type: string,  required: false, note: "--desc event description." }
          note:     { type: string,  required: false, note: "--note _note_*.md filename to link." }
          reminder: { type: list,    required: false, note: "--reminder METHOD:MINUTES (repeatable), e.g. popup:30." }
          attendee: { type: list,    required: false, note: "--attendee EMAIL (repeatable)." }
      list:
        usage: "calendar_tool.py list [--upcoming DAYS]"
        inputs:
          upcoming: { type: integer, required: false, default: 7, note: "--upcoming days ahead." }
      delete:
        usage: "calendar_tool.py delete <event_id>"
        inputs:
          event_id: { type: string, required: true, note: "Positional event ID to delete." }
    constraints:
      mode: exec
    libraries:
      - google-api-python-client
      - google-auth-oauthlib
DINOMEM_NEURON_TOOLS_BODY
)
TOOLS_BLOCK="$BEGIN
$TOOLS_BODY
$END"

touch "$TOOLS"
if grep -qF "$BEGIN" "$TOOLS" 2>/dev/null; then
  skip "TOOLS.md already wired (use --force to refresh)"
else
  printf '\n%s\n' "$TOOLS_BLOCK" >> "$TOOLS"
  ok "TOOLS.md wired"
fi

# ── Patch openclaw.json bootstrap caps ───────────────────────────────────────
hr "OpenClaw config"
OPENCLAW_JSON="${OPENCLAW_CONFIG:-$HOME/.openclaw/openclaw.json}"
[ -f "$OPENCLAW_JSON" ] || OPENCLAW_JSON="$OPENCLAW_DIR/openclaw.json"
if [ ! -f "$OPENCLAW_JSON" ]; then
  warn "openclaw.json not found — skipping bootstrap cap patch"
else
  DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, sys, os

path = os.environ["DINOMEM_OPENCLAW_JSON"]
with open(path) as f:
    original = f.read()
cfg = json.loads(original)

changed = []

agents = cfg.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})

# bootstrapMaxChars / bootstrapTotalMaxChars -> raise caps to fit what dinomem-neuron
# injects, so the policy blocks are never silently truncated. Measured, not a
# fixed delta: read each root bootstrap file's ACTUAL size (the AGENTS.md/TOOLS.md
# blocks have already been appended by this point in the script), and raise the
# caps to max(existing_or_default, measured + buffer). ONLY ever increases.
import os
FILE_DEFAULT = 20000
TOTAL_DEFAULT = 60000
FILE_BUFFER = 10000
TOTAL_BUFFER = 10000
SANITY_FILE = 100000
try:
    ws = os.environ["DINOMEM_WS"]
    root_files = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md", "MEMORY.md"]
    sizes = {}
    for rf in root_files:
        p = os.path.join(ws, rf)
        if os.path.isfile(p):
            sizes[rf] = os.path.getsize(p)
    if sizes:
        max_file = max(sizes.values())
        total = sum(sizes.values())
        biggest = max(sizes, key=sizes.get)
        if max_file > SANITY_FILE:
            print(f"  \033[33m[warn]\033[0m {biggest} is {max_file} chars (>100k) — raising cap anyway, but consider trimming; large bootstrap inflates every prompt.")
        need_file = max_file + FILE_BUFFER
        need_total = total + TOTAL_BUFFER
        cur_file = defaults.get("bootstrapMaxChars", FILE_DEFAULT)
        cur_total = defaults.get("bootstrapTotalMaxChars", TOTAL_DEFAULT)
        new_file = max(cur_file, FILE_DEFAULT, need_file)
        new_total = max(cur_total, TOTAL_DEFAULT, need_total)
        if new_file != defaults.get("bootstrapMaxChars"):
            defaults["bootstrapMaxChars"] = new_file
            changed.append(f"bootstrapMaxChars -> {new_file} (fits largest root file {biggest}={max_file} + {FILE_BUFFER} buffer; raise-only)")
        if new_total != defaults.get("bootstrapTotalMaxChars"):
            defaults["bootstrapTotalMaxChars"] = new_total
            changed.append(f"bootstrapTotalMaxChars -> {new_total} (fits all root files {total} + {TOTAL_BUFFER} buffer; raise-only)")
except Exception as _e:
    print(f"  \033[33m[warn]\033[0m bootstrap cap auto-raise skipped: {_e}")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m patch openclaw.json: {c}")
elif not changed:
    print("  \033[33m[skip]\033[0m openclaw.json bootstrap caps already correct")
else:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print(f"  \033[32m[ok]\033[0m   patched: {c}")
    print("  \033[33m[warn]\033[0m Restart OpenClaw: openclaw gateway restart")
PYEOF
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
echo "  🔑 MODEL_HINT (now relevant — neuron adds the reasoning tier):"
echo "  Until now (base only) every script was no-reasoning bulk. Neuron adds"
echo "  reasoning scripts (synthesis/contradiction/promote). The model split is"
echo "  now meaningful — and it is MANUAL, so set it now or it won't happen:"
echo "    • Bulk (cheap):  DINOMEM_CHEAP_MODEL = cheapest high-context model"
echo "      (same model also for agents.defaults.compaction.model)"
echo "    • Reasoning:     stays on agents.defaults.model.primary (your default),"
echo "      + thinking via DINOMEM_REASONING_THINKING (default high) — automatic."
echo "  AGENT_ACTION: if autonomous, read the user's openclaw.json catalog, set"
echo "  DINOMEM_CHEAP_MODEL + compaction.model to a cheap high-context model, or"
echo "  recommend it to your human. Unset = everything on the default (default-safe)."
echo "  Why now: base-install recommended this too, but it only matters once neuron"
echo "  is here. If you skipped it at base install, this is the moment to set it."
echo "  Guide: https://github.com/02-dino/dinomem-neuron#model-selection"
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
