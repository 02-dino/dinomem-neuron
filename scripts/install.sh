#!/usr/bin/env bash
# dinomem-neuron — install script
# Extends dinomem with L2 graph, L3 synthesis, L4 promotion, contradiction check, topic index.
# Requires dinomem base. This installer ALWAYS installs/refreshes the base FIRST
# on every run (git clone/pull + base install.sh) so the user automatically gets
# the latest base — no separate update step. If the base is MISSING and cannot be
# provisioned, it BLOCKS (hard fail). If a base was already present and the
# refresh fails (e.g. offline), it warns and continues with the existing base.
# ORDER MATTERS: base installs BEFORE neuron copies its files, because neuron is
# a strict extension that overwrites a subset of base files with enhanced
# versions — base-after-neuron would nullify the upgrade. Pass --no-auto-base to
# skip touching the base entirely (then the base must already exist, or fail).
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-cron] [--force] [--agree] [--no-auto-base]
#
# Env:
#   DINOMEM_BASE_REPO   override base repo URL (default https://github.com/02-dino/dinomem)
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
DO_CRON=1
FORCE=0
NO_AUTO_BASE=0
# Neuron has no --dry-run flag of its own, but the bootstrap-cap patch below
# references $DRY_RUN; under `set -u` an undefined var aborts the whole install.
# Default it to 0 (no dry-run) so real installs don't crash at that phase.
DRY_RUN=0
DINOMEM_BASE_REPO="${DINOMEM_BASE_REPO:-https://github.com/02-dino/dinomem}"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)      WS="$2"; shift 2 ;;
    --agent-id)       AGENT_ID="$2"; shift 2 ;;
    --no-cron)        DO_CRON=0; shift ;;
    --force)          FORCE=1; shift ;;
    --agree)          LICENSE_AGREED=1; shift ;;
    --no-auto-base)   NO_AUTO_BASE=1; shift ;;
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

# AGENT_ID resolution (kept MANUAL by design — never auto-detected from OpenClaw
# config). Three tiers, tracked so the auto-base path can refuse to guess:
#   AGENT_ID_SOURCE=flag        --agent-id was passed explicitly (authoritative)
#   AGENT_ID_SOURCE=convention  derived from a workspace-<id> directory name
#   AGENT_ID_SOURCE=guess       neither — basename of a non-conventional workspace
AGENT_ID_SOURCE="flag"
if [ -z "$AGENT_ID" ]; then
  _WS_BASE="$(basename "$WS")"
  case "$_WS_BASE" in
    workspace-?*) AGENT_ID="${_WS_BASE#workspace-}"; AGENT_ID_SOURCE="convention" ;;
    *)            AGENT_ID="$_WS_BASE";              AGENT_ID_SOURCE="guess" ;;
  esac
fi

OPENCLAW_DIR="$(dirname "$WS")"

# ── dinomem base: helper + installer (always run — see block below) ──────────
# neuron is a strict extension of the dinomem base. Base marker files
# (procedures/session_reset.py AND procedures/extract_memory.py) MUST exist.
# base_installed() checks their presence; auto_install_base() clones/pulls the
# base repo and runs its (idempotent) installer. The driver block further down
# runs this on EVERY install so users always get the latest base, and enforces
# base-before-neuron ordering.
base_installed() { [ -f "$WS/procedures/session_reset.py" ] && [ -f "$WS/procedures/extract_memory.py" ]; }

auto_install_base() {
  # Clones DINOMEM_BASE_REPO next to the workspace and runs its install.sh with
  # the SAME --workspace/--agent-id, non-interactively. Returns nonzero on any
  # failure so the caller can hard-fail.
  local clone_dir base_install
  command -v git >/dev/null 2>&1 || { warn "git not found — cannot auto-install dinomem base"; return 1; }
  clone_dir="$OPENCLAW_DIR/dinomem"
  if [ -d "$clone_dir/.git" ]; then
    ok "dinomem base repo already cloned at $clone_dir — pulling latest"
    git -C "$clone_dir" pull --ff-only >/dev/null 2>&1 || warn "git pull failed — using existing clone"
  elif [ -e "$clone_dir" ]; then
    warn "$clone_dir exists but is not a git repo — using it as-is"
  else
    ok "cloning dinomem base: $DINOMEM_BASE_REPO -> $clone_dir"
    git clone --depth 1 "$DINOMEM_BASE_REPO" "$clone_dir" >/dev/null 2>&1 || { warn "git clone failed: $DINOMEM_BASE_REPO"; return 1; }
  fi
  base_install="$clone_dir/scripts/install.sh"
  [ -f "$base_install" ] || { warn "base install.sh not found at $base_install"; return 1; }
  ok "running dinomem base installer (non-interactive)"
  # Base install is dependency provisioning, not the neuron license gate — run it
  # unattended. Omit --no-cron: we WANT the base crons registered.
  bash "$base_install" --workspace "$WS" --agent-id "$AGENT_ID" ${LICENSE_AGREED:+--agree} || { warn "dinomem base installer exited nonzero"; return 1; }
  return 0
}

# ── ALWAYS install/refresh dinomem base FIRST (dependency + auto-update) ──────
# Design decision (dino): run the base installer on EVERY neuron install, not
# only when the base is missing. Reason: if we shipped a newer base, the user
# gets it automatically here — they stay up to date without a separate step.
# The base installer is idempotent, so re-running on an already-installed base
# just refreshes it to the latest clone.
#
# ORDERING INVARIANT (critical): base install MUST happen BEFORE neuron copies
# its own files (the "Copying scripts" section far below). neuron is a strict
# extension that OVERWRITES a subset of base files with enhanced versions. If the
# base installer ran AFTER neuron, base would clobber neuron's overrides and
# silently nullify the upgrade. So: base first (here), neuron second (later).
# Do not move this block below the copy section.
HAD_BASE_BEFORE=0
if base_installed; then HAD_BASE_BEFORE=1; fi

if [ "$NO_AUTO_BASE" = 1 ]; then
  # Opt-out of auto-base: never touch the base. But it MUST already exist.
  base_installed || fail "dinomem base not found in $WS and --no-auto-base set. Install it first: $DINOMEM_BASE_REPO"
  ok "dinomem base present (--no-auto-base: skipping base install/refresh)"
else
  # Guess-guard applies ONLY to FRESH provisioning. When the base is MISSING and
  # the agent id is only a guess (no --agent-id, non-conventional workspace),
  # provisioning a NEW base under a wrong id is dangerous → BLOCK. When the base
  # ALREADY exists we are merely refreshing it with its established id, so a
  # guessed basename is harmless and we proceed.
  if [ "$HAD_BASE_BEFORE" != 1 ] && [ "$AGENT_ID_SOURCE" = "guess" ]; then
    fail "Cannot auto-install dinomem base: agent id is ambiguous.
  --agent-id was not passed, and workspace '$WS' does not match the
  workspace-<id> convention, so '$AGENT_ID' is only a guess. Provisioning a NEW
  base under a guessed id could bind it to the wrong agent.
  Re-run stating the id explicitly:
    bash scripts/install.sh --workspace $WS --agent-id <your-agent-id> --agree"
  fi

  if [ "$HAD_BASE_BEFORE" = 1 ]; then
    ok "dinomem base present — refreshing to latest (agent id: $AGENT_ID [$AGENT_ID_SOURCE])"
  else
    warn "dinomem base not detected in $WS — installing it now (required dependency, agent id: $AGENT_ID [$AGENT_ID_SOURCE])"
  fi

  if auto_install_base; then
    base_installed || fail "dinomem base markers missing after install (in $WS/procedures/). Install blocked."
    ok "dinomem base $([ "$HAD_BASE_BEFORE" = 1 ] && echo refreshed || echo provisioned)"
  else
    # Install/refresh failed. If base was ALREADY present, degrade to a warning
    # and continue with the existing base (don't break neuron over a failed
    # refresh — e.g. offline). If base was MISSING, this is fatal.
    if [ "$HAD_BASE_BEFORE" = 1 ]; then
      warn "dinomem base refresh failed (offline?) — continuing with existing base. It may not be the latest."
    else
      fail "dinomem base install failed. Install it manually then re-run:
    git clone $DINOMEM_BASE_REPO
    bash dinomem/scripts/install.sh --workspace $WS --agent-id $AGENT_ID
  then re-run this neuron installer."
    fi
  fi
fi
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
# Resolve the SAME interpreter used for import-checks, and install deps into IT.
# Bare `pip` can target a DIFFERENT python than `python3` (e.g. /usr/local pip -> 3.11
# while python3 -> 3.14), silently installing deps where the tools can't import them.
# Always install via `$PY -m pip`. On PEP-668 externally-managed envs, retry with
# --break-system-packages (only that case; normal envs never hit the retry).
PY="$(command -v python3)"
py_can_import() { "$PY" -c "import $1" 2>/dev/null; }
pip_install() {
  # usage: pip_install <pip-args...>; returns 0 on success.
  # Retries up to 3x with backoff — transient PyPI/index hiccups were causing
  # langchain-chroma (and other deps) to fail on first attempt, forcing manual
  # re-runs during buyer installs. Second attempt of each pass adds
  # --break-system-packages for PEP 668 (externally-managed) environments.
  local _try
  for _try in 1 2 3; do
    "$PY" -m pip install -q "$@" 2>/dev/null && return 0
    "$PY" -m pip install -q --break-system-packages "$@" 2>/dev/null && return 0
    [ "$_try" -lt 3 ] && sleep $(( _try * 3 ))
  done
  return 1
}
# ── System resource check (warn-only; base already pulled the TEI image) ─────
# Neuron adds daily L2 graph build + L3 synthesis. Recommended floor:
#   base + neuron : 2 vCPU / 3 GB RAM. No large local install here, so no disk block.
NEURON_MIN_RAM_MB=3072
NEURON_MIN_CPU=2
PREFLIGHT_WARN=""   # accumulator: machine-readable below-spec signal for agent-driven installs
if [ "$(uname)" = "Darwin" ]; then
  TOTAL_RAM_MB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 ))
  CPU_COUNT=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
else
  TOTAL_RAM_MB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))
  CPU_COUNT=$(nproc 2>/dev/null || echo 0)
fi
if [ "$TOTAL_RAM_MB" -gt 0 ] && [ "$TOTAL_RAM_MB" -lt "$NEURON_MIN_RAM_MB" ]; then
  warn "RAM ${TOTAL_RAM_MB}MB is below recommended ${NEURON_MIN_RAM_MB}MB for base+neuron — graph build/synthesis may be tight. Continuing."
  PREFLIGHT_WARN="${PREFLIGHT_WARN}ram=${TOTAL_RAM_MB}MB<${NEURON_MIN_RAM_MB}MB "
elif [ "$TOTAL_RAM_MB" -gt 0 ]; then
  ok "RAM ${TOTAL_RAM_MB}MB"
else
  warn "Could not detect RAM — ensure >= ${NEURON_MIN_RAM_MB}MB for base+neuron."
fi
if [ "$CPU_COUNT" -gt 0 ] && [ "$CPU_COUNT" -lt "$NEURON_MIN_CPU" ]; then
  warn "CPU ${CPU_COUNT} core(s) is below recommended ${NEURON_MIN_CPU} — daily graph rebuild will be slow. Continuing."
  PREFLIGHT_WARN="${PREFLIGHT_WARN}cpu=${CPU_COUNT}<${NEURON_MIN_CPU} "
elif [ "$CPU_COUNT" -gt 0 ]; then
  ok "CPU ${CPU_COUNT} core(s)"
fi
# Agent-installer contract: machine-readable, hard-to-skim below-spec signal so an
# automated caller cannot silently blur past the warnings above.
if [ -n "$PREFLIGHT_WARN" ]; then
  printf '\033[1;33m[AGENT-NOTICE]\033[0m below-recommended spec detected. If this install is agent-driven, surface this to the user before continuing:\n'
  printf 'DINOMEM_NEURON_PREFLIGHT_WARN=%s\n' "$PREFLIGHT_WARN"
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
# By this point the top-of-script dependency gate has guaranteed the base is
# present (or auto-installed it, or hard-failed). This is a defensive re-check
# plus the VERSION floor enforcement.
if ! base_installed; then
  fail "dinomem base markers missing (session_reset.py/extract_memory.py). Install blocked."
else
  ok "dinomem base detected"
  # Check VERSION file
  DINOMEM_VERSION_FILE="$(dirname "$WS")/dinomem/VERSION"
  if [ ! -f "$DINOMEM_VERSION_FILE" ]; then
    # Try SKILL_DIR sibling
    DINOMEM_VERSION_FILE="$(dirname "$SKILL_DIR")/dinomem/VERSION"
  fi
  # Simple semver compare (major.minor.patch): true when $1 >= $2.
  # Sort both ascending; if the smaller line equals $2, then $1 >= $2.
  version_gte() {
    [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]
  }
  if [ -f "$DINOMEM_VERSION_FILE" ]; then
    DINOMEM_VERSION=$(cat "$DINOMEM_VERSION_FILE" | tr -d '[:space:]')
    if version_gte "$DINOMEM_VERSION" "$DINOMEM_MIN_VERSION"; then
      ok "dinomem version $DINOMEM_VERSION (>= $DINOMEM_MIN_VERSION required)"
    else
      warn "dinomem version $DINOMEM_VERSION is below minimum required $DINOMEM_MIN_VERSION — attempting auto-update."
      DINOMEM_UPDATE="$(dirname "$DINOMEM_VERSION_FILE")/scripts/update.sh"
      if [ "$NO_AUTO_BASE" != 1 ] && [ -f "$DINOMEM_UPDATE" ]; then
        bash "$DINOMEM_UPDATE" --workspace "$WS" --agent-id "$AGENT_ID" ${LICENSE_AGREED:+--agree} \
          || warn "dinomem base update.sh exited nonzero"
        DINOMEM_VERSION=$(cat "$DINOMEM_VERSION_FILE" 2>/dev/null | tr -d '[:space:]')
      elif [ "$NO_AUTO_BASE" != 1 ]; then
        # No update.sh in this base checkout — refresh the clone and rerun base install.
        auto_install_base || warn "dinomem base auto-update failed"
        DINOMEM_VERSION=$(cat "$DINOMEM_VERSION_FILE" 2>/dev/null | tr -d '[:space:]')
      fi
      # Re-check the floor — if STILL below, BLOCK.
      if version_gte "${DINOMEM_VERSION:-0.0.0}" "$DINOMEM_MIN_VERSION"; then
        ok "dinomem version $DINOMEM_VERSION (>= $DINOMEM_MIN_VERSION required, updated)"
      else
        fail "dinomem version ${DINOMEM_VERSION:-unknown} is below required $DINOMEM_MIN_VERSION and could not be auto-updated. Update it manually:
    bash dinomem/scripts/update.sh --workspace $WS --agent-id $AGENT_ID
  then re-run this neuron installer. Install blocked."
      fi
    fi
  else
    warn "dinomem VERSION file not found — cannot verify version floor ($DINOMEM_MIN_VERSION). Proceeding, but reasoning scripts may be silently degraded on an old base."
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
py_can_import langchain_chroma && skip "langchain-chroma (installed)" || (
  pip_install langchain-chroma && ok "langchain-chroma" || warn "langchain-chroma install failed"
)
py_can_import langchain_huggingface && skip "langchain-huggingface (installed)" || (
  pip_install langchain-huggingface && ok "langchain-huggingface" || warn "langchain-huggingface install failed"
)
py_can_import langchain_core && skip "langchain-core (installed)" || (
  pip_install langchain-core && ok "langchain-core" || warn "langchain-core install failed"
)
py_can_import numpy && skip "numpy (installed)" || (
  pip_install numpy && ok "numpy" || warn "numpy install failed"
)
py_can_import pandas && skip "pandas (installed)" || (
  pip_install pandas && ok "pandas" || warn "pandas install failed"
)
py_can_import duckdb && skip "duckdb (installed)" || (
  pip_install duckdb && ok "duckdb" || warn "duckdb install failed (structured_data feature unavailable; semantic tools unaffected)"
)
py_can_import yaml && skip "pyyaml (installed)" || (
  pip_install pyyaml && ok "pyyaml" || warn "pyyaml install failed"
)
py_can_import googleapiclient && skip "google-api-python-client (installed)" || (
  pip_install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 && ok "google-api-python-client" || warn "google-api-python-client install failed"
)

# ── 1) Create directories ─────────────────────────────────────────────────────
hr "Directories"
for d in topics kb/memory_neuron/l2_graph kb/memory_neuron/l3_synthesis kb/memory_neuron/l4_promoted kb/data_store; do
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
# NOTE: procedures/extract_memory.py is the NEURON SUPERSET (adds extract_from_text()
# + explicit no-reasoning routing). Base's installer already placed base's version,
# so this MUST overwrite even without --force — otherwise the upgrade silently ships
# base's older engine and neuron's distill_done_note.py (which imports
# extract_memory.extract_from_text) crashes. It is listed in ALWAYS_OVERWRITE below.
#
# The scripts/*.sh + resolve_done_notes.py + distill_done_note.py helpers are the
# cron targets referenced in the cron prompts; without them a fresh neuron install
# registers crons whose scripts are missing (Step-0 pre-gate fails).
for f in procedures/extract_memory.py procedures/memory_graph.py procedures/memory_synthesis.py procedures/memory_promote.py procedures/contradiction_check.py procedures/generate_topic_index.py procedures/docs_ingest.py procedures/session_ingest.py procedures/_schema_adapter.py procedures/data_register.py tools/docs_search.py tools/session_search.py tools/graph_search.py tools/cache_utils.py tools/data_query.py scripts/claim_note.sh scripts/check_projects.sh scripts/check_improvable.sh scripts/check_deletable.sh scripts/cron_gate.sh scripts/resolve_done_notes.py scripts/distill_done_note.py; do
  dst="$WS/$f"
  # extract_memory.py is the neuron superset over base's copy — always overwrite it,
  # even without --force, or the upgrade ships base's older engine.
  always_overwrite=0
  case "$f" in procedures/extract_memory.py) always_overwrite=1 ;; esac
  # license_check.py must sit beside procedures so the runtime guard can import it.
  true
  if [ -f "$dst" ] && [ "$FORCE" = 0 ] && [ "$always_overwrite" = 0 ]; then
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
    case "$f" in *.sh) chmod +x "$dst" 2>/dev/null || true;; esac
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

  # project_advancer — OpenClaw agentTurn cron (NOT shell crontab): auto-advance type:project in_progress notes
  # one step per run, unattended, announces progress. Premium: only neuron installs this.
  python3 - <<PYEOF
import subprocess, json, os

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_cron_job(job, idfile, label, idsdir):
    """Upgrade-safe cron register. If a job with job['name'] exists, DISABLE it and
    refresh its agentTurn message (kills old self-firing + updates the prompt);
    else create it (already enabled:False in the dict). Capture the id either way."""
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Project Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Manual add: openclaw cron add --name '{name}' --cron '<expr>' --message '<prompt>' --session isolated --no-deliver --disabled  (or use the OpenClaw cron tool)")
                return
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = ''
            # (A) READ-BACK: confirm the gateway actually stored it (some CLI versions
            # accept the args but silently no-op). If missing, surface a real error.
            vid = _cron_verify(name)
            if not vid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing).")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            jid = jid or vid
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Project Cron Gate) \u2713 verified")
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Project Advancer",
    "schedule": {"kind": "cron", "expr": "*/15 * * * *", "tz": "UTC"},
    "enabled": False,
    "payload": {
        "kind": "agentTurn",
        "message": "Project Advancer. Auto-advance step-by-step project notes unattended.\n\nSILENT-STOP CONTRACT: whenever an instruction below says 'silent' / 'no announce' / 'stop immediately', your ENTIRE final message for this run must be exactly the literal token NO_REPLY and nothing else — no extra sentence. This is what makes the cron delivery layer suppress the run; a descriptive sentence like 'nothing to advance' still gets delivered if delivery is not mode:none. REAL reports (state actually changed, safety-gate pause, flagged issue) must be sent explicitly via the message tool to the owner's default channel — do not rely on the runner's fallback announce, since delivery may be mode:none.\n\nStep 0: PRE-CHECK (zero LLM cost gate). Run: bash $WS/scripts/check_projects.sh\nIf exit code != 0 -> no in_progress project notes exist. Stop immediately. Output exactly NO_REPLY and nothing else.\n\nStep 1: ls memory/_note_*.md in $WS/memory/. Read each. Select only notes with type: project AND status: in_progress.\n\nStep 1b — HEAVINESS CHECK (pre-task): Before advancing any project, look at its NEXT unchecked step. If that step is heavy (write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle), commit to doing ONLY that one step this run. If it is light (tiny edit/config/doc/one-liner), you may chain several light steps in this run. Turn boundaries are decided by step heaviness, not by context %.\n\nStep 1c: If no in_progress notes AND nothing flagged, stop silently. Output exactly NO_REPLY and nothing else.\n\nStep 2 — CLAIM CHECK (atomic task claiming). ALL timestamps are UTC ISO8601; compute claim age in UTC. For each in_progress note, read claimed_by and claimed_at fields. (G6 live-session guard) If claimed_by STARTS WITH 'live-session' AND claimed_at is less than 120 minutes (2 hours) ago, SKIP this note entirely — a human is actively working it in an interactive session; never fight a live session. If claimed_by is set (any other value) AND claimed_at is less than 30 minutes ago, SKIP this note entirely (another Advancer instance is working it). If claimed_at is older than its window (120min for live-session, 30min otherwise), the claim is stale — clear claimed_by and claimed_at, then proceed. If claimed_at is in the FUTURE (clock/timezone error), treat the claim as INVALID/stale and clear it. If claimed_by is absent, proceed.\n\nStep 3 — CLAIM WRITE: Before executing any step, write claimed_by: <this run's cron run_id or a short uuid> and claimed_at: <current time as UTC ISO8601 with trailing Z, e.g. 2026-07-07T00:55:00Z> to the note. Re-read the note immediately after writing to verify the claim was written correctly. If the re-read shows a different claimed_by (race condition), abort this note and move on.\n\nStep 4: For each claimed in_progress project note, pick the lowest unchecked step at current_step. Resume from resume_state ONLY (the note is self-sufficient; do not rely on chat history).\n\nStep 4b — DRIFT CHECK (verify before executing): Do NOT assume current_step's checkbox state reflects reality — notes can drift (e.g. work finished in a live human session but the checkbox never flipped, or a companion note already covered this step). Before treating this step as pending work, independently verify whether its OWN done_when is already satisfied against real state (file/artifact exists, git -C <repo> rev-parse HEAD == @{u}, feature already shipped, companion note already did it, etc — the same kind of locally-checkable verification Daily Note Review uses). If already satisfied: mark it [x], advance current_step, rewrite resume_state, then repeat this same Step 4b check for the NEW current_step (loop until you hit a step that is genuinely NOT done, or the whole project completes) — treat this drift-catchup as part of the same LIGHT turn, not a new heavy step. If genuinely not satisfied, proceed to Step 5 normally.\n\nStep 5: SAFETY GATE before executing the step. If the step is destructive or external (deletes data, git push, sends external messages, irreversible writes outside workspace, spends money, touches credentials), DO NOT execute it. Leave it pending, clear your claim (Step 8), send a message via the message tool to the owner that the project is paused awaiting approval at step n/m — tag the owner (use platform-appropriate mention format), move to the next project.\n\nStep 6: Execute the one current step. After it: verify the step done_when. If met, mark [x], advance current_step, REWRITE resume_state with what the next step needs (paths, decisions, assumptions). If not met, leave pending and record why in resume_state.\n\nStep 7: Turn-boundary guard (heaviness-based). Gauge the heaviness of the NEXT step. Heavy = write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle. If the next step is heavy, do only the current step this run, checkpoint (state already written), CLEAR claimed_by and claimed_at, and stop advancing this run — do NOT chain another heavy step. Light steps (tiny edit/config/doc/one-liner) may be chained: continue to the next step of the same project until the next step is heavy or the project is done.\n\nStep 8 — CLAIM RELEASE: After completing or checkpointing a project (step done, heavy-step checkpoint, or safety gate pause), clear claimed_by and claimed_at from the note before moving to the next project.\n\nStep 9: When a project done_when (all steps [x] / final artifact) is met, flip status to done, clear claimed_by and claimed_at. Daily Note Review will delete/promote it.\n\nStep 10: REPORT only on real state change, via the message tool (do not rely on runner fallback delivery, which may be disabled). Rules:\n- Step advanced or completed -> send a message with a progress bar\n- First time hitting safety gate -> send a message once, tag owner (use platform-appropriate mention)\n- Already paused (resume_state contains 'awaiting approval') -> silent. Output exactly NO_REPLY and nothing else.\n- Skipped due to a live-session or other active claim -> silent (not a state change). Output exactly NO_REPLY and nothing else.\n- Malformed or stalled -> send a message only if last_flagged not set in resume_state OR last_flagged > 24h ago; write last_flagged: <ISO timestamp> to resume_state after sending. Otherwise silent: output exactly NO_REPLY.\n- No state change -> silent. Output exactly NO_REPLY and nothing else.\nProgress bar (only when actually sending a real state-change report): [████░░░░░░] 4/10 40% — project name, step n/m. Short, group-chat style.",
        "timeoutSeconds": 600
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
# UPSERT (upgrade-safe): if a job with this name already exists, DISABLE it and
# refresh its prompt (so old installs stop self-firing + get the new message);
# otherwise create it disabled. Either way capture the id for the gate. Never
# create a duplicate, never leave an old enabled copy behind.
upsert_cron_job(job, "advancer", "project_advancer", "$WS/.cron_ids")
PYEOF
fi

# ── project_improver (Component 2) — OpenClaw agentTurn cron ──────────────────
# Self-improving closer, half 1 of 2: LLM improves COMPLETED-but-unverified
# project notes (behavior-preserving, all domains), then stamps verified: true.
# Zero-LLM pre-gate (check_improvable.sh) guards cost. Premium: neuron only.
python3 - <<PYEOF
import subprocess, json, os

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_cron_job(job, idfile, label, idsdir):
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Project Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Manual add: openclaw cron add --name '{name}' --cron '<expr>' --message '<prompt>' --session isolated --no-deliver --disabled  (or use the OpenClaw cron tool)")
                return
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = ''
            # (A) READ-BACK: confirm the gateway actually stored it (some CLI versions
            # accept the args but silently no-op). If missing, surface a real error.
            vid = _cron_verify(name)
            if not vid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing).")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            jid = jid or vid
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Project Cron Gate) \u2713 verified")
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Project Improver (Component 2)",
    "schedule": {"kind": "cron", "expr": "20,50 * * * *", "tz": "UTC", "staggerMs": 60000},
    "enabled": False,
    "payload": {
        "kind": "agentTurn",
        "message": "Project Improver (Component 2). You improve COMPLETED-but-unverified project notes, then stamp them verified so Component 1 can retire them. Autonomous, behavior-preserving, all domains. Hold the safety gate HARD — you edit real artifacts unattended.\n\nStep 0 — ZERO-LLM PRE-GATE: Run: bash $WS/scripts/check_improvable.sh\nIf exit code != 0 → nothing to improve. STOP IMMEDIATELY. No further tool calls, no announce.\n\nStep 1 — SELECT: ls memory/_note_*.md in $WS/memory/. Read each. Pick notes with type: project AND status: done AND NOT verified: true. Handle ONE note this run (the oldest by date). If none, stop silently.\n\nStep 2 — CLAIM CHECK (respect the shared claim protocol; never fight another job). ALL timestamps are UTC ISO8601; compute age in UTC. Read claimed_by / claimed_at. If claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes (2h) ago → SKIP (a human is working it live; never fight a live session). If claimed_by is any other value AND claimed_at < 30 min ago → SKIP (Advancer or another improver instance owns it). If claimed_at older than its window → stale, clear and proceed. If claimed_at is in the FUTURE (clock/timezone error) → treat as invalid/stale, clear and proceed. If absent → proceed.\n\nStep 3 — CLAIM WRITE: Write claimed_by: component2-improver AND claimed_at: <current time as UTC ISO8601 with trailing Z, e.g. 2026-07-07T00:55:00Z> into the note frontmatter. Re-read immediately to confirm your claim stuck. If a different claimed_by appears (lost a race) → abort this note, stop.\n\nStep 4 — UNDERSTAND: Read the project's done_when and resume_state, and inspect the actual artifact(s) it produced (scripts, deliverables, reports, config — whatever the project made). Establish what the project DOES and how you'll verify it still does that after any change (its behavior contract = its done_when).\n\nStep 5 — ASSESS (principle, NOT a checklist): Ask 'can this be made cheaper, faster, clearer, or more reliable — AND does that even make sense for THIS project?' There is NO prescribed action. Do not force caching/logging/locks/dedup or any technique. The right move is whatever fits this specific project — possibly a technique nobody would list, possibly NOTHING. 'Already efficient, nothing needed' is a valid, common, correct outcome.\n\nStep 6 — IMPROVE (behavior-preserving ONLY): If there is a worthwhile improvement, make it. You MUST NOT change WHAT the project does — only how efficiently/reliably/clearly it does it. Back up any file before editing (bash $SKILL_DIR/scripts/file-backup.sh <file>). Keep changes minimal and targeted.\n\nStep 7 — VALIDATE / DEBUG until reliable: After improving, verify the artifact STILL satisfies the project's done_when — for code: it runs / tests pass / compiles; for a deliverable: it is coherent, complete, and verifiably correct against done_when. If your change broke behavior, debug and fix, or REVERT to the backup. Do not proceed until the behavior contract holds again. If you cannot make it hold, revert fully and treat as 'no safe improvement'.\n\nStep 8 — SAFETY GATE (non-negotiable): You may do LOCAL, reversible work only. If improving/validating would require an EXTERNAL or DESTRUCTIVE action — git push, deleting data, sending external messages, spending money, touching credentials, irreversible writes outside the workspace — DO NOT do it. Do the local part, then STOP before the external action. Do NOT stamp verified. Clear your claim (Step 10), and in Step 11 report the project is 'improved locally, paused awaiting approval for <the external step>', tag the owner. Record in resume_state that it is awaiting approval so the next run does not re-loop on it endlessly.\n\nStep 9 — STAMP VERIFIED: Only if the project is validated behavior-preserving AND no external step is blocking, write verified: true into the note frontmatter. This is the signal Component 1 uses to delete it. (If you determined 'nothing needed', still stamp verified: true — you reviewed and confirmed it's already sound.)\n\nStep 10 — CLAIM RELEASE: Clear claimed_by and claimed_at from the note (whether you stamped verified, paused at the safety gate, or aborted). Never leave a dead claim.\n\nStep 11 — REPORT to the owner via the message tool, using the agent's default owner channel (platform-appropriate mention; do not hardcode a chat id). One short message: project name + what you did. Formats:\n- Improved: 'Improved <project>: <one-line what+why>. Validated behavior-preserving, verified.'\n- Nothing needed: 'Reviewed <project>: already efficient, nothing to change. Verified.'\n- Safety-gated: 'Improved <project> locally: <what>. Paused — needs your approval for <external step> before I finish.' (tag owner)\nKeep it terse. No report if Step 0 gated out or you skipped on a claim (those are silent).\n\nHARD RULES: behavior-preserving only, never change what a project does; local+reversible only, external/destructive → stop+report; one note per run; always release your claim; respect live-session and advancer claims; 'nothing needed' is a valid outcome — never invent busywork.",
        "timeoutSeconds": 900
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_cron_job(job, "improver", "project_improver", "$WS/.cron_ids")
PYEOF

# ── verified_note_deleter (Component 1) — OpenClaw agentTurn cron ─────────────
# Self-improving closer, half 2 of 2: thin wrapper over resolve_done_notes.py.
# Deterministically deletes notes carrying verified: true (snapshot + _pin_
# promote + non-blocking lock + claim-respect). Premium: neuron only.
python3 - <<PYEOF
import subprocess, json, os

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_cron_job(job, idfile, label, idsdir):
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Project Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Manual add: openclaw cron add --name '{name}' --cron '<expr>' --message '<prompt>' --session isolated --no-deliver --disabled  (or use the OpenClaw cron tool)")
                return
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = ''
            # (A) READ-BACK: confirm the gateway actually stored it (some CLI versions
            # accept the args but silently no-op). If missing, surface a real error.
            vid = _cron_verify(name)
            if not vid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing).")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            jid = jid or vid
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Project Cron Gate) \u2713 verified")
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Verified Note Deleter (Component 1)",
    "schedule": {"kind": "cron", "expr": "25,55 * * * *", "tz": "UTC", "staggerMs": 30000},
    "enabled": False,
    "payload": {
        "kind": "agentTurn",
        "message": "Verified Note Deleter (Component 1). Deterministic janitor: delete project/task notes that carry verified: true, so the workspace stays clean after Component 2 has improved+validated them. This is a THIN wrapper around a script that does all the real work (zero-LLM gate, claim-respect, snapshot, distill-to-regular-memory, non-blocking lock).\n\nStep 1: Run exactly this command: python3 $WS/scripts/resolve_done_notes.py\n\nStep 2: Read its output. The script self-gates: if no status:done note exists it exits immediately (no work). It deletes ONLY notes with verified: true that are NOT freshly claimed by another job; before deleting each it snapshots it and DISTILLS its knowledge into normal schema memory entries (which then age/freeze on merit via the usual retention review). There is no _pin_ promotion — durable pins are authored directly as _pin_ files, never produced by note retirement.\n\nStep 3: ANNOUNCE only if it actually deleted something (grep the output for 'deleted verified note'). If so, send ONE terse line to the owner via the message tool using the agent's default owner channel (platform-appropriate; do not hardcode a chat id): e.g. 'Retired N verified note(s): <names>.' If it deleted nothing (gate exit, lock busy, or no verified notes), stay SILENT — no message.\n\nDo nothing else. Do not edit notes, do not re-check done_when, do not stamp verified — that is Component 2's job. You are the janitor.",
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_cron_job(job, "deleter", "verified_note_deleter", "$WS/.cron_ids")
PYEOF

# ── project_cron_gate — ZERO-LLM dispatcher (command cron) ────────────────────
# The three jobs above (Advancer/Improver/Deleter) are registered DISABLED so they
# never self-fire. This single command-kind cron runs every 15 min in the Gateway
# process at $0 LLM, runs each cheap check_*.sh, and force-runs (openclaw cron run)
# only the jobs whose check reports real work. That is the whole point of the
# redesign: the 15-min heartbeat costs no LLM unless there is an actual project
# note to advance/improve/retire.
#
# Command crons need the Gateway to allow command-kind jobs (operator.admin). If a
# buyer's gateway refuses them, we FALL BACK by re-enabling the three agentTurn
# jobs on their own schedules (the pre-redesign behavior) so nothing breaks — and
# we print a LOUD warning, because that fallback path costs LLM on every idle tick.
GATE_CHECK=$(python3 -c "
import subprocess, json, sys
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    exists = any('project cron gate' in j.get('name','').lower() for j in (jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])))
    print('exists' if exists else 'missing')
except: print('skip')
" 2>/dev/null)
if [ "$GATE_CHECK" = "exists" ]; then
  skip "project_cron_gate OpenClaw cron (exists)"
elif [ "$GATE_CHECK" = "skip" ]; then
  warn "Could not check OpenClaw cron — add Project Cron Gate manually via OpenClaw"
else
  python3 - <<PYEOF
import subprocess, json, os

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a


WS = "$WS"
idsdir = WS + "/.cron_ids"
def readid(name):
    try:
        return open(idsdir + "/" + name).read().strip()
    except Exception:
        return ""
adv = readid("advancer"); imp = readid("improver"); dele = readid("deleter")

# Fallback helper: re-enable the three agentTurn jobs on their own schedules so
# behavior is preserved even without the zero-LLM gate.
def reenable(label, jid):
    if not jid:
        return
    subprocess.run(['openclaw', 'cron', 'enable', jid], capture_output=True, text=True, timeout=15)

def fallback(reason):
    print("  \033[33m[WARN]\033[0m ==================================================================")
    print("  \033[33m[WARN]\033[0m  Zero-LLM command-cron gate could NOT be installed:")
    print("  \033[33m[WARN]\033[0m    " + reason)
    print("  \033[33m[WARN]\033[0m  Falling back to per-15-min agentTurn checks for Advancer/Improver/Deleter.")
    print("  \033[33m[WARN]\033[0m  This path COSTS LLM on every idle tick (~192 turns/day of mostly no-op).")
    print("  \033[33m[WARN]\033[0m  To get the free path: allow command cron jobs (operator.admin) on your")
    print("  \033[33m[WARN]\033[0m  gateway, then re-run this installer.")
    print("  \033[33m[WARN]\033[0m ==================================================================")
    reenable("advancer", adv); reenable("improver", imp); reenable("deleter", dele)

if not (adv and imp and dele):
    fallback("one or more LLM job ids were not captured (cron add may have failed above).")
else:
    gate = {
        "name": "Project Cron Gate",
        "description": "Zero-LLM dispatcher: checks project-note gates every 15min and force-runs Advancer/Improver/Deleter only when work exists",
        "schedule": {"kind": "cron", "expr": "*/15 * * * *", "tz": "UTC"},
        "payload": {
            "kind": "command",
            "argv": ["sh", "-lc", "bash " + WS + "/scripts/cron_gate.sh"],
            "cwd": WS,
            "env": {
                "OPENCLAW_WORKSPACE": WS,
                "GATE_ADVANCER_ID": adv,
                "GATE_IMPROVER_ID": imp,
                "GATE_DELETER_ID": dele,
            },
        },
        "delivery": {"mode": "none"},
    }
    r = subprocess.run(_cron_add_argv(gate), capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        fallback("gateway rejected the command-cron add: " + (r.stderr or '')[:120])
    else:
        # REAL PROBE: confirm the gateway actually stored it as a command-kind job.
        gid = ''
        try:
            gid = json.loads(r.stdout).get('id','')
        except Exception:
            pass
        kind_ok = False
        if gid:
            g = subprocess.run(['openclaw', 'cron', 'get', gid, '--json'], capture_output=True, text=True, timeout=15)
            if g.returncode == 0:
                try:
                    gj = json.loads(g.stdout)
                    kind_ok = (gj.get('payload', {}).get('kind') == 'command') and (gj.get('enabled') is True)
                except Exception:
                    kind_ok = False
        else:
            # some builds return the job inline without a separate get; trust returncode + parse
            try:
                gj = json.loads(r.stdout)
                kind_ok = (gj.get('payload', {}).get('kind') == 'command')
            except Exception:
                kind_ok = False
        if kind_ok:
            print("  \033[32m[ok]\033[0m   project_cron_gate OpenClaw command-cron registered (*/15, zero-LLM) — drives Advancer/Improver/Deleter")
        else:
            # Gateway downgraded/refused a real command job — remove the bad gate and fall back.
            if gid:
                subprocess.run(['openclaw', 'cron', 'remove', gid], capture_output=True, text=True, timeout=15)
            fallback("gateway did not store a real command-kind job (payload.kind != command).")
PYEOF
fi

# ── daily_note_review — OpenClaw agentTurn cron (general note janitor) ────────
# Retires ALL note types once resolvable: task_bound notes whose done_when is met,
# type:project notes finished out-of-band or parked at a safety-gated final step,
# and abandoned notes past stale_after. Complements the Advancer/Improver/Deleter
# (which only handle project step-advance + verified deletion). Claim-aware:
# never touches a freshly-claimed note (live-session <120m / other <30m).
# Premium: neuron only. Without this, done task_bound + non-verified done notes
# would never auto-retire on a buyer install.
python3 - <<PYEOF
import subprocess, json

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_selfsched(job, label):
    """Upgrade-safe register for a SELF-scheduled agentTurn cron (no gate driving it).
    If a job with job['name'] exists, refresh its prompt in place (--message) and keep
    it ENABLED on its own schedule; else create it. Never duplicates, never freezes an
    old prompt. (Contrast with gate-driven jobs, which get --disable.)"""
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            args = ['openclaw', 'cron', 'edit', existing_id, '--message', msg, '--enable']
            sched = job.get('schedule', {})
            if sched.get('kind') == 'cron' and sched.get('expr'):
                args += ['--cron', sched['expr']]
            subprocess.run(args, capture_output=True, text=True, timeout=15)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed, stays enabled)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            # (A) READ-BACK: confirm the gateway actually stored it.
            if not _cron_verify(name):
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing). Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered \u2713 verified")
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Daily Note Review",
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        "message": "Scan all memory/_note_*.md files in $WS/memory/. Resolve each note (today = current UTC date). FIRST, for every note read claimed_by and claimed_at (UTC ISO8601): if claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes ago, OR claimed_by is any other value AND claimed_at < 30 minutes ago, SKIP that note entirely this run (another job or a live human owns it). Stale or future-dated claims are ignored (proceed). Then: 1) SKIP any note with an expires: field until today > expires; delete a time_bound note only once today > its expires date (generic time-based GC; no external calendar service). 2) task_bound notes (have done_when:): verify the done_when condition against workspace state (file exists, feature shipped, grep, exit 0). If verified, flip status to done and delete the note (its knowledge is distilled into regular memory by the Verified Note Deleter pipeline; do NOT copy it to _pin_ — pins are authored directly, not produced by retirement). Else leave pending. 3) type:project notes: these are normally advanced/closed by the Project Advancer and improved+retired by the Project Improver + Verified Note Deleter, BUT a project can be finished out-of-band by a human-driven session and left status:in_progress, or parked at a safety-gated final step (git push / external action) the Advancer is forbidden to run — so it would otherwise orphan here. For any type:project note, verify its done_when the SAME way as task_bound (run the locally-checkable condition; e.g. for a git-push done_when run git -C <repo> rev-parse HEAD vs @{u}). If done_when verifies and/or all steps are [x], flip status to done and delete it (no _pin_ promotion). If still in_progress with unchecked non-gated steps remaining, leave it for the Advancer. Never delete a project whose done_when does not verify. 4) stale_after GC: if a note is still pending/in_progress AND done_when was never met AND today > stale_after (default date+30d, or date+7d for reminder/quick-todo notes), delete it as abandoned. 5) Legacy notes with no schema fields: infer the task from content, delete if clearly resolved, else leave. Report what resolved, what was GC'd, what remains, and what was skipped (including claim-skips).",
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_selfsched(job, "daily_note_review")
PYEOF

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
  note: ingest auto-extracts via pdftotext+pandoc (no manual convert); re-ingested daily 4:00 UTC; ready for docs_search after; never save long docs to memory/; row|count|range|filter|aggregate over structured data (csv|json|parquet) → data_query NOT docs_search

## structured_data
  when: row | count | range | filter | sort | aggregate | field lookup | "how many" | "under $X" | "on <date>" | "cheapest/most" | "list ... where" over registered exact data (csv|json|parquet; any structured/semi-structured dataset)
  action:
    query: read kb/data_store/catalog.json → pick table(s) by keywords+columns → python3 tools/data_query.py --sql "SELECT ... FROM <table> WHERE ..."  # SELECT-only
    register: python3 procedures/data_register.py --file <path.csv|json|parquet> --name <table> --desc "..." [--keywords a,b,c]
  route: meaning|"like this"|passage → docs_search | fact|decision|preference → memory_search | recent-convo <7d → session_search | exact pattern|regex over raw text/logs → exec (ripgrep/grep)
  note: store=kb/data_store/ (one duckdb, one table per dataset, NOT a category column, NOT db-per-category); handles structured+semi-structured (nested json ok); catalog.json=schema map+intent router; retrieval=exact SQL no embeddings; NEVER save rows to memory/

## retrieval_router
  meaning | "explain" | "like this" | passage → docs_search
  recent conversation detail (<7d) → session_search
  fact | decision | preference | todo → memory_search
  row | count | range | filter | sort | aggregate | field lookup (csv|json|parquet) → data_query
  exact pattern | regex | grep | exact-string over raw text/logs → exec (ripgrep/grep)

## auto_note
  triggers:
    - self_commitment: own output implies deferred work → auto-write _note_
    - external_commitment: own output commits to discrete external artifact (github issue/PR, external send) not done THIS turn → auto-write _note_ immediately with verifiable done_when
    - completion_followup: task done but follow-up remains → auto-write _note_
    - project_estimate: request trips project gate → auto-write project _note_ before starting
  guardrail: only if concrete done_when is writable; personal/ambiguous → ask first
  upgrade: enumerate steps FIRST; if >1 digital/verifiable step → type: project; never skip enumeration; physical/non-verifiable tasks → task_bound

## note_claim_protocol
  when: a LIVE interactive session is about to EDIT/write a project or task _note_ (_note_*.md) that a cron (Advancer/Improver/Deleter) could also touch
  why: the crons READ claimed_by/claimed_at and back off a fresh claim (live-session 120min window), but nothing writes the claim for the live agent. Unclaimed => a cron can grab the same note mid-edit and double-write / re-nag. The live agent is the write side of the protocol.
  action:
    - BEFORE the first edit: bash $WS/scripts/claim_note.sh claim <note> live-session-<agent-id>   # exit 3 = another job holds it fresh -> do not fight; pick another or tell the user
    - each later turn resuming the SAME note: bash $WS/scripts/claim_note.sh refresh <note> live-session-<agent-id>
    - when done / paused / abandoning: bash $WS/scripts/claim_note.sh release <note>
  skip_when: read-only note lookups; _pin_ files; a note created+finished in one edit with no cron overlap
  enforce: mandatory before multi-turn or build-related note edits; this makes the claim protocol enforceable instead of documentation-only

## memory_tools
  # extends base memory_tools; adds neuron-specific tools
  session_search: python3 tools/session_search.py; when memory_search thin/uncertain AND session <7d old AND transcript detail needed
  session_start_recall: extends base M_session_start — at cold start, when opener names entity/feature/continuation absent from _note_ files, run session_search "<natural-language topic of opener>" over recent raw convo <7d and read hit BEFORE answering; glob + session_search both mandatory (neuron-only: base has no session_search tool)
  graph_search: tools/graph_search.py; when complex relational query; fallback: memory_search; skip when relation_edges not yet populated
  data_query_handoff: row|count|range|filter|sort|aggregate|field-lookup over registered structured data (csv|json|parquet) → data_query (exact SQL), NOT memory_search; memory has no <,>,=,COUNT,SUM; see ## structured_data / ## retrieval_router
  # G2: session_search embeds queries too — the base M3_query_style rule applies to it as well:
  #      prefer natural language, avoid technical identifiers/code terms/exact strings/variable names.
  M3_query_style_extends: applies_to also includes session_search (same natural-language rule as memory_search)

## project_note
  purpose: context-bounded step-by-step executor; durable plan on disk survives context limits + new sessions
  type: project (extends base _note_ schema)
  format: '# Project <name>\ntype: project\nstatus: in_progress | done\ndate: YYYY-MM-DD\ndone_when: <final artifact / all steps [x]>\nstale_after: YYYY-MM-DD\ncurrent_step: <n>\nclaimed_by: <run_id or blank>\nclaimed_at: <ISO timestamp or blank>\nsteps:\n  - [ ] 1. <step> -- done_when: <check>\n  - [ ] 2. ...\nresume_state: <paths/decisions/assumptions next turn needs to resume blind>'
  claimed_by: set by Project Advancer / Project Improver on claim; cleared on step complete, checkpoint, or safety-gate pause; stale if claimed_at older than its window (live-session 120min / other 30min)
  claimed_at: UTC ISO8601 timestamp (trailing Z) of claim write; used to detect stale claims. Windows: live-session 120min, other claimant 30min. Future-dated = invalid/stale.
  estimate:
    when: at request time, before starting
    gate: '>3 files OR >5 sequential tool-steps OR research-then-build OR user says project/build'
    method: heuristic gate floor + LLM override; trip gate -> make project note, else normal one-turn
    upgrade: enumerate steps FIRST; if >1 digital/verifiable step -> type: project; never skip enumeration; physical/non-verifiable tasks -> task_bound
  heaviness_gate:
    signal: heaviness = execution load of a step (files touched, LOC, test/debug surface)
    heavy: write+test a whole tool/file | multi-file change | research-then-build | full write->run->debug->verify cycle
    light: tiny edit | config | doc | one-liner
    rule: heavy step -> own turn; light steps -> batch a few per turn
  execute:
    turn_boundary: heaviness of NEXT step decides continue-vs-stop; heavy -> stop, start it fresh next turn; light -> do it, continue
    gate: do NOT start a heavy step to batch; a heavy step gets its own turn
    heavy_step: checkpoint (write resume_state), stop cleanly, tell user step n/m done; resume next turn/cron from current_step
    light_step: mark step [x], update resume_state, advance current_step, continue next light step same turn
    spawn: if next step heavy AND remaining steps > 2 AND sessions_spawn available -> spawn subagent with task=remaining steps + resume_state; subagent works in clean context; parent waits for completion event
    resume: new session reads note, jumps to current_step, resumes from resume_state ONLY (no chat-history reliance)
  mandatory: after every step rewrite resume_state so note is self-sufficient; reliability linchpin
  note_edit_safety: # G5b -- guards against silently losing a resume_state/status write
    why: the edit tool applies a multi-edit batch ATOMICALLY -- if ANY one oldText fails to match, the WHOLE batch rolls back and NOTHING changes; the tool returns an error but a note claimed 'updated' before reading that result is silently stale (=> Advancer resumes from wrong step, or a done note never closes)
    trap: chaining edits where a later oldText anchor overlaps/depends on text an earlier edit in the SAME batch already changed, or re-using an anchor a prior applied edit consumed -> stale anchor -> full rollback
    rules:
      - NEVER treat a note write as done until you have READ the tool's success/error result; an error = zero changes applied
      - after any batch of >1 edit to a note, grep/re-read the changed lines (status, current_step, resume_state, claimed_at) to CONFIRM before advancing or announcing
      - keep batch edits to DISJOINT independent regions; if edits overlap or depend on each other, split into sequential calls and verify between them
      - on rollback: re-read the note FRESH (anchors may reflect a partial earlier success), rebuild oldText from current content, retry
  self_check: # G5
    when: before stopping at ANY checkpoint while status in_progress
    verify: type:project, status:in_progress, current_step matches lowest unchecked step, resume_state non-empty + concrete
    on_fail: fix note THIS TURN before stopping; malformed note = silent death (Advancer skips it)
  live_session_claim: # G6 rule -- makes claim automatic in interactive sessions, not just cron-vs-cron
    when: a live interactive session actively executes a project note (not the Advancer cron)
    action: write claimed_by=live-session-<agentid> + claimed_at=<now ISO> to the note BEFORE the first edit; refresh claimed_at each resuming turn; clear both on finish/pause
    why: without this the 4h Advancer can wake mid-conversation and double-edit the same note; the claim lock is otherwise only honored cron-vs-cron
  driver: hybrid -- user may say continue; else Project Advancer cron auto-advances in_progress notes (unattended, announces progress, pauses on destructive/external steps)
  resolve: done_when met -> Daily Note Review cron (neuron, 06:00 UTC, claim-aware) deletes/promotes via normal pipeline
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
      - sentence-transformers (multilingual-e5-small, local)

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
    note: errors with a build hint if kb/vector_db_docs is missing (run docs_ingest.py)
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
      - sentence-transformers (multilingual-e5-small, local)

  data_query:
    path: tools/data_query.py
    type: exec
    db: kb/data_store/data.duckdb
    catalog: kb/data_store/catalog.json
    capabilities:
      - exact_sql_retrieval
      - csv_json_parquet_ingest
      - semi_structured_nested_json
      - filter_range_sort_aggregate
      - multi_table_one_db
      - list_catalog_tables
      - select_only_readonly
    inputs:
      sql:
        type: string
        required: false
        note: "SELECT-only query (--sql). Non-SELECT rejected."
      table:
        type: string
        required: false
        note: "Scope/inspect one catalog table (--table)."
      list:
        type: boolean
        required: false
        note: "List catalog tables+schema+keywords; no sql needed (--list)."
      format:
        type: string
        required: false
        default: text
        note: "text|json output (--format)."
    output:
      type: list
      structure: rows
    constraints:
      mode: read_only
    libraries:
      - duckdb

  data_register:
    path: procedures/data_register.py
    type: exec
    db: kb/data_store/data.duckdb
    catalog: kb/data_store/catalog.json
    capabilities:
      - materialize_csv_json_parquet
      - infer_column_types
      - write_catalog_entry
      - refresh_prune_hash_skip
      - nested_json
    inputs:
      file:
        type: string
        required: false
        note: "Source file to register (--file); csv|json|parquet."
      name:
        type: string
        required: false
        note: "Table name (--name); SQL-safe identifier."
      desc:
        type: string
        required: false
        note: "One-line dataset description (--desc)."
      keywords:
        type: string
        required: false
        note: "Comma-separated routing keywords (--keywords)."
      list:
        type: boolean
        required: false
        note: "List registered tables (--list)."
      dry_run:
        type: boolean
        required: false
        note: "Validate readability; no write (--dry-run)."
      refresh:
        type: boolean
        required: false
        note: "Re-scan sources; update schema/rows; prune vanished (--refresh)."
    output:
      type: text
    constraints:
      mode: read_write
    libraries:
      - duckdb
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
    import subprocess
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    # Validate after write; if this OpenClaw version rejects it, restore the exact
    # pre-write bytes (these are only two raise-only ints — a full revert here loses
    # nothing else, since this block patches nothing but bootstrap caps).
    def _validate():
        if os.environ.get("DINOMEM_SKIP_CONFIG_VALIDATE") == "1":
            return None
        try:
            return subprocess.run(["openclaw", "config", "validate"],
                                  capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return None
        except Exception:
            return None
    v = _validate()
    if v is not None and v.returncode != 0:
        with open(path, "w") as f:
            f.write(original)
        print("  \033[33m[warn]\033[0m bootstrap-cap patch rejected by schema — reverted (caps left at prior values):")
        for line in (v.stderr or v.stdout or "").strip().splitlines():
            line = line.strip()
            if line:
                print(f"           {line}")
    else:
        for c in changed:
            print(f"  \033[32m[ok]\033[0m   patched: {c}")
        if v is not None:
            print("  \033[32m[ok]\033[0m   openclaw.json validated against schema")
        print("  \033[33m[warn]\033[0m Restart OpenClaw: openclaw gateway restart")
PYEOF
fi

# ── Install note-claim-refresh hook plugin (auto-refresh live-session claims) ──
# The note_claim_protocol (wired into AGENTS.md above) tells the live agent to run
# `claim_note.sh refresh` each turn it keeps editing a note. That is documentation-
# only — an agent that forgets lets a cron (Advancer/Improver/Deleter) grab the note
# mid-edit and double-write. This plugin makes the refresh AUTOMATIC: an
# after_tool_call hook runs `claim_note.sh refresh` as a CONSEQUENCE of any
# edit/write/apply_patch to a memory/_note_*.md the live session holds. Observe-only,
# never blocks a tool. Workspace is resolved dynamically at runtime; we ALSO pin it in
# plugin config here for robustness on multi-workspace hosts.
hr "Note-claim-refresh hook plugin"
PLUGIN_SRC="$SKILL_DIR/plugins/note-claim-refresh"
PLUGIN_DST="$WS/plugins/note-claim-refresh"
if [ ! -d "$PLUGIN_SRC" ]; then
  warn "plugin source $PLUGIN_SRC missing — skipping (claim refresh stays manual)"
else
  mkdir -p "$PLUGIN_DST"
  cp "$PLUGIN_SRC/openclaw.plugin.json" "$PLUGIN_SRC/package.json" "$PLUGIN_SRC/index.ts" "$PLUGIN_DST/"
  ok "plugin files -> $PLUGIN_DST"
  if [ ! -f "$OPENCLAW_JSON" ]; then
    warn "openclaw.json not found — plugin copied but NOT wired. Add id 'note-claim-refresh' to plugins.allow + its path to plugins.load.paths, then restart."
  else
    DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_PLUGIN_DST="$PLUGIN_DST" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, os

path = os.environ["DINOMEM_OPENCLAW_JSON"]
plugin_dst = os.environ["DINOMEM_PLUGIN_DST"]
ws = os.environ["DINOMEM_WS"]
PID = "note-claim-refresh"
with open(path) as f:
    cfg = json.load(f)

changed = []
plugins = cfg.setdefault("plugins", {})

# 1) allowlist (only if an allow list is enforced; create it if absent so the
#    plugin is explicitly permitted)
allow = plugins.get("allow")
if not isinstance(allow, list):
    allow = []
    plugins["allow"] = allow
if PID not in allow:
    allow.append(PID)
    changed.append(f"plugins.allow += {PID}")

# 2) load.paths -> abs plugin dir (dedup)
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
    load["paths"] = paths
if plugin_dst not in paths:
    paths.append(plugin_dst)
    changed.append(f"plugins.load.paths += {plugin_dst}")

# 3) per-plugin config.workspaceDir (robustness on multi-workspace hosts)
pconf = plugins.get("config")
if not isinstance(pconf, dict):
    pconf = {}
    plugins["config"] = pconf
entry = pconf.get(PID)
if not isinstance(entry, dict):
    entry = {}
    pconf[PID] = entry
if entry.get("workspaceDir") != ws:
    entry["workspaceDir"] = ws
    changed.append(f"plugins.config.{PID}.workspaceDir -> {ws}")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m wire openclaw.json: {c}")
elif not changed:
    print("  \033[33m[skip]\033[0m note-claim-refresh already wired in openclaw.json")
else:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print(f"  \033[32m[ok]\033[0m   wired: {c}")
    print("  \033[33m[warn]\033[0m Restart OpenClaw to load the plugin: openclaw gateway restart")
PYEOF
  fi
fi

echo
# ── (B) Post-install self-check summary ──────────────────────────────────────
# One glanceable PASS/WARN table so an agent-installer immediately sees what
# actually landed (crons, TEI, python deps, config) instead of discovering gaps
# piecemeal. Non-fatal: this only REPORTS, never exits nonzero.
hr "Self-check"
SC_FAIL=0
sc_pass() { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
sc_warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; SC_FAIL=1; }
# 0) CONFIG VALIDITY + GATEWAY LIVENESS — the 'bot goes silent' guard. A config that
# fails schema validation will crash the gateway on the next restart (total silence),
# so this is the single most important check. If a gateway is already running, also
# confirm it still answers (a half-broken restart shows up here).
if command -v openclaw >/dev/null 2>&1; then
  if openclaw config validate >/dev/null 2>&1; then
    sc_pass "openclaw.json passes schema validation (safe to restart)"
  else
    sc_warn "openclaw.json FAILS schema validation — DO NOT restart yet; gateway would not come back. Run: openclaw config validate"
  fi
  if openclaw status >/dev/null 2>&1; then
    sc_pass "OpenClaw gateway responding now (restart to load dinomem changes)"
  else
    sc_warn "OpenClaw gateway not responding to 'openclaw status' — start/restart it: openclaw gateway restart"
  fi
else
  sc_warn "openclaw CLI not found on PATH — cannot validate config or check gateway"
fi
# 1) OpenClaw crons present (by name)
if command -v openclaw >/dev/null 2>&1; then
  _CRON_LIST="$(openclaw cron list --json 2>/dev/null || echo '[]')"
  for _cname in "Project Advancer" "Project Improver (Component 2)" "Verified Note Deleter (Component 1)" "Project Cron Gate" "Daily Note Review"; do
    if printf '%s' "$_CRON_LIST" | grep -qiF "$_cname"; then
      sc_pass "cron present: $_cname"
    else
      sc_warn "cron MISSING: $_cname  (add via the OpenClaw cron tool)"
    fi
  done
else
  sc_warn "openclaw CLI not found on PATH — cannot verify crons"
fi
# 2) TEI embed server /health
if curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${TEI_PORT:-8080}/health" 2>/dev/null | grep -q '^200$'; then
  sc_pass "TEI embed server healthy on :${TEI_PORT:-8080}"
else
  sc_warn "TEI /health not 200 on :${TEI_PORT:-8080} — start it or run dinomem doctor.sh"
fi
# 3) Key python deps importable
for _mod in langchain_chroma langchain_huggingface numpy pandas; do
  if "$PY" -c "import $_mod" >/dev/null 2>&1; then
    sc_pass "python import: $_mod"
  else
    sc_warn "python import FAILED: $_mod  (re-run: $PY -m pip install $_mod)"
  fi
done
# 4) memorySearch wired to dinomem TEI in openclaw.json
if [ -f "$OPENCLAW_JSON" ] && grep -q '"memorySearch"' "$OPENCLAW_JSON" 2>/dev/null; then
  sc_pass "openclaw.json has memorySearch config"
else
  sc_warn "openclaw.json memorySearch not detected — embeddings may not be wired"
fi
if [ "$SC_FAIL" = 0 ]; then
  printf '  \033[32m== self-check: all green ==\033[0m\n'
else
  printf '  \033[33m== self-check: some items need attention (see WARN above) ==\033[0m\n'
fi
echo ""

hr "done"
echo "  dinomem-neuron installed for agent: $AGENT_ID"
echo "  workspace: $WS"
echo ""
echo "  Next steps:"
echo "  1. Restart OpenClaw:       openclaw gateway restart"
echo "  2. Run first graph build:  python3 $WS/procedures/memory_graph.py"
echo "  3. Run first synthesis:    python3 $WS/procedures/memory_synthesis.py"
echo "  4. Verify TEI/embed:       bash <dinomem-base>/scripts/doctor.sh"
echo ""
echo "  AGENT-INSTALLER NOTE: to verify the install, run dinomem's doctor.sh (step 4;"
echo "     pure curl, non-interactive). Do NOT run OpenClaw's own 'openclaw doctor' in an"
echo "     unattended/agent session -- it is INTERACTIVE and blocks waiting for TTY input."
echo "     If you must run it, use a non-TTY 'openclaw doctor --fix', or skip it and fix"
echo "     config directly."
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
echo "  License: Proprietary — unauthorized redistribution prohibited. https://t.me/dinotlgrm"
echo "  Undo: bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
