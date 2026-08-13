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
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-cron] [--repair-cron] [--force] [--agree] [--no-auto-base]
#
#   --repair-cron : re-register ONLY the OpenClaw crons (Advancer/Improver/Deleter
#                   + Note Cron Gate + Daily Note Review) and re-run the self-check.
#                   Idempotent; skips base refresh, pip, TEI, file copy, config wiring.
#                   Use when a fresh install left some crons unregistered.
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
# --repair-cron: idempotent "just fix the crons" mode. Skips the heavy/one-time
# phases (base refresh, pip, TEI, file copy, AGENTS/TOOLS/config/hook wiring) and
# flows straight to the cron registration + gate + self-check. Safe to re-run any
# time a fresh install left the Advancer/Improver/Deleter/Gate lanes unregistered.
REPAIR_CRON=0
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
    --repair-cron)    REPAIR_CRON=1; DO_CRON=1; NO_AUTO_BASE=1; LICENSE_AGREED=1; shift ;;
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
  local base_branch="${DINOMEM_BASE_BRANCH:-main}"
  # ALWAYS force the base clone to the remote's latest tip. The old code used a
  # shallow clone + `pull --ff-only` whose failure was a soft warn, so a stale
  # base (e.g. one cloned months ago, predating the flag-based cron migration)
  # would silently persist while neuron advanced — the base then ran its OLD
  # install.sh (legacy cron API) and broke registration. We now HARD-SYNC:
  # fetch the remote tip and reset --hard to it, so "base already exists" can
  # never mean "base is stale". Failure to sync a REQUIRED dependency is loud.
  if [ -d "$clone_dir/.git" ]; then
    ok "dinomem base repo present at $clone_dir — hard-syncing to origin/$base_branch (latest)"
    # unshallow if it was a --depth 1 clone (so reset --hard has real history);
    # ignore error if already complete. Then fetch tip + reset hard.
    git -C "$clone_dir" fetch --depth 1 origin "$base_branch" >/dev/null 2>&1 \
      || git -C "$clone_dir" fetch origin "$base_branch" >/dev/null 2>&1 \
      || { warn "git fetch failed for base ($DINOMEM_BASE_REPO) — cannot verify latest"; return 1; }
    git -C "$clone_dir" reset --hard FETCH_HEAD >/dev/null 2>&1 \
      || { warn "git reset --hard FETCH_HEAD failed — base may be stale"; return 1; }
    ok "base synced -> $(git -C "$clone_dir" rev-parse --short HEAD 2>/dev/null)"
  elif [ -e "$clone_dir" ]; then
    warn "$clone_dir exists but is not a git repo — cannot verify it is latest; re-cloning fresh"
    rm -rf "$clone_dir"
    git clone --depth 1 --branch "$base_branch" "$DINOMEM_BASE_REPO" "$clone_dir" >/dev/null 2>&1 \
      || { warn "git clone failed: $DINOMEM_BASE_REPO"; return 1; }
    ok "base cloned -> $(git -C "$clone_dir" rev-parse --short HEAD 2>/dev/null)"
  else
    ok "cloning dinomem base: $DINOMEM_BASE_REPO -> $clone_dir"
    git clone --depth 1 --branch "$base_branch" "$DINOMEM_BASE_REPO" "$clone_dir" >/dev/null 2>&1 \
      || { warn "git clone failed: $DINOMEM_BASE_REPO"; return 1; }
    ok "base cloned -> $(git -C "$clone_dir" rev-parse --short HEAD 2>/dev/null)"
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
# Audio/video/URL ingest tools (optional — enable transcription).
# ffmpeg extracts audio from any media; yt-dlp fetches stream URLs + captions;
# faster-whisper is the no-key/no-GPU local ASR fallback (subtitle-first and any
# DEEPGRAM_API_KEY are tried before it). All optional: missing → audio/video/URL
# ingest degrades gracefully with a clear message; every other format still works.
if command -v ffmpeg &>/dev/null; then ok "ffmpeg present — audio/video ingest enabled"; else
  warn "ffmpeg missing — audio/video ingest needs it. Attempting install..."
  pm_install ffmpeg \
    && command -v ffmpeg &>/dev/null && ok "ffmpeg installed" \
    || warn "  could not auto-install — audio/video ingest disabled until you install ffmpeg. Other formats still work."
fi
if command -v yt-dlp &>/dev/null; then ok "yt-dlp present — URL/stream ingest enabled"; else
  warn "yt-dlp missing — URL ingest needs it. Attempting install (pip)..."
  pip_install yt-dlp \
    && ok "yt-dlp installed (pip)" \
    || warn "  could not auto-install — URL ingest disabled until you install yt-dlp. File ingest still works."
fi
# faster-whisper: local ASR fallback. ~91MB deps (ctranslate2/av), NO torch, NO
# GPU. Only the fallback rung — subtitle grab + any Deepgram key run first.
if "$PY" -c 'import faster_whisper' 2>/dev/null; then ok "faster-whisper present — local transcription fallback enabled"; else
  warn "faster-whisper missing — installing local ASR fallback (no GPU/torch)..."
  pip_install faster-whisper \
    && "$PY" -c 'import faster_whisper' 2>/dev/null && ok "faster-whisper installed" \
    || warn "  could not auto-install faster-whisper — audio/video transcription will skip unless a DEEPGRAM_API_KEY is set or captions exist. Install: $PY -m pip install faster-whisper"
fi
# Optional hosted transcript API (bulletproof URL ingest on blocked/datacenter IPs).
# yt-dlp scrapes from THIS machine's IP; on a flagged/datacenter IP YouTube may
# refuse it. A hosted API runs on the vendor's own IPs, sidestepping that wall —
# it becomes the PRIMARY URL rung the moment a key is set, else silently skipped
# (zero setup). No install action needed; this is just a heads-up in the log.
if [ -n "${SUPADATA_API_KEY:-}" ]; then
  ok "SUPADATA_API_KEY set — hosted caption API is the primary URL rung (bypasses local IP blocks)"
else
  echo "  ℹ️  SUPADATA_API_KEY not set (optional) — URL ingest uses yt-dlp (captions→whisper). Works on clean/residential IPs; set SUPADATA_API_KEY (free tier at supadata.ai) for bulletproof URL ingest on blocked/datacenter IPs."
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

# ── REPAIR-CRON FAST PATH ─────────────────────────────────────────────────────
# In --repair-cron mode we skip every heavy/one-time phase (pip, TEI, dir create,
# file copy, hook + skill copy) and jump straight to cron registration + gate +
# self-check. These phases are idempotent on a normal install, but re-running them
# is pointless (and slow) when the ONLY thing we're fixing is unregistered crons.
if [ "$REPAIR_CRON" = 0 ]; then

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
  pip_install duckdb && ok "duckdb" || warn "duckdb install failed — data_query / structured-data feature OFFLINE (semantic tools unaffected; loud recap below)"
)
py_can_import yaml && skip "pyyaml (installed)" || (
  pip_install pyyaml && ok "pyyaml" || warn "pyyaml install failed"
)
py_can_import googleapiclient && skip "google-api-python-client (installed)" || (
  pip_install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 && ok "google-api-python-client" || warn "google-api-python-client install failed"
)

# ── spaCy (SVO relation-mining) — MINIMUM-gated auto-install, no user choice ────
# spaCy + en_core_web_sm powers the memory graph's SVO layer: verb-typed
# subject->object relation edges auto-mined from memory prose (e.g. "X acquired
# Y" -> a typed 'acquire' edge), enriching graph_search's relational leg
# (why/led-to/affects/trace). The graph is FULLY FUNCTIONAL without it (similarity
# + explicit [relation] edges); SVO is an ENRICHMENT of one leg.
#
# GATE PHILOSOPHY (two-tier — MINIMUM decides install, RECOMMENDED only informs):
#   * MINIMUM (hard): "can it even run?" spaCy loads en_core_web_sm in ~200MB on
#     1 core — so anything that runs the base clears this. We only HARD-SKIP on a
#     genuinely tiny box (< SPACY_MIN_RAM_MB) where the model load would fight the
#     graph build for RAM. RAM is the real can-it-load axis.
#   * RECOMMENDED (soft, informational): "will it be slow?" SVO parse is single-
#     threaded per doc; with < SPACY_REC_CPU cores rebuilds take extra minutes.
#     We STILL INSTALL, but print a stated note so the noob isn't surprised by
#     slower cron/reset rebuilds. Cores decide the note, NOT the install.
# Net: install-by-default wherever it can run; note-if-slow; hard-skip only on
# tiny-RAM. No flag, no prompt. TOTAL_RAM_MB / CPU_COUNT come from preflight.
SPACY_MIN_RAM_MB=2048     # hard floor: below this, model load competes with graph build -> skip
SPACY_REC_CPU=3           # recommended cores: below this, install anyway + slow-rebuild note
_spacy_slow_note() {
  if [ "${CPU_COUNT:-0}" -gt 0 ] && [ "${CPU_COUNT:-0}" -lt "$SPACY_REC_CPU" ]; then
    warn "  note: ${CPU_COUNT}-core host is below the recommended ${SPACY_REC_CPU} cores for SVO — graph rebuilds will be slower (single-threaded parse). Runs on cron/reset, so interactive use is unaffected."
  fi
}
if py_can_import spacy && "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
  skip "spacy + en_core_web_sm (installed) — SVO relation-mining ON"
  _spacy_slow_note
elif [ "${TOTAL_RAM_MB:-0}" -gt 0 ] && [ "${TOTAL_RAM_MB:-0}" -lt "$SPACY_MIN_RAM_MB" ]; then
  # Below the HARD RAM minimum -> genuinely can't run comfortably; skip w/ reason.
  skip "spacy — SVO OFF: host ${TOTAL_RAM_MB}MB below hard minimum ${SPACY_MIN_RAM_MB}MB (model load would fight the graph build). Graph fully functional without it."
else
  # Clears the hard minimum (or RAM unknown) -> INSTALL BY DEFAULT. Slow-rebuild
  # note fires separately if cores are below the recommended bar.
  if pip_install spacy; then
    # Install the MODEL through pip_install (NOT `spacy download`) so it inherits
    # the PEP-668 --break-system-packages fallback + retry loop. `spacy download`
    # bypasses pip_install and hard-fails on externally-managed hosts / flaky nets
    # (verified live 2026-08-12: PEP 668 blocked it; RemoteDisconnected reset it).
    # en_core_web_sm is NOT on PyPI -> install from spaCy's release wheel URL, tag
    # matched to the installed spaCy major.minor (models track spaCy's X.Y line).
    _spacy_xy="$("$PY" -c 'import spacy;print(".".join(spacy.__version__.split(".")[:2]))' 2>/dev/null)"
    _model_ver="${_spacy_xy:-3.8}.0"
    _model_whl="https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-${_model_ver}/en_core_web_sm-${_model_ver}-py3-none-any.whl"
    if pip_install "$_model_whl" && "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
      ok "spacy + en_core_web_sm (${_model_ver}) — SVO relation-mining ON"
      _spacy_slow_note
    elif pip_install en_core_web_sm && "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
      # Fallback: some spaCy versions publish the model as a plain pip name too.
      ok "spacy + en_core_web_sm — SVO relation-mining ON"
      _spacy_slow_note
    else
      warn "spacy installed but en_core_web_sm model did not install — SVO stays OFF. Fix: $PY -m pip install $_model_whl  (add --break-system-packages if PEP-668)"
    fi
  else
    warn "spacy install failed — SVO relation-mining OFF (graph still works on similarity + explicit relations)"
  fi
fi

# ── HARD post-install assertion: the langchain trio GATES the KB-search tools ──
# These are NOT a nicety — docs_search / session_search / semantic_search (and
# their ingest procedures) hard-fail at import without them. A buried per-package
# warn is easy to skim past, so re-check the actual imports here and, if any are
# missing, surface ONE prominent, actionable block naming exactly which tools are
# offline + the copy-paste fix. Core memory_search is TEI/HTTP-only and unaffected,
# so we do NOT block the install — but we make the gap impossible to miss.
KB_DEPS_MISSING=""
for _d in langchain_chroma langchain_huggingface langchain_core; do
  py_can_import "$_d" || KB_DEPS_MISSING="${KB_DEPS_MISSING}${KB_DEPS_MISSING:+ }$_d"
done
if [ -n "$KB_DEPS_MISSING" ]; then
  echo ""
  printf '  \033[1;33m⚠ KB search tools are OFFLINE — Python deps did not install:\033[0m %s\n' "$KB_DEPS_MISSING"
  echo "     Affected (these will error until fixed): docs_search, session_search"
  echo "     (+ their ingest: docs_ingest, session_ingest). Your core memory_search is UNAFFECTED."
  # NB: graph_search is stdlib-only (unaffected); data_query needs duckdb, not langchain.
  printf '     \033[1mFix (copy-paste):\033[0m %s -m pip install langchain-chroma langchain-huggingface langchain-core\n' "$PY"
  echo "     If that errors with 'externally-managed-environment', append: --break-system-packages"
  echo "     Then re-run this installer (idempotent) to finish wiring the KB tools."
  echo ""
fi

# ── HARD post-install assertion: duckdb GATES data_query (structured-data SQL) ──
# Same principle as the KB trio: a feature that legit can't run without its dep
# must not fail SILENTLY. duckdb was Tier-B (buried one-line warn); promote it to
# a loud, named, copy-paste-fixable recap so the noob knows data_query is OFFLINE
# and how to fix it, instead of hitting an import error later with no context.
if ! py_can_import duckdb; then
  echo ""
  printf '  \033[1;33m⚠ data_query is OFFLINE — duckdb did not install.\033[0m\n'
  echo "     Affected (will error until fixed): data_query (structured CSV/JSON/parquet SQL)"
  echo "     (+ its ingest: data_register). memory_search / graph_search / KB tools are UNAFFECTED."
  printf '     \033[1mFix (copy-paste):\033[0m %s -m pip install duckdb\n' "$PY"
  echo "     If that errors with 'externally-managed-environment', append: --break-system-packages"
  echo "     Then re-run this installer (idempotent)."
  echo ""
fi

# ── spaCy SVO recap: STATE its status (installed / off-by-spec / failed) ───────
# SVO is an ENRICHMENT (graph works without it), so a missing spaCy is NOT a
# broken promise the way duckdb/KB are — but silent-nothing still leaves the noob
# guessing why their relational recall is thinner. So we STATE the outcome either
# way: ON (installed), OFF-by-design (below the hard RAM minimum, intentional), or
# FAILED (cleared the minimum but pip/model download broke -> here's the fix).
if py_can_import spacy && "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
  : # ON — already surfaced via ok/skip in the dep pass; no recap needed.
elif [ "${TOTAL_RAM_MB:-0}" -gt 0 ] && [ "${TOTAL_RAM_MB:-0}" -lt "${SPACY_MIN_RAM_MB:-2048}" ]; then
  : # OFF-by-design (below hard RAM minimum) — already stated via skip in the dep pass; intentional, no error recap.
else
  # Cleared the hard RAM minimum (or RAM unknown) yet spaCy still isn't importable
  # -> a real install FAILURE. Under the two-tier gate, few-cores does NOT skip the
  # install (it installs + notes slow rebuilds), so a not-importable spaCy here is a
  # genuine pip/model break, not a spec decision.
  if ! (py_can_import spacy && "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1); then
  echo ""
  printf '  \033[1;33m⚠ SVO relation-mining is OFF — spaCy cleared the spec gate but did not install.\033[0m\n'
  echo "     Impact: graph_search's relational leg (why/led-to/affects/trace) is THINNER"
  echo "     (auto-mined verb-typed relation edges absent). Similarity + explicit relations UNAFFECTED."
  printf '     \033[1mFix (copy-paste):\033[0m %s -m pip install spacy && %s -m pip install "$_model_whl"\n' "$PY" "$PY"
  echo "     If pip errors with 'externally-managed-environment', append --break-system-packages to each pip step."
  echo "     (Model is NOT on PyPI; install it from spaCy's release wheel URL, not 'spacy download'.)"
  echo "     Then re-run this installer (idempotent)."
  echo ""
  fi
fi

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
for f in procedures/extract_memory.py procedures/memory_graph.py procedures/graph_store.py procedures/graph_store_test.py procedures/memory_synthesis.py procedures/memory_promote.py procedures/contradiction_check.py procedures/confidence_engine.py procedures/lifecycle_state.py procedures/explain_memory.py procedures/generate_topic_index.py procedures/docs_ingest.py procedures/pdf_visual_probe.py procedures/session_ingest.py procedures/_schema_adapter.py procedures/_cheap_model.py procedures/valid_time.py procedures/data_register.py tools/docs_search.py tools/session_search.py tools/graph_search.py tools/fact_query.py tools/hybrid_recall.py tools/cache_utils.py tools/data_query.py scripts/claim_note.sh scripts/check_projects.sh scripts/check_improvable.sh scripts/check_deletable.sh scripts/check_ideator.sh scripts/cron_gate.sh scripts/resolve_done_notes.py scripts/distill_done_note.py; do
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

# ── Install open-notes hook (NEURON SUPERSET — overwrites base's) ────────────
# Neuron's dinomem-open-notes hook adds the session_search cold-start recall line
# on top of base's open-note manifest. Base's installer ran FIRST and placed
# base's version; this MUST overwrite it even without --force (like
# extract_memory.py) or the upgrade silently keeps base's hook without neuron's
# recall behavior. rm -rf before cp prevents the cp -r nesting footgun.
if [ -d "$SKILL_DIR/hooks/dinomem-open-notes" ]; then
  hr "Open-notes hook (neuron superset: open-note manifest + session_search recall)"
  HOOK_SRC="$SKILL_DIR/hooks/dinomem-open-notes"
  HOOK_DST="$WS/hooks/dinomem-open-notes"
  mkdir -p "$WS/hooks"
  rm -rf "$HOOK_DST"
  cp -r "$HOOK_SRC" "$HOOK_DST"
  ok "hooks/dinomem-open-notes/ copied (neuron superset overwrites base)"
  if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
    openclaw hooks enable dinomem-open-notes >/dev/null 2>&1 \
      && ok "dinomem-open-notes hook enabled (restart OpenClaw to activate)" \
      || warn "openclaw hooks enable failed — run manually: openclaw hooks enable dinomem-open-notes"
  else
    warn "OpenClaw not running — run after restart: openclaw hooks enable dinomem-open-notes"
  fi
fi

# ── Install neuron skills (SUPERSET — overwrites base's same-named skills) ───
# memory-pinning is the neuron superset (adds the dedup gate) and overwrites
# base's copy; project-notes / retrieval-routing / note-claim are neuron-only.
# Always overwrite (base ran first) so the upgrade ships neuron's versions.
# rm -rf before cp prevents the cp -r nesting footgun.
if [ -d "$SKILL_DIR/skills" ]; then
  hr "Skills (project-notes, memory-pinning, retrieval-routing, note-claim)"
  for _sk in "$SKILL_DIR/skills"/*/; do
    [ -d "$_sk" ] || continue
    _skname="$(basename "$_sk")"
    _skdst="$WS/skills/$_skname"
    mkdir -p "$WS/skills"
    rm -rf "$_skdst"
    cp -r "$_sk" "$_skdst"
    # Bake the real workspace path into skill bodies so script-call examples
    # resolve at agent runtime (agent shell has no $WS). Matches the sed pass
    # used for procedures/tools scripts above.
    find "$_skdst" -name '*.md' -exec sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" {} +
    ok "skills/$_skname/ copied"
  done
fi

# ── Install benchmark suite (self-contained memory-quality A/B harness) ───────
# Ships run_benchmark.py + fixtures/seed.py + scenarios/*.jsonl. No placeholder
# sed needed — the runner resolves tool paths relative to its own ROOT (the WS).
# Overwrite code/fixtures/scenarios (upgrade ships neuron's versions) but never
# clobber a user's results/ (their prior benchmark runs). Gives installers a
# one-command health-check:  python3 $WS/benchmark/run_benchmark.py --all
if [ -d "$SKILL_DIR/benchmark" ]; then
  hr "Benchmark suite (memory-quality A/B)"
  mkdir -p "$WS/benchmark/results"
  cp "$SKILL_DIR/benchmark/run_benchmark.py" "$WS/benchmark/run_benchmark.py"
  [ -f "$SKILL_DIR/benchmark/README.md" ] && cp "$SKILL_DIR/benchmark/README.md" "$WS/benchmark/README.md"
  mkdir -p "$WS/benchmark/fixtures"
  cp "$SKILL_DIR/benchmark/fixtures/seed.py" "$WS/benchmark/fixtures/seed.py"
  [ -f "$SKILL_DIR/benchmark/fixtures/atlas_metrics.csv" ] && cp "$SKILL_DIR/benchmark/fixtures/atlas_metrics.csv" "$WS/benchmark/fixtures/atlas_metrics.csv"
  rm -rf "$WS/benchmark/scenarios"
  cp -r "$SKILL_DIR/benchmark/scenarios" "$WS/benchmark/scenarios"
  ok "benchmark/ copied (run: python3 $WS/benchmark/run_benchmark.py --all)"
fi

fi  # end REPAIR-CRON FAST PATH (heavy phases)
if [ "$REPAIR_CRON" = 1 ]; then
  hr "repair-cron mode"
  ok "skipping base/pip/TEI/copy/config phases — re-registering crons only"
fi

# ── 3) Register cron jobs ─────────────────────────────────────────────────────
# upsert_cron: add or update a cron entry by script keyword
upsert_cron() {
  local keyword="$1" comment="$2" cron_line="$3" label="$4"
  local existing
  # AGENT-SCOPED MATCH (multi-agent safety): the crontab is per-USER(root), shared
  # by every agent on this host. A bare `grep "$keyword"` (e.g. memory_graph.py)
  # matches EVERY agent's line, so `grep -v` would strip another agent's cron.
  # We scope the match by the workspace fingerprint ("cd $WS &&"), which is unique
  # per agent, AND suffix the comment marker with [agent:$AGENT_ID] for clarity.
  # Fallback: if $WS is somehow empty, degrade to the old keyword match (single-
  # agent buyer path is unaffected — one agent, one line).
  local scope
  if [ -n "${WS:-}" ]; then scope="cd $WS && .*$keyword"; else scope="$keyword"; fi
  comment="$comment [agent:${AGENT_ID:-default}]"
  existing=$(crontab -l 2>/dev/null | grep -E "$scope" || true)
  if [ "$existing" = "$cron_line" ]; then
    skip "$label (exists, up to date)"
  elif [ -n "$existing" ]; then
    { crontab -l 2>/dev/null | grep -vE "$scope"; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (updated)"
  else
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (registered)"
  fi
}

if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # crond does not inherit the interactive shell, so model-routing env vars must
  # be baked into the cron lines or they are silently ignored on scheduled runs.
  # CHEAP_ENV  -> non-reasoning crons (topic_index, docs_ingest): route to the
  #              cheap model when DINOMEM_CHEAP_MODEL is set; unset = default-safe.
  # REASON_ENV -> reasoning crons (synthesis, contradiction_check): carry the
  #              thinking level when DINOMEM_REASONING_THINKING is set.
  CHEAP_ENV=""
  if [ -n "${DINOMEM_CHEAP_MODEL:-}" ]; then
    CHEAP_ENV="DINOMEM_CHEAP_MODEL=$DINOMEM_CHEAP_MODEL "
    ok "cron cheap model: $DINOMEM_CHEAP_MODEL"
  fi
  REASON_ENV=""
  if [ -n "${DINOMEM_REASONING_THINKING:-}" ]; then
    REASON_ENV="DINOMEM_REASONING_THINKING=$DINOMEM_REASONING_THINKING "
    ok "cron reasoning thinking: $DINOMEM_REASONING_THINKING"
  fi

  # memory_graph — daily at 3:00 UTC (L2, no LLM — pure graph linking)
  GRAPH_CRON="0 3 * * * cd $WS && python3 procedures/memory_graph.py >> logs/memory_graph.log 2>&1"
  upsert_cron "memory_graph.py" "dinomem-neuron: L2 graph linking" "$GRAPH_CRON" "memory_graph cron (daily 3:00 UTC)"

  # memory_synthesis + contradiction_check — daily at 3:30 UTC (L3, reasoning=True)
  # confidence_engine (P1 #1, no-LLM calibration) runs AFTER synthesis writes
  # current_insights.json and AFTER contradiction_check, BEFORE the weekly promote
  # reads it — so the gate inherits calibrated confidence with zero downstream
  # change. Chained into the same daily L3 run; pure bookkeeping, cannot OOM.
  SYNTH_CRON="30 3 * * * cd $WS && ${REASON_ENV}python3 procedures/memory_synthesis.py >> logs/memory_synthesis.log 2>&1 && ${REASON_ENV}python3 procedures/contradiction_check.py >> logs/contradiction_check.log 2>&1 && python3 procedures/confidence_engine.py >> logs/confidence_engine.log 2>&1"
  upsert_cron "memory_synthesis.py" "dinomem-neuron: L3 synthesis + contradiction check + calibration" "$SYNTH_CRON" "memory_synthesis + contradiction_check + confidence_engine cron (daily 3:30 UTC)"

  # memory_promote (no LLM) + generate_topic_index (no-reasoning) — weekly Sun 4:00 UTC (L4)
  PROMOTE_CRON="0 4 * * 0 cd $WS && python3 procedures/memory_promote.py >> logs/memory_promote.log 2>&1 && ${CHEAP_ENV}python3 procedures/generate_topic_index.py >> logs/topic_index.log 2>&1"
  upsert_cron "memory_promote.py" "dinomem-neuron: L4 promotion + topic index (weekly)" "$PROMOTE_CRON" "memory_promote + generate_topic_index cron (weekly Sunday 4:00 UTC)"

  # docs_ingest — daily at 4:00 UTC (RAG long docs, no-reasoning)
  DOCS_CRON="0 4 * * * cd $WS && ${CHEAP_ENV}python3 procedures/docs_ingest.py >> logs/docs_ingest.log 2>&1"
  upsert_cron "docs_ingest.py" "dinomem-neuron: RAG docs re-ingest (daily)" "$DOCS_CRON" "docs_ingest cron (daily 4:00 UTC)"
  # session_ingest is triggered automatically by auto_session_reset.py (every 15 min)
  # No separate cron needed.

  # retrieval_log rollup — daily at 4:15 UTC (no LLM, pure stdlib): compresses
  # dailies older than 30d into monthly rollups + prunes raw, refreshes health.json
  # (routing-quality snapshot: calls/fallback-rate/empty-rate per retrieval tool).
  RLOG_CRON="15 4 * * * cd $WS && python3 procedures/_retrieval_log.py --workspace $WS --rollup >> logs/retrieval_log_rollup.log 2>&1"
  upsert_cron "_retrieval_log.py --rollup" "dinomem-neuron: retrieval-log rollup + health snapshot (daily)" "$RLOG_CRON" "retrieval_log rollup cron (daily 4:15 UTC)"

  # project_advancer — OpenClaw agentTurn cron (NOT shell crontab): auto-advance type:project in_progress notes
  # one step per run, unattended, announces progress. Premium: only neuron installs this.
  DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
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
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Note Cron Gate) \u2713 verified")
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
        "message": "Project Advancer. Auto-advance step-by-step project notes unattended.\n\nSILENT-STOP CONTRACT: whenever an instruction below says 'silent' / 'no announce' / 'stop immediately', your ENTIRE final message for this run must be exactly the literal token NO_REPLY and nothing else — no extra sentence. This is what makes the cron delivery layer suppress the run; a descriptive sentence like 'nothing to advance' still gets delivered if delivery is not mode:none. REAL reports (state actually changed, safety-gate pause, flagged issue) must be sent explicitly via the message tool to the owner's default channel — do not rely on the runner's fallback announce, since delivery may be mode:none.\n\nStep 0: PRE-CHECK (zero LLM cost gate). Run: bash $WS/scripts/check_projects.sh\nIf exit code != 0 -> no in_progress project notes exist. Stop immediately. Output exactly NO_REPLY and nothing else.\n\nStep 1: ls memory/_note_*.md in $WS/memory/. Read each. Select only notes with type: project AND status: in_progress.\n\nStep 1b — HEAVINESS CHECK (pre-task): Before advancing any project, look at its NEXT unchecked step. If that step is heavy (write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle), commit to doing ONLY that one step this run. If it is light (tiny edit/config/doc/one-liner), you may chain several light steps in this run. Turn boundaries are decided by step heaviness, not by context %.\n\nStep 1c: If no in_progress notes AND nothing flagged, stop silently. Output exactly NO_REPLY and nothing else.\n\nStep 2 — CLAIM PRE-CHECK (cheap filter only; the AUTHORITATIVE gate is Step 3). ALL timestamps are UTC ISO8601; compute claim age in UTC. For each in_progress note, read claimed_by and claimed_at fields. (G6 live-session guard) If claimed_by STARTS WITH 'live-session' AND claimed_at is less than 120 minutes (2 hours) ago, SKIP this note entirely — a human is actively working it in an interactive session; never fight a live session. If claimed_by is set (any other value) AND claimed_at is less than 30 minutes ago, SKIP this note entirely (another Advancer instance is working it). This prose check only avoids spawning the claim script on an obviously-busy note; it is NOT trusted to be race-safe on its own — Step 3 is the real lock.\n\nStep 3 — CLAIM (ATOMIC, via the shared lock — do NOT hand-write claimed_by). Run EXACTLY: bash $WS/scripts/claim_note.sh claim <full note path> advancer-<this run's cron run id or a short uuid>. Interpret the exit code: exit 0 -> you now hold the claim atomically (flock + verified write); proceed to Step 4. exit 3 -> BUSY: a fresh claim (a live session or another job) holds this note; SKIP it entirely this run — do NOT edit it, do NOT fight, do NOT hand-write a claim; move to the next note. Any other non-zero -> treat as unable-to-claim; SKIP this note this run. NEVER write claimed_by/claimed_at yourself with the edit tool — claim_note.sh is the ONLY writer, so your claim goes through the SAME flock the live session uses. This closes the claim-clobber TOCTOU race: previously this step hand-wrote the claim unlocked and could overwrite a live session's claim landing in the read->write gap. If you keep editing the same note across chained light steps, re-assert the claim with bash $WS/scripts/claim_note.sh refresh <note> advancer-<id>.\n\nStep 4: For each claimed in_progress project note, pick the lowest unchecked step at current_step. Resume from resume_state ONLY (the note is self-sufficient; do not rely on chat history).\n\nStep 4a — WHOLE-PROJECT DONE_WHEN PRECHECK (run BEFORE anything else, even when parked at an early/non-verifiable step): Read the note's TOP-LEVEL done_when (the whole-project completion condition, not the current step). If any part of it is locally checkable against a real artifact (file/dir exists, grep of a named code anchor, test \"$(git -C <repo> rev-parse HEAD)\" = \"$(git -C <repo> rev-parse @{u})\", feature shipped + live in sync), VERIFY IT NOW regardless of what resume_state says — a stale resume_state like 'awaiting owner sign-off' / 'do NOT write code yet' is NOT trusted over the artifact, because the work may have shipped out-of-band in a live session and the note never got written back. If the whole-project done_when verifies against the artifact: mark ALL steps [x], flip status to done, rewrite resume_state to 'DONE (verified against <artifact> on <date>)', release the claim, report the closure, and STOP processing this note. Only if the top-level done_when does NOT verify (or is not artifact-checkable, e.g. a pure human sign-off with no shipped artifact) do you fall through to Step 4b. This closes the rot-gap where a fully-shipped project parked at step 1 stays in_progress forever because nobody checked the final artifact.\n\nStep 4b — DRIFT CHECK (verify before executing): Do NOT assume current_step's checkbox state reflects reality — notes can drift (e.g. work finished in a live human session but the checkbox never flipped, or a companion note already covered this step). Before treating this step as pending work, independently verify whether its OWN done_when is already satisfied against real state (file/artifact exists, test "$(git -C <repo> rev-parse HEAD)" = "$(git -C <repo> rev-parse @{u})", feature already shipped, companion note already did it, etc — the same kind of locally-checkable verification Daily Note Review uses). If already satisfied: mark it [x], advance current_step, rewrite resume_state, then repeat this same Step 4b check for the NEW current_step (loop until you hit a step that is genuinely NOT done, or the whole project completes) — treat this drift-catchup as part of the same LIGHT turn, not a new heavy step. If genuinely not satisfied, proceed to Step 5 normally. OPTIONAL git corroboration (only if $WS/memory is git-tracked; skip silently if not): you MAY run 'git -C $WS log --oneline -3 -- memory/<note>' to see when the note itself was last edited, as a cheap sanity check alongside artifact verification. Never required — absence of git changes nothing about how drift is verified.\n\nStep 5: SAFETY GATE before executing the step. If the step is destructive or external (deletes data, git push, sends external messages, irreversible writes outside workspace, spends money, touches credentials), DO NOT execute it. Leave it pending, clear your claim (Step 8), send a message via the message tool to the owner that the project is paused awaiting approval at step n/m — tag the owner (use platform-appropriate mention format), move to the next project.\n\nStep 6: Execute the one current step. After it: verify the step done_when. If met, mark [x], advance current_step, REWRITE resume_state with what the next step needs (paths, decisions, assumptions). If not met, leave pending and record why in resume_state.\n\nStep 7: Turn-boundary guard (heaviness-based). Gauge the heaviness of the NEXT step. Heavy = write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle. If the next step is heavy, do only the current step this run, checkpoint (state already written), CLEAR claimed_by and claimed_at, and stop advancing this run — do NOT chain another heavy step. Light steps (tiny edit/config/doc/one-liner) may be chained: continue to the next step of the same project until the next step is heavy or the project is done.\n\nStep 8 — CLAIM RELEASE (via the script, atomic): run EXACTLY bash $WS/scripts/claim_note.sh release <full note path> after completing or checkpointing a project (step done, heavy-step checkpoint, or safety gate pause), before moving to the next project. Never leave a note with a live claim after you stop working it, and never hand-strip claimed_by with the edit tool — use the script so the release goes through the same lock.\n\nStep 9: When a project done_when (all steps [x] / final artifact) is met, flip status to done, then release the claim via bash $WS/scripts/claim_note.sh release <note>. Daily Note Review will delete/promote it.\n\nStep 10: REPORT only on real state change, via the message tool (do not rely on runner fallback delivery, which may be disabled). BEFORE composing any progress report, RE-READ the note's current_step and status from disk and report the CURRENT values — never a remembered/cached earlier state (a stale 'step 1/6' report after the note already reached a later step is a real bug this prevents). Rules:\n- Step advanced or completed -> send a message with a progress bar\n- First time hitting safety gate -> send a message once, tag owner (use platform-appropriate mention)\n- Already paused (resume_state contains 'awaiting approval') -> silent. Output exactly NO_REPLY and nothing else.\n- Skipped due to a live-session or other active claim -> silent (not a state change). Output exactly NO_REPLY and nothing else.\n- Malformed or stalled -> send a message only if last_flagged not set in resume_state OR last_flagged > 24h ago; write last_flagged: <ISO timestamp> to resume_state after sending. Otherwise silent: output exactly NO_REPLY.\n- No state change -> silent. Output exactly NO_REPLY and nothing else.\nProgress bar (only when actually sending a real state-change report): [████░░░░░░] 4/10 40% — project name, step n/m. Short, group-chat style.",
        "lightContext": True,
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
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_cron_job(job, idfile, label, idsdir):
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
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
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Note Cron Gate) \u2713 verified")
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
        "message": "Project Improver (Component 2). You improve COMPLETED-but-unverified project notes, then stamp them verified so Component 1 can retire them. Autonomous, behavior-preserving, all domains. Hold the safety gate HARD — you edit real artifacts unattended.\n\nStep 0 — ZERO-LLM PRE-GATE: Run: bash $WS/scripts/check_improvable.sh\nIf exit code != 0 → nothing to improve. STOP IMMEDIATELY. No further tool calls, no announce.\n\nStep 1 — SELECT: ls memory/_note_*.md in $WS/memory/. Read each. Pick notes with type: project AND status: done AND NOT verified: true. Handle ONE note this run (the oldest by date). If none, stop silently.\n\nStep 2 — CLAIM PRE-CHECK (cheap filter only; the AUTHORITATIVE gate is Step 3). ALL timestamps are UTC ISO8601; compute age in UTC. Read claimed_by / claimed_at. If claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes (2h) ago → SKIP (a human is working it live; never fight a live session). If claimed_by is any other value AND claimed_at < 30 min ago → SKIP (Advancer or another improver instance owns it). This prose check only avoids spawning the claim script on an obviously-busy note; it is NOT trusted to be race-safe on its own — Step 3 is.\n\nStep 3 — CLAIM (ATOMIC, via the shared lock — do NOT hand-write claimed_by). Run EXACTLY: bash $WS/scripts/claim_note.sh claim <full note path> component2-improver. Interpret the exit code: exit 0 -> you now hold the claim atomically (flock + verified write); proceed. exit 3 -> BUSY: a fresh claim (live-session or another job) holds this note; SKIP it entirely this run — do NOT edit it, do NOT fight, do NOT hand-write a claim; move to the next eligible note. Any other non-zero -> treat as unable-to-claim; SKIP this note this run. NEVER write claimed_by/claimed_at yourself with the edit tool — the script is the only writer, so your claim goes through the same flock the live session uses (this is the fix for the claim-clobber race). If you keep editing the same note across steps, re-assert with bash $WS/scripts/claim_note.sh refresh <note> component2-improver.\n\nStep 4 — UNDERSTAND: Read the project's done_when and resume_state, and inspect the actual artifact(s) it produced (scripts, deliverables, reports, config — whatever the project made). Establish what the project DOES and how you'll verify it still does that after any change (its behavior contract = its done_when).\n\nStep 5 — ASSESS (principle, NOT a checklist): Ask 'can this be made cheaper, faster, clearer, or more reliable — AND does that even make sense for THIS project?' There is NO prescribed action. Do not force caching/logging/locks/dedup or any technique. The right move is whatever fits this specific project — possibly a technique nobody would list, possibly NOTHING. 'Already efficient, nothing needed' is a valid, common, correct outcome.\n\nStep 6 — IMPROVE (behavior-preserving ONLY): If there is a worthwhile improvement, make it. You MUST NOT change WHAT the project does — only how efficiently/reliably/clearly it does it. Back up any file before editing (bash $SKILL_DIR/scripts/file-backup.sh <file>). Keep changes minimal and targeted.\n\nStep 7 — VALIDATE / DEBUG until reliable: After improving, verify the artifact STILL satisfies the project's done_when — for code: it runs / tests pass / compiles; for a deliverable: it is coherent, complete, and verifiably correct against done_when. If your change broke behavior, debug and fix, or REVERT to the backup. Do not proceed until the behavior contract holds again. If you cannot make it hold, revert fully and treat as 'no safe improvement'. OPTIONAL git evidence (only if the repo is git-tracked; skip silently if not): you MAY run 'git -C $WS log --oneline -5 -- <artifact>' or 'git -C $WS diff HEAD~1 -- <artifact>' to see what actually changed for this project as corroborating validation evidence. This is an OPTIONAL aid, never required — absence of git changes nothing about how you validate.\n\nStep 8 — SAFETY GATE (non-negotiable): You may do LOCAL, reversible work only. If improving/validating would require an EXTERNAL or DESTRUCTIVE action — git push, deleting data, sending external messages, spending money, touching credentials, irreversible writes outside the workspace — DO NOT do it. Do the local part, then STOP before the external action. Do NOT stamp verified. Clear your claim (Step 10), and in Step 11 report the project is 'improved locally, paused awaiting approval for <the external step>', tag the owner. Record in resume_state that it is awaiting approval so the next run does not re-loop on it endlessly.\n\nStep 9 — STAMP VERIFIED: Only if the project is validated behavior-preserving AND no external step is blocking, write verified: true into the note frontmatter. This is the signal Component 1 uses to delete it. (If you determined 'nothing needed', still stamp verified: true — you reviewed and confirmed it's already sound.)\n\nStep 10 — CLAIM RELEASE (via the script, atomic): run EXACTLY bash $WS/scripts/claim_note.sh release <full note path> (whether you stamped verified, paused at the safety gate, or aborted). Never leave a dead claim, and never hand-strip claimed_by with the edit tool — use the script so the release goes through the same lock.\n\nStep 11 — REPORT to the owner via the message tool, using the agent's default owner channel (platform-appropriate mention; do not hardcode a chat id). One short message: project name + what you did. Formats:\n- Improved: 'Improved <project>: <one-line what+why>. Validated behavior-preserving, verified.'\n- Nothing needed: 'Reviewed <project>: already efficient, nothing to change. Verified.'\n- Safety-gated: 'Improved <project> locally: <what>. Paused — needs your approval for <external step> before I finish.' (tag owner)\nKeep it terse. No report if Step 0 gated out or you skipped on a claim (those are silent).\n\nHARD RULES: behavior-preserving only, never change what a project does; local+reversible only, external/destructive → stop+report; one note per run; ALWAYS claim + release via claim_note.sh (never hand-write claimed_by); respect live-session and advancer claims; 'nothing needed' is a valid outcome — never invent busywork.",
        "lightContext": True,
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
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_cron_job(job, idfile, label, idsdir):
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
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
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Note Cron Gate) \u2713 verified")
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

# Deleter is a near-zero-cognition janitor (run script, grep output, send one
# line) -> pin it to the cheap/non-reasoning model when DINOMEM_CHEAP_MODEL is
# set. Unset -> agent default (default-safe). Advancer/Improver are intentionally
# LEFT on the capable default: they write+debug+validate real artifacts unattended
# and forcing them cheap would degrade that work.
_cheap = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
job = {
    "name": "Verified Note Deleter (Component 1)",
    "schedule": {"kind": "cron", "expr": "25,55 * * * *", "tz": "UTC", "staggerMs": 30000},
    "enabled": False,
    "payload": {
        "kind": "agentTurn",
        **({"model": _cheap} if _cheap else {}),
        "message": "Verified Note Deleter (Component 1). Deterministic janitor: delete project/task notes that carry verified: true, so the workspace stays clean after Component 2 has improved+validated them. This is a THIN wrapper around a script that does all the real work (zero-LLM gate, claim-respect, snapshot, distill-to-regular-memory, non-blocking lock).\n\nStep 1: Run exactly this command: python3 $WS/scripts/resolve_done_notes.py\n\nStep 2: Read its output. The script self-gates: if no status:done note exists it exits immediately (no work). It deletes ONLY notes with verified: true that are NOT freshly claimed by another job; before deleting each it snapshots it and DISTILLS its knowledge into normal schema memory entries (which then age/freeze on merit via the usual retention review). There is no _pin_ promotion — durable pins are authored directly as _pin_ files, never produced by note retirement.\n\nStep 3: ANNOUNCE only if it actually deleted something (grep the output for 'deleted verified note'). If so, send ONE terse line to the owner via the message tool using the agent's default owner channel (platform-appropriate; do not hardcode a chat id): e.g. 'Retired N verified note(s): <names>.' If it deleted nothing (gate exit, lock busy, or no verified notes), stay SILENT — no message.\n\nDo nothing else. Do not edit notes, do not re-check done_when, do not stamp verified — that is Component 2's job. You are the janitor.",
        "toolsAllow": ["read", "write", "exec", "process", "message"],
        "lightContext": True,
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_cron_job(job, "deleter", "verified_note_deleter", "$WS/.cron_ids")
PYEOF

# ── brainstorm_ideator — OpenClaw agentTurn cron ─────────────────────────────
# The Ideator acts ONLY on type:brainstorm / status:design notes — the thinking
# class the Advancer/Improver/Deleter gates (which hard-require type:project or
# verified:true) are blind to by design. It VERIFIES/tests/debugs the note's claims
# (read-only exec), pressure-tests the reasoning, and APPENDS to a ## CRON CRITIQUE
# section. It NEVER builds/advances/completes/reaps a note, NEVER mutates the body,
# NEVER changes type/status (promotion is owner-only — it only suggests). Premium:
# only neuron installs this. Disabled; driven by the Note Cron Gate like the trio.
  DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
    pay = job.get('payload', {}) or {}
    if pay.get('message'):
        a += ['--message', pay['message']]
    if pay.get('model'):
        a += ['--model', pay['model']]
    if pay.get('thinking'):
        a += ['--thinking', pay['thinking']]
    if pay.get('lightContext'):
        a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''

def upsert_cron_job(job, idfile, label, idsdir):
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--disable', '--message', msg],
                           capture_output=True, text=True, timeout=15)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                return
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = ''
            vid = _cron_verify(name)
            if not vid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing).")
                return
            jid = jid or vid
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled; driven by Note Cron Gate) \u2713 verified")
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Brainstorm Ideator",
    "schedule": {"kind": "cron", "expr": "35 * * * *", "tz": "UTC", "staggerMs": 60000},
    "enabled": False,
    "payload": {
        "kind": "agentTurn",
        "message": "Brainstorm Ideator. You pressure-test and VERIFY thinking notes (type: brainstorm) — you investigate, test, and debug their claims, then sharpen the thinking. You do NOT build, advance, complete, or reap them. Verify/test/debug is your job; building is forbidden.\n\nStep 0 — ZERO-LLM PRE-GATE: Run: bash $WS/scripts/check_ideator.sh\nIf exit code != 0 → no brainstorm note needs critique. STOP IMMEDIATELY. No further tool calls, no announce. Output exactly NO_REPLY.\n\nStep 1 — SELECT: ls memory/_note_*.md in $WS/memory/. Read each. Pick notes with type: brainstorm AND status: design. Handle ONE note this run (oldest by date whose ## CRON CRITIQUE section is stalest or absent). If none, stop silently (NO_REPLY).\n\nStep 2 — CLAIM PRE-CHECK (cheap filter only; Step 3 is authoritative). ALL timestamps UTC ISO8601; compute age in UTC. Read claimed_by / claimed_at. If claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes (2h) ago → SKIP (a human is thinking through it live; NEVER interrupt a live design session). If claimed_by is any other value AND claimed_at < 30 min ago → SKIP (another ideator instance owns it).\n\nStep 3 — CLAIM (ATOMIC — do NOT hand-write claimed_by). Run EXACTLY: bash $WS/scripts/claim_note.sh claim <full note path> ideator. exit 0 → you hold it, proceed. exit 3 → BUSY, SKIP this note entirely this run. Any other non-zero → SKIP. The script is the only writer of claimed_by/claimed_at. If you edit across steps, re-assert with: bash $WS/scripts/claim_note.sh refresh <note> ideator.\n\nStep 4 — INVESTIGATE (this is the whole job — you TEST, you do NOT assume). Read the note's thinking. For each checkable claim/assumption/decision, ACTUALLY CHECK IT — run the verification, report real findings, never speculate:\n  - CLAIMS marked verified: re-run the check. Read the cited file/line, exec the referenced test, grep the config, query the data. Report CONFIRMED (with evidence) / REFUTED (with the contradicting evidence) / INCONCLUSIVE (what's missing to decide).\n  - UNVERIFIED 'we should check X' items: DO the check now. Read it, run it, query it. Report what you found.\n  - HOLES / CONTRADICTIONS: test whether they're real by inspecting the ACTUAL artifacts (files, logs, outputs), not by reasoning alone. Search other memory notes for conflicts.\n  - DEBUG: if the note describes something that should work, try to reproduce/verify it. If it breaks, isolate WHERE (read logs, trace, re-run read-only) and report the ROOT CAUSE. You DIAGNOSE the bug — you do NOT fix it.\n  - NEXT QUESTION: the sharpest unanswered question that would most move the thinking forward — a QUESTION, not a task.\n  - ALTERNATIVE FRAME: a materially different angle the note didn't consider (only if genuinely different — never invent contrarianism to look useful).\nBe a skeptical peer who does the legwork, not a cheerleader and not a doubter-for-show. 'Verified the claims, thinking holds, no new holes' is a valid, correct outcome — say it briefly rather than manufacture doubt.\n\nStep 5 — EXEC SAFETY BOUNDARY (hard, non-negotiable — you run unattended):\n  - ALLOWED: read files, grep, run tests/linters/diagnostics, query data/APIs read-only, re-run READ-ONLY referenced commands, inspect logs/config/state, bash -n syntax checks.\n  - FORBIDDEN: writing/editing ANY tracked file (the ONLY write you may make is appending to THIS note's ## CRON CRITIQUE section), git commit/push, any send/message to third parties, spending, credentials, installing, config mutation (openclaw config set/patch, cron add/edit), deleting, deploying, or ANY command that changes system/repo/external state.\n  - VERIFY != BUILD: you may find a bug and PROPOSE the fix in the critique, but you MUST NOT apply it. Debugging finds the cause; fixing is building; building is forbidden. When unsure whether a command mutates state, DO NOT run it — write 'BLOCKED: needs owner to run <exact command>' in the critique and move on.\n\nStep 6 — APPEND ONLY: Add a section at the END of the note titled '## CRON CRITIQUE (ideator, <UTC date>)'. Put your findings there. NEVER mutate the settled-decisions body above it. If a prior CRON CRITIQUE exists, add a NEW dated subsection — do not overwrite past critique (the thinking's evolution is the value). Back up the note first: bash $SKILL_DIR/scripts/file-backup.sh <note path>.\n\nStep 7 — PROMOTION SUGGESTION (suggest, NEVER act): If the thinking has crystallized into something buildable, add ONE line: 'Suggest promoting to type: project: <what>.' You do NOT change type/status yourself — promotion is always the owner's call.\n\nStep 8 — CLAIM RELEASE (via the script, atomic): Run EXACTLY: bash $WS/scripts/claim_note.sh release <full note path>. Never leave a dead claim; never hand-strip claimed_by.\n\nStep 9 — REPORT to the owner via the message tool, using the agent's default owner channel (platform-appropriate mention; do not hardcode a chat id). One terse message: '<note name>: <n> critique/verification points — <the sharpest one>.' If you verified and found nothing new ('thinking still sound'), stay SILENT (NO_REPLY) — do not ping for a no-op. No report if Step 0 gated out or you skipped on a claim.\n\nHARD RULES: verify/test/debug only — NEVER build/advance/complete/resolve/fix; append-only to ## CRON CRITIQUE, NEVER mutate the body; NEVER change status or type (promotion is owner-only, you only suggest); read-only/diagnostic exec ONLY (see Step 5 boundary); one note per run; ALWAYS claim + release via claim_note.sh (never hand-write claimed_by); respect live-session claims absolutely; 'thinking is sound' is a valid outcome — never manufacture doubt to look useful.",
        "lightContext": True,
        "timeoutSeconds": 900
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_cron_job(job, "ideator", "brainstorm_ideator", "$WS/.cron_ids")
PYEOF

# ── Note Cron Gate — ZERO-LLM dispatcher (command cron) ───────────────────────
# CANONICAL SINGLE GATE. Named "Note Cron Gate" in BOTH base and neuron. base's
# installer may have already created it (driving the note_review + pending_reminder
# lanes); if so we MERGE our project-trio + Daily lanes into that SAME gate's env
# rather than creating a second one. Never two gates -> never double-dispatch.
#
# The trio jobs (Advancer/Improver/Deleter) + Daily Note Review are registered
# DISABLED so they never self-fire. This one command-kind cron runs every 15 min
# in the Gateway process at $0 LLM, runs each cheap check_*.sh, and force-runs
# (openclaw cron run) only the jobs whose check reports real work.
#
# Command crons need the Gateway to allow command-kind jobs (operator.admin). If a
# buyer's gateway refuses them, we FALL BACK by re-enabling the agentTurn jobs on
# their own schedules (pre-redesign behavior) so nothing breaks — with a LOUD
# warning, because that fallback costs LLM on every idle tick.
#
# LEGACY CLEANUP: older neuron installs created "Project Cron Gate". If that job
# still exists we REMOVE it here so it can't keep firing alongside the new gate.
LEGACY_GATE_ID=$(python3 -c "
import subprocess, json
try:
    r = subprocess.run(['openclaw','cron','list','--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    jl = jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])
    print(next((j.get('id','') for j in jl if (j.get('name','').strip().lower()=='project cron gate' and (not _DINO_AID or not (j.get('agentId') or '') or (j.get('agentId') or '').strip().lower()==_DINO_AID))), ''))
except: print('')
" 2>/dev/null)
if [ -n "$LEGACY_GATE_ID" ]; then
  openclaw cron remove "$LEGACY_GATE_ID" >/dev/null 2>&1 && ok "removed legacy 'Project Cron Gate' (replaced by canonical 'Note Cron Gate')"
fi
# SAME-NAME DEDUP SWEEP (idempotency guarantee across ANY prior state): the
# upsert helpers already update-by-name so a clean re-run never duplicates, BUT a
# PRIOR half-finished/crashed run (or a manual add) could have left two jobs with
# the SAME canonical name. If so, the by-name lookup picks one arbitrarily and the
# other keeps firing as a zombie. Here we sweep every canonical lane name: if >1
# job shares it, keep the NEWEST (highest createdAtMs) and remove the rest. This
# makes 'update dinomem' converge to exactly one of each lane no matter what state
# it started from. Pure cleanup: never creates anything, only removes true dupes.
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<'PYEOF'
import subprocess, json
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--
CANON = [
    "Project Advancer",
    "Project Improver (Component 2)",
    "Verified Note Deleter (Component 1)",
    "Brainstorm Ideator",
    "Daily Note Review",
    "Note Cron Gate",
]
try:
    r = subprocess.run(['openclaw','cron','list','--json'], capture_output=True, text=True, timeout=10)
    data = json.loads(r.stdout) if r.returncode == 0 else []
    jl = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
except Exception:
    jl = []
for cname in CANON:
    matches = [j for j in (jl or []) if _dino_name_agent_match(j, cname)]
    if len(matches) <= 1:
        continue
    # keep the newest by createdAtMs (fallback updatedAtMs); remove the rest
    matches.sort(key=lambda j: (j.get('createdAtMs', 0), j.get('updatedAtMs', 0)), reverse=True)
    for dup in matches[1:]:
        did = dup.get('id','')
        if did:
            subprocess.run(['openclaw','cron','remove',did], capture_output=True, text=True, timeout=15)
            print("  \033[32m[ok]\033[0m   deduped '%s' — removed duplicate %s (kept newest)" % (cname, did[:8]))
PYEOF
if true; then
  python3 - <<PYEOF
import subprocess, json, os

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
dnr = readid("daily_note_review")  # optional lane; '' if not yet registered
idea = readid("brainstorm_ideator")  # neuron ideator lane; '' if not yet registered

GATE_NAME = "Note Cron Gate"
def _find_cron(name):
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        jl = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        return next((j.get('id','') for j in (jl or []) if j.get('name','').strip().lower()==name.strip().lower()), '')
    except Exception:
        return ''
def _gate_env(gate_id):
    try:
        g = subprocess.run(['openclaw', 'cron', 'get', gate_id, '--json'], capture_output=True, text=True, timeout=15)
        if g.returncode == 0:
            return (json.loads(g.stdout).get('payload', {}) or {}).get('env', {}) or {}
    except Exception:
        pass
    return {}

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

# Our lanes to add/merge. Trio + Daily; base already owns pending_reminder (and,
# if base ran first, note_review too) inside the SAME gate's env — we never strip
# those, only add ours.
our_lanes = {}
if adv:  our_lanes["GATE_ADVANCER_ID"] = adv
if imp:  our_lanes["GATE_IMPROVER_ID"] = imp
if dele: our_lanes["GATE_DELETER_ID"] = dele
if dnr:  our_lanes["GATE_DAILY_NOTE_REVIEW_ID"] = dnr
if idea: our_lanes["GATE_IDEATOR_ID"] = idea

existing_gate = _find_cron(GATE_NAME)
if existing_gate:
    # Base (or a prior neuron run) already created the canonical gate. MERGE our
    # lanes into its env — preserving base's note_review + pending_reminder ids.
    env = _gate_env(existing_gate); env["OPENCLAW_WORKSPACE"] = env.get("OPENCLAW_WORKSPACE", WS); env.update(our_lanes)
    args = ['openclaw', 'cron', 'edit', existing_gate]
    for k, v in env.items():
        args += ['--command-env', f'{k}={v}']
    subprocess.run(args, capture_output=True, text=True, timeout=15)
    subprocess.run(['openclaw', 'cron', 'enable', existing_gate], capture_output=True, text=True, timeout=15)
    print("  \033[32m[ok]\033[0m   Note Cron Gate found — merged Advancer/Improver/Deleter + Daily lanes into its env (single gate, zero double-dispatch)")
elif not (adv and imp and dele):
    fallback("one or more LLM job ids were not captured (cron add may have failed above).")
else:
    gate = {
        "name": GATE_NAME,
        "description": "Zero-LLM dispatcher: runs cheap note/project checks every 15min and force-runs a worker only when its check reports work.",
        "schedule": {"kind": "cron", "expr": "*/15 * * * *", "tz": "UTC"},
        "payload": {
            "kind": "command",
            "argv": ["sh", "-lc", "bash " + WS + "/scripts/cron_gate.sh"],
            "cwd": WS,
            "env": {"OPENCLAW_WORKSPACE": WS, **our_lanes},
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
            print("  \033[32m[ok]\033[0m   Note Cron Gate registered (*/15 command cron, zero-LLM) — drives Advancer/Improver/Deleter + Daily Note Review")
        else:
            # Gateway downgraded/refused a real command job — remove the bad gate and fall back.
            if gid:
                subprocess.run(['openclaw', 'cron', 'remove', gid], capture_output=True, text=True, timeout=15)
            fallback("gateway did not store a real command-kind job (payload.kind != command).")
PYEOF
fi

# ── Log rotation for the cron logs dinomem (base+neuron) writes ───────────────
# Every dinomem cron appends to logs/*.log with NO rotation. On a busy box the
# */15 auto_session_reset + extraction logs grow unbounded (observed: 175MB+
# auto_reset.log, 161MB session_reset.log) until they threaten disk. Drop a
# logrotate config generated from $WS. Prefer the system dir (/etc/logrotate.d)
# when writable; else write a workspace-local config the operator can wire in.
# copytruncate keeps append-mode crons writing without a restart.
if [ "${DRY_RUN:-0}" = 1 ]; then
  plan "install logrotate config for $WS/logs/*.log (size 10M, keep 3, compress, copytruncate)" 2>/dev/null || true
else
  LOGROTATE_BODY="$WS/logs/*.log {
    size 10M
    rotate 3
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}"
  if [ -d /etc/logrotate.d ] && [ -w /etc/logrotate.d ]; then
    LR_DST="/etc/logrotate.d/dinomem-$AGENT_ID"
    printf '%s\n' "$LOGROTATE_BODY" > "$LR_DST" 2>/dev/null \
      && ok "logrotate installed: $LR_DST (10M x3, copytruncate)" \
      || warn "could not write $LR_DST — logs/*.log will not auto-rotate"
  else
    LR_DST="$WS/logs/logrotate.conf"
    mkdir -p "$WS/logs" 2>/dev/null || true
    printf '%s\n' "$LOGROTATE_BODY" > "$LR_DST" 2>/dev/null || true
    warn "no writable /etc/logrotate.d — wrote $LR_DST instead."
    warn "  Wire it in yourself, e.g. add to root crontab:"
    warn "  0 3 * * * /usr/sbin/logrotate --state $WS/logs/.logrotate.state $LR_DST"
  fi
fi

# ── daily_note_review — OpenClaw agentTurn cron (general note janitor) ────────
# Retires ALL note types once resolvable: task_bound notes whose done_when is met,
# type:project notes finished out-of-band or parked at a safety-gated final step,
# and abandoned notes past stale_after. Complements the Advancer/Improver/Deleter
# (which only handle project step-advance + verified deletion). Claim-aware:
# never touches a freshly-claimed note (live-session <120m / other <30m).
# Premium: neuron only. Without this, done task_bound + non-verified done notes
# would never auto-retire on a buyer install.
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: 'openclaw cron add' rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead so
    # the self-check's repair path can surface exactly which lane is malformed.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_gated(job, idfile, label, idsdir, gate_env_key):
    """Register a GATE-DRIVEN agentTurn cron: created DISABLED (never self-fires),
    its id written to .cron_ids/<idfile>, then the already-created Note Cron Gate
    cron gets this id injected into its command-env under <gate_env_key> so the gate
    dispatches it. This makes the lane work fully automatically at install — no
    manual env-var editing, no script edits. Upgrade-safe: if the job exists, refresh
    its prompt AND force it disabled (in case an older install left it self-scheduled).
    If the gate itself is missing (command-crons refused -> fallback path), we
    RE-ENABLE this job on its own schedule so behavior is preserved."""
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    jid = ''
    try:
        if existing_id:
            jid = existing_id
            msg = job.get('payload', {}).get('message', '')
            args = ['openclaw', 'cron', 'edit', existing_id, '--message', msg]
            sched = job.get('schedule', {})
            if sched.get('kind') == 'cron' and sched.get('expr'):
                args += ['--cron', sched['expr']]
            subprocess.run(args, capture_output=True, text=True, timeout=15)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed)")
        else:
            j2 = dict(job); j2['enabled'] = False  # create DISABLED
            ar = subprocess.run(_cron_add_argv(j2), capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = _cron_verify(name)
            if not jid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing). Add it manually. Install continues.")
                return
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered \u2713 verified")
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")
        return
    if not jid:
        return
    # Persist the id for future upgrade runs.
    try:
        os.makedirs(idsdir, exist_ok=True)
        open(idsdir + '/' + idfile, 'w').write(jid)
    except Exception:
        pass
    # Locate the Note Cron Gate. If present (zero-LLM path succeeded), wire this
    # lane's id into its env AND force this worker disabled. If ABSENT (fallback),
    # re-enable this worker on its own schedule so it still runs (behavior-preserving).
    gate_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if (j.get('name','').strip().lower() == 'note cron gate' and (not _DINO_AID or not (j.get('agentId') or '') or (j.get('agentId') or '').strip().lower()==_DINO_AID)):
                    gate_id = j.get('id',''); break
    except Exception:
        gate_id = ''
    if gate_id:
        # Force this worker DISABLED (gate drives it), then inject its id into the
        # gate's existing env. --command-env is repeatable and REPLACES the env set,
        # so we must re-send every existing key plus the new one.
        subprocess.run(['openclaw', 'cron', 'edit', jid, '--disable'], capture_output=True, text=True, timeout=15)
        existing_env = {}
        try:
            g = subprocess.run(['openclaw', 'cron', 'get', gate_id, '--json'], capture_output=True, text=True, timeout=15)
            if g.returncode == 0:
                existing_env = (json.loads(g.stdout).get('payload', {}) or {}).get('env', {}) or {}
        except Exception:
            existing_env = {}
        existing_env[gate_env_key] = jid
        envargs = ['openclaw', 'cron', 'edit', gate_id]
        for k, v in existing_env.items():
            envargs += ['--command-env', f'{k}={v}']
        subprocess.run(envargs, capture_output=True, text=True, timeout=15)
        print(f"  \033[32m[ok]\033[0m   {label} wired into Note Cron Gate ({gate_env_key}) — gate-driven, zero idle LLM")
    else:
        # No gate (command-crons refused). Re-enable on own schedule (fallback).
        subprocess.run(['openclaw', 'cron', 'enable', jid], capture_output=True, text=True, timeout=15)
        print(f"  \033[33m[warn]\033[0m {label}: no Note Cron Gate found — running on its own 06:00 schedule (costs 1 LLM/day). Enable command-crons + re-run for zero-cost gating.")

# Daily Note Review is verification + rubric-driven GC (run locally-checkable
# done_when conditions, flip/delete on the result) — not deep reasoning. Pin it
# to the cheap/non-reasoning model when DINOMEM_CHEAP_MODEL is set; unset -> agent
# default (default-safe).
_cheap = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
job = {
    "name": "Daily Note Review",
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        **({"model": _cheap} if _cheap else {}),
        "message": "SCOPE LOCK (READ FIRST, NON-NEGOTIABLE): You operate ONLY on files whose basename matches the glob _note_*.md in $WS/memory/. Get the exact set by running: ls $WS/memory/_note_*.md. If that returns nothing / no matches, STOP IMMEDIATELY, touch nothing, output exactly NO_REPLY. You must NEVER read, evaluate, flip, delete, or GC any file that is not a _note_*.md file. In particular you must NEVER touch: _pin_*.md, _permanent*.md, MEMORY.md, or any date-prefixed distilled memory file (e.g. 2026-06-20_insight_*.md, *_entity_*.md, *_preference_*.md, *_relation_*.md, *_insight_*.md). Those are PERMANENT KNOWLEDGE, not task notes — deleting one is data loss. Do NOT glob memory/*.md, do NOT 'scan all memory files', do NOT infer that a distilled memory file is a resolvable note. If the count of _note_*.md files you are about to process is more than a handful (say >50) or includes anything not literally named _note_*.md, that is a BUG in your file selection — STOP and report it instead of acting. Only after this scope lock is satisfied, proceed.\n\nScan the _note_*.md files in $WS/memory/. Resolve each note (today = current UTC date). FIRST, for every note read claimed_by and claimed_at (UTC ISO8601): if claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes ago, OR claimed_by is any other value AND claimed_at < 30 minutes ago, SKIP that note entirely this run (another job or a live human owns it). Stale or future-dated claims are ignored (proceed). Then: 1) SKIP any note with an expires: field until today > expires; delete a time_bound note only once today > its expires date (generic time-based GC; no external calendar service). 2) task_bound notes (have done_when:): verify the done_when condition against workspace state (file exists, feature shipped, grep, exit 0). If verified, flip status to done and delete the note (its knowledge is distilled into regular memory by the Verified Note Deleter pipeline; do NOT copy it to _pin_ — pins are authored directly, not produced by retirement). Else leave pending. 3) type:project notes: these are normally advanced/closed by the Project Advancer and improved+retired by the Project Improver + Verified Note Deleter, BUT a project can be finished out-of-band by a human-driven session and left status:in_progress, or parked at a safety-gated final step (git push / external action) the Advancer is forbidden to run — so it would otherwise orphan here. For any type:project note, verify its done_when the SAME way as task_bound (run the locally-checkable condition; e.g. for a git-push done_when run git -C <repo> rev-parse HEAD vs @{u}). BEFORE deferring on unchecked steps, ALWAYS run the top-level done_when artifact check FIRST and do NOT trust resume_state over the artifact: a stale resume_state (e.g. 'awaiting owner sign-off', 'do NOT write code yet', 'step 1/9') is NOT evidence the work is unshipped — it may have shipped out-of-band in a live session and the note never got written back. If done_when verifies and/or all steps are [x], flip status to done and delete it (no _pin_ promotion) EVEN IF steps are still unchecked and EVEN IF resume_state says it is parked/awaiting-approval — the artifact wins. If still in_progress with unchecked non-gated steps remaining AND the top-level done_when does NOT verify against a real artifact, leave it for the Advancer. Never delete a project whose done_when does not verify against a real artifact. 4) type:brainstorm notes (settled-thinking, status:design; NOT tasks, so they carry NO done_when and are normally left to the Brainstorm Ideator): a brainstorm can nonetheless have a SHIPPABLE outcome that already landed out-of-band in a live session, leaving it stranded at status:design forever. IF such a note carries a shipped_when: field, verify it the SAME locally-checkable way as done_when (file exists / grep / exit 0 / test "$(git -C <repo> rev-parse HEAD)" = "$(git -C <repo> rev-parse @{u})"). If shipped_when verifies, flip status:design -> status:resolved and LEAVE THE NOTE IN PLACE (do NOT delete it — a brainstorm's thinking is the value; resolved brainstorms are retained/promoted, never reaped by this cron). If shipped_when does not verify, leave it design for the Ideator. If a brainstorm has NO shipped_when field, DO NOT infer completion — leave it entirely to the Ideator (pure open-ended thinking has no machine-checkable resolution and must stay human/ideator-resolved). Never delete a type:brainstorm note here regardless. 5) stale_after GC: if a note is still pending/in_progress AND done_when was never met AND today > stale_after (default date+30d, or date+7d for reminder/quick-todo notes), delete it as abandoned. 6) Legacy notes with no schema fields: infer the task from content, delete if clearly resolved, else leave. Report what resolved, what was GC'd, what remains, and what was skipped (including claim-skips).",
        "lightContext": True,
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_gated(job, "daily_note_review", "daily_note_review", "$WS/.cron_ids", "GATE_DAILY_NOTE_REVIEW_ID")
PYEOF

# ── Wire AGENTS.md ───────────────────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
if [ -f "$AGENTS" ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$AGENTS" >/dev/null 2>&1 && ok "AGENTS.md backed up" || true  # backup is a safety nicety; original file is untouched, so a failed .bak is not user-actionable — stay silent
fi
if [ -f "$WS/MEMORY.md" ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$WS/MEMORY.md" >/dev/null 2>&1 && ok "MEMORY.md backed up" || true
fi
BEGIN="<!-- BEGIN:dinomem-neuron (managed) -->"
END="<!-- END:dinomem-neuron -->"
# Body is a literal quoted heredoc: embedded double-quotes ("<title>", "<query>")
# and <angle-bracket> placeholders must NOT be shell-evaluated. Markers wrapped after.
DINOMEM_BODY=$(cat <<'DINOMEM_NEURON_AGENTS_BODY'
## neuron_skills
  # Neuron ships AgentSkills that hold the full procedures. Read the SKILL.md when its trigger fires; the stubs below are just the when-to-use.
  retrieval-routing: which retrieval tool per query (docs_search | session_search | memory_search | data_query | graph_search | grep) + how to ingest long docs/images and register/query structured data. READ when: doc OR image shared/uploaded (pdf/docx/etc OR jpg/png/screenshot/photo/scan) | save/remember a doc or image | content >2000 chars | row|count|range|filter|sort|aggregate|field-lookup over csv/json/parquet | unsure which retrieval tool to use.
  memory-pinning: how to write durable memory — permanent _pin_ facts and transient _note_ todos/reminders — with the semantic dedup gate + auto-note triggers. READ when: user says remember/pin/note this | your reply commits to deferred/external work | a todo/reminder/time-bound item arises. Always run the dedup gate (procedures/memory_dedup_check.py) BEFORE authoring a NEW _pin_/_note_; match:true -> merge into the existing file, not a second one.
  project-notes: plan+execute multi-step builds as durable on-disk project notes (format, estimate gate, heaviness/turn-boundary, self-check, resume_state). READ when: a request trips the project gate (>3 files OR >5 sequential tool-steps OR research-then-build OR user says project/build) | resuming an in_progress project note.
  note-claim: claim a project/task _note_ before editing it from a live session so the Advancer/Improver/Deleter crons back off. READ + claim BEFORE the first multi-turn or build-related edit to any memory/_note_*.md a cron could also touch.

## cold_start_recall
  # The dinomem-open-notes hook auto-injects a manifest of OPEN _note_*.md at bootstrap (and a session_search recall nudge). Behavior: read the relevant open note and resume from its resume_state BEFORE answering; do NOT restart finished work. When the opener names something no injected note covers, run session_search "<natural-language topic>" (recent convo <7d) before answering.

## retrieval_tools_quickref
  # EXACT-vs-FUZZY fork. Check the EXACT/ingest/meta forks first; everything else
  # (semantic recall) goes through the ONE fuzzy door. Full rules in retrieval-routing skill.
  # --- EXACT / ingest / meta (explicit, NEVER fused) ---
  row | count | range | filter | sort | aggregate | field lookup (csv|json|parquet) → data_query (exact SQL, NOT memory_search)
  exact pattern | regex | grep | exact-string over raw text/logs → exec (ripgrep/grep)
  long doc / file shared / "remember this doc" → docs_ingest ; csv|json|parquet dataset → data_register
  what CHANGED in memory (adds/merges/deletes) → read memory/.diffs/ ; which retrieval tool ran / routing quality → kb/retrieval_log/
  # --- FUZZY recall (the one door) ---
  fact | decision | preference | todo | meaning | passage | recent convo (<7d) | relational (why/led-to/affects/trace) → hybrid_recall
    # hybrid_recall fuses docs_search + session_search + graph_search legs; run memory_search natively and pass its hits via --external-hits to fold them in.
    # M3 natural-language query rule applies (avoid code identifiers/exact strings).
DINOMEM_NEURON_AGENTS_BODY
)
BLOCK="$BEGIN
$DINOMEM_BODY
$END"

touch "$AGENTS"
if grep -qF "$BEGIN" "$AGENTS" 2>/dev/null; then
  # Neuron block already present. Refresh it in place ONLY under --force, so
  # upgrades from an older dinomem-neuron (longer inline block, no skill stubs)
  # actually pick up the current short block instead of keeping stale text.
  # Strip the old BEGIN..END span first, then append the fresh block
  # (idempotent upsert — never stacks a second block). Targets neuron's OWN
  # markers, so base's separate BEGIN:dinomem block is untouched.
  if [ "$FORCE" = 1 ]; then
    _tmp_agents="$(mktemp)"
    # Delete the inclusive BEGIN..END region (fixed-string match), keep the rest.
    awk -v b="$BEGIN" -v e="$END" '
      index($0,b){skip=1}
      !skip{print}
      index($0,e){skip=0}
    ' "$AGENTS" > "$_tmp_agents"
    # Trim trailing blank lines the removal may leave, then append fresh block.
    awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp_agents" > "$AGENTS"
    rm -f "$_tmp_agents"
    printf '\n%s\n' "$BLOCK" >> "$AGENTS"
    ok "AGENTS.md block refreshed (old neuron block stripped, current block written)"
  else
    skip "AGENTS.md already wired (re-run with --force to refresh the managed block)"
  fi
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
  bash "$SKILL_DIR/scripts/file-backup.sh" "$TOOLS" >/dev/null 2>&1 && ok "TOOLS.md backed up" || true
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
    scope: |
      Indexes only archived session .jsonl within the last 21 days (MAX_AGE_DAYS),
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
# Install into the PERSISTENT global extensions dir, NOT the workspace. Plugin
# registration in openclaw.json is global; a workspace-relative load.paths entry
# dangles (and crash-loops the gateway) if the workspace is ever removed/moved.
PLUGIN_DST="$OPENCLAW_DIR/extensions/note-claim-refresh"
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

# 1b) bundledDiscovery — REQUIRED companion to plugins.allow on OpenClaw
#     2026.6.x+. Setting plugins.allow WITHOUT bundledDiscovery makes the new
#     schema reject the whole config ("plugins: Invalid input") and the gateway
#     refuses to start — total bot silence. "compat" preserves legacy bundled
#     provider/channel discovery (safest: won't disable existing chat channels).
if plugins.get("bundledDiscovery") not in ("compat", "allowlist"):
    plugins["bundledDiscovery"] = "compat"
    changed.append('plugins.bundledDiscovery -> "compat"')

# 2) load.paths -> abs plugin dir (dedup) + PRUNE stale workspace-relative paths.
#    Older installer builds registered $WS/plugins/note-claim-refresh, which
#    dangles if the workspace is deleted/moved (schema-invalid -> gateway
#    crash-loop). Remove any prior PID path that isn't the persistent one.
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
    load["paths"] = paths
# prune stale note-claim-refresh entries (any path ending in the PID dir that
# isn't the current persistent plugin_dst)
stale_paths = [p for p in paths
               if isinstance(p, str)
               and p.rstrip("/").endswith("/" + PID)
               and p != plugin_dst]
for sp in stale_paths:
    paths.remove(sp)
    changed.append(f"plugins.load.paths -= {sp} (stale)")
if plugin_dst not in paths:
    paths.append(plugin_dst)
    changed.append(f"plugins.load.paths += {plugin_dst}")

# 3) per-plugin config + enable -> plugins.entries.<PID>.config
#    IMPORTANT: OpenClaw's `plugins` schema is .strict() and does NOT accept a
#    top-level `plugins.config` key (only enabled/allow/deny/load/slots/entries/
#    bundledDiscovery). Per-plugin config is valid ONLY under
#    plugins.entries.<name>.config. Writing plugins.config at the top level makes
#    the whole config schema-invalid and the gateway refuses to start. Also, a
#    stale top-level `plugins.config` from an older installer build must be
#    migrated/removed here.
entries = plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    entries = {}
    plugins["entries"] = entries
entry = entries.get(PID)
if not isinstance(entry, dict):
    entry = {}
    entries[PID] = entry
if entry.get("enabled") is not True:
    entry["enabled"] = True
    changed.append(f"plugins.entries.{PID}.enabled -> true")
econf = entry.get("config")
if not isinstance(econf, dict):
    econf = {}
    entry["config"] = econf
if econf.get("workspaceDir") != ws:
    econf["workspaceDir"] = ws
    changed.append(f"plugins.entries.{PID}.config.workspaceDir -> {ws}")

# 3b) MIGRATE/REMOVE any invalid top-level plugins.config (from older installer
#     builds). Move a matching PID entry into plugins.entries, then delete the
#     top-level key entirely so the config validates.
stale = plugins.get("config")
if isinstance(stale, dict):
    stale_pid = stale.get(PID)
    if isinstance(stale_pid, dict):
        for k, v in stale_pid.items():
            if econf.get(k) != v:
                econf.setdefault(k, v)
    del plugins["config"]
    changed.append("removed invalid top-level plugins.config (migrated into entries)")

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

# ── Install note-claim-oncreate hook plugin (before_tool_call claim-on-write) ──
# Companion to note-claim-refresh. That plugin REFRESHES an existing live-session
# claim on any tool call; this one STAMPS the claim in the same turn a write/edit/
# apply_patch first targets a memory/_note_*.md the session doesn't yet hold —
# closing the last discipline-dependent sliver (a note first-edited mid-session
# before the model runs claim_note.sh). before_tool_call, observe-only: it claims
# as a side effect and NEVER blocks or rewrites the tool. Same persistent-extensions
# install + openclaw.json wiring pattern as note-claim-refresh above.
hr "Note-claim-oncreate hook plugin"
PLUGIN_SRC="$SKILL_DIR/plugins/note-claim-oncreate"
PLUGIN_DST="$OPENCLAW_DIR/extensions/note-claim-oncreate"
if [ ! -d "$PLUGIN_SRC" ]; then
  warn "plugin source $PLUGIN_SRC missing — skipping (claim-on-create stays manual)"
else
  mkdir -p "$PLUGIN_DST"
  cp "$PLUGIN_SRC/openclaw.plugin.json" "$PLUGIN_SRC/package.json" "$PLUGIN_SRC/index.ts" "$PLUGIN_DST/"
  ok "plugin files -> $PLUGIN_DST"
  if [ ! -f "$OPENCLAW_JSON" ]; then
    warn "openclaw.json not found — plugin copied but NOT wired. Add id 'note-claim-oncreate' to plugins.allow + its path to plugins.load.paths, then restart."
  else
    DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_PLUGIN_DST="$PLUGIN_DST" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, os

path = os.environ["DINOMEM_OPENCLAW_JSON"]
plugin_dst = os.environ["DINOMEM_PLUGIN_DST"]
ws = os.environ["DINOMEM_WS"]
PID = "note-claim-oncreate"
with open(path) as f:
    cfg = json.load(f)

changed = []
plugins = cfg.setdefault("plugins", {})

# 1) allowlist (create if absent so the plugin is explicitly permitted)
allow = plugins.get("allow")
if not isinstance(allow, list):
    allow = []
    plugins["allow"] = allow
if PID not in allow:
    allow.append(PID)
    changed.append(f"plugins.allow += {PID}")

# 1b) bundledDiscovery — REQUIRED companion to plugins.allow on OpenClaw 2026.6.x+.
#     Without it the new schema rejects the whole config and the gateway refuses
#     to start. "compat" preserves legacy bundled provider/channel discovery.
if plugins.get("bundledDiscovery") not in ("compat", "allowlist"):
    plugins["bundledDiscovery"] = "compat"
    changed.append('plugins.bundledDiscovery -> "compat"')

# 2) load.paths -> abs plugin dir (dedup) + PRUNE stale workspace-relative paths.
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
    load["paths"] = paths
stale_paths = [p for p in paths
               if isinstance(p, str)
               and p.rstrip("/").endswith("/" + PID)
               and p != plugin_dst]
for sp in stale_paths:
    paths.remove(sp)
    changed.append(f"plugins.load.paths -= {sp} (stale)")
if plugin_dst not in paths:
    paths.append(plugin_dst)
    changed.append(f"plugins.load.paths += {plugin_dst}")

# 3) per-plugin config + enable -> plugins.entries.<PID>.config
entries = plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    entries = {}
    plugins["entries"] = entries
entry = entries.get(PID)
if not isinstance(entry, dict):
    entry = {}
    entries[PID] = entry
if entry.get("enabled") is not True:
    entry["enabled"] = True
    changed.append(f"plugins.entries.{PID}.enabled -> true")
econf = entry.get("config")
if not isinstance(econf, dict):
    econf = {}
    entry["config"] = econf
if econf.get("workspaceDir") != ws:
    econf["workspaceDir"] = ws
    changed.append(f"plugins.entries.{PID}.config.workspaceDir -> {ws}")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m wire openclaw.json: {c}")
elif not changed:
    print("  \033[33m[skip]\033[0m note-claim-oncreate already wired in openclaw.json")
else:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print(f"  \033[32m[ok]\033[0m   wired: {c}")
    print("  \033[33m[warn]\033[0m Restart OpenClaw to load the plugin: openclaw gateway restart")
PYEOF
  fi
fi

# ── Override dinomem-recall-gate config (neuron-tier recallTools) ─────────────
# OWNERSHIP: the recall-gate plugin CODE (index.ts/package.json) + its openclaw.json
# wiring are OWNED BY BASE — base's installer (run FIRST above) copies them to the
# global extensions dir and wires them. neuron is a strict extension: it OVERWRITES
# ONLY openclaw.plugin.json with its neuron-tier recallTools (adds session_search/
# graph_search/docs_search/data_query on top of base's memory_search/memory_get),
# exactly the base-owns / neuron-extends pattern used for extract_memory.py. We do
# NOT re-ship index.ts/package.json here — that was the duplication bug. If base did
# not install the plugin (e.g. --no-auto-base against a base too old to ship it),
# we fall back to installing the full plugin from neuron's own copy is NOT possible
# (neuron no longer carries the code), so we WARN instead of silently half-installing.
hr "Recall-gate config override (neuron-tier)"
PLUGIN_SRC="$SKILL_DIR/plugins/dinomem-recall-gate"
PLUGIN_DST="$OPENCLAW_DIR/extensions/dinomem-recall-gate"
if [ ! -f "$PLUGIN_SRC/openclaw.plugin.json" ]; then
  warn "neuron recall-gate config source missing — leaving base's config in place"
elif [ ! -f "$PLUGIN_DST/index.ts" ]; then
  # SELF-HEAL (noob-smooth): the plugin CODE is owned by BASE. If it's missing,
  # don't dump a 'reinstall base yourself' wall on the user — just re-run base's
  # installer (idempotent) to drop the plugin, then recheck. Only attempt when
  # auto-base is allowed and we can locate the base installer.
  _healed=0
  # Base is cloned to $OPENCLAW_DIR/dinomem by auto_install_base(); reuse that path.
  _base_install="$OPENCLAW_DIR/dinomem/scripts/install.sh"
  if [ "${NO_AUTO_BASE:-0}" != 1 ] && [ -f "$_base_install" ]; then
    warn "recall-gate plugin code missing — re-running base installer to restore it..."
    bash "$_base_install" --workspace "$WS" --agent-id "$AGENT_ID" >/dev/null 2>&1 || true
    [ -f "$PLUGIN_DST/index.ts" ] && { ok "base restored recall-gate plugin code"; _healed=1; }
  fi
  if [ "$_healed" = 0 ]; then
    warn "recall-gate plugin code not found (OPTIONAL) — mid-session recall discipline"
    warn "  stays bootstrap-only. Core memory + retrieval still work. To enable it later:"
    warn "  re-run the base installer. Not blocking."
  fi
fi
if [ -f "$PLUGIN_DST/index.ts" ] && [ -f "$PLUGIN_SRC/openclaw.plugin.json" ]; then
  # Overwrite ONLY the config; base owns index.ts/package.json.
  cp "$PLUGIN_SRC/openclaw.plugin.json" "$PLUGIN_DST/"
  ok "neuron-tier recallTools config -> $PLUGIN_DST/openclaw.plugin.json"
  if [ ! -f "$OPENCLAW_JSON" ]; then
    warn "openclaw.json not found — plugin copied but NOT wired. Add id 'dinomem-recall-gate' to plugins.allow + its path to plugins.load.paths, then restart."
  else
    DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_PLUGIN_DST="$PLUGIN_DST" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, os

path = os.environ["DINOMEM_OPENCLAW_JSON"]
plugin_dst = os.environ["DINOMEM_PLUGIN_DST"]
ws = os.environ["DINOMEM_WS"]
PID = "dinomem-recall-gate"
with open(path) as f:
    cfg = json.load(f)

changed = []
plugins = cfg.setdefault("plugins", {})

# 1) allowlist (create if absent so the plugin is explicitly permitted)
allow = plugins.get("allow")
if not isinstance(allow, list):
    allow = []
    plugins["allow"] = allow
if PID not in allow:
    allow.append(PID)
    changed.append(f"plugins.allow += {PID}")

# 1b) bundledDiscovery — REQUIRED companion to plugins.allow on OpenClaw 2026.6.x+.
if plugins.get("bundledDiscovery") not in ("compat", "allowlist"):
    plugins["bundledDiscovery"] = "compat"
    changed.append('plugins.bundledDiscovery -> "compat"')

# 2) load.paths -> abs plugin dir (dedup) + PRUNE stale workspace-relative paths.
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
    load["paths"] = paths
stale_paths = [p for p in paths
               if isinstance(p, str)
               and p.rstrip("/").endswith("/" + PID)
               and p != plugin_dst]
for sp in stale_paths:
    paths.remove(sp)
    changed.append(f"plugins.load.paths -= {sp} (stale)")
if plugin_dst not in paths:
    paths.append(plugin_dst)
    changed.append(f"plugins.load.paths += {plugin_dst}")

# 3) per-plugin config + enable -> plugins.entries.<PID>.config
#    agentFilter left as the plugin's shipped default ("" = all agents) so a
#    neuron installer gets mid-session recall on every agent out of the box.
entries = plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    entries = {}
    plugins["entries"] = entries
entry = entries.get(PID)
if not isinstance(entry, dict):
    entry = {}
    entries[PID] = entry
if entry.get("enabled") is not True:
    entry["enabled"] = True
    changed.append(f"plugins.entries.{PID}.enabled -> true")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m wire openclaw.json: {c}")
elif not changed:
    print("  \033[33m[skip]\033[0m dinomem-recall-gate already wired in openclaw.json")
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
REQUIRED_CRON_GAP=0   # set to 1 by the cron gate below if a REQUIRED lane is still missing after auto-repair
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
# 1) OpenClaw crons present (by name) — REQUIRED-CRON GATE with auto-repair.
# The 4 REQUIRED lanes below are what make the note lifecycle self-drive. If any
# is missing after install, the old behavior only WARNed (buried in green output),
# so buyers silently ran without them. Now: collect the gap, and if the gateway is
# reachable, AUTO-REPAIR by re-running this installer with --repair-cron (idempotent,
# cron-only). If we can't auto-repair, surface ONE copy-paste fix command and, on a
# normal install (not already inside a repair run), exit NONZERO so the gap is loud.
# Daily Note Review is treated as recommended-not-required (it's a redundant janitor).
_REQUIRED_CRONS="Project Advancer|Project Improver (Component 2)|Verified Note Deleter (Component 1)|Project Cron Gate"
_MISSING_REQUIRED=""
if command -v openclaw >/dev/null 2>&1; then
  _CRON_LIST="$(openclaw cron list --all --json 2>/dev/null || echo '[]')"
  # required lanes
  _IFS_SAVE="$IFS"; IFS='|'
  for _cname in $_REQUIRED_CRONS; do
    if grep -qiF "$_cname" <<< "$_CRON_LIST"; then
      sc_pass "cron present: $_cname"
    else
      sc_warn "cron MISSING (required): $_cname"
      _MISSING_REQUIRED="${_MISSING_REQUIRED}${_MISSING_REQUIRED:+, }$_cname"
    fi
  done
  IFS="$_IFS_SAVE"
  # recommended (non-blocking)
  if grep -qiF "Daily Note Review" <<< "$_CRON_LIST"; then
    sc_pass "cron present: Daily Note Review"
  else
    sc_warn "cron MISSING (recommended): Daily Note Review — general note janitor; not blocking"
  fi

  # ── AUTO-REPAIR ────────────────────────────────────────────────────────────
  # Only meaningful on a normal install. If we are ALREADY in --repair-cron and
  # lanes are still missing, a re-run won't help (real gateway/permission problem)
  # — so don't loop; just surface the manual command + fail.
  if [ -n "$_MISSING_REQUIRED" ]; then
    _REPAIR_CMD="bash $SKILL_DIR/scripts/install.sh --workspace $WS --agent-id $AGENT_ID --repair-cron"
    if [ "$REPAIR_CRON" = 0 ] && openclaw status >/dev/null 2>&1; then
      printf '  \033[33m[repair]\033[0m required cron(s) missing (%s) — auto-repairing (cron-only re-run)...\n' "$_MISSING_REQUIRED"
      if bash "$SKILL_DIR/scripts/install.sh" --workspace "$WS" --agent-id "$AGENT_ID" --repair-cron; then
        # re-verify after repair
        _CRON_LIST2="$(openclaw cron list --all --json 2>/dev/null || echo '[]')"
        _STILL_MISSING=""
        _IFS_SAVE="$IFS"; IFS='|'
        for _cname in $_REQUIRED_CRONS; do
          grep -qiF "$_cname" <<< "$_CRON_LIST2" || _STILL_MISSING="${_STILL_MISSING}${_STILL_MISSING:+, }$_cname"
        done
        IFS="$_IFS_SAVE"
        if [ -z "$_STILL_MISSING" ]; then
          sc_pass "auto-repair fixed all required crons"
          _MISSING_REQUIRED=""
        else
          sc_warn "auto-repair ran but these are STILL missing: $_STILL_MISSING"
          _MISSING_REQUIRED="$_STILL_MISSING"
        fi
      else
        sc_warn "auto-repair (--repair-cron) exited nonzero — see output above"
      fi
    fi
    # If still missing after (or without) auto-repair, surface ONE fix command.
    if [ -n "$_MISSING_REQUIRED" ]; then
      sc_warn "required cron(s) still missing: $_MISSING_REQUIRED"
      printf '  \033[33m[fix]\033[0m Run this to register them (idempotent, cron-only):\n'
      printf '        %s\n' "$_REPAIR_CMD"
      printf '  \033[33m[fix]\033[0m If it keeps failing: your gateway may reject command-kind crons.\n'
      printf '        Allow command cron jobs (operator.admin) on the gateway, then re-run the above.\n'
      REQUIRED_CRON_GAP=1
    fi
  fi
else
  sc_warn "openclaw CLI not found on PATH — cannot verify/register crons"
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
# 5) Hook liveness (L3/L3b): `hooks enable` only sets the config flag; if $WS is
#    not the gateway's scanned workspace, the hook in $WS/hooks/ silently never
#    loads. Assert eligibility via `hooks check --json` (grep on the table is
#    unreliable — emoji wrap splits names). Self-heal into global ~/.openclaw/hooks/.
if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
  _elig="$(openclaw hooks check --json 2>/dev/null | "$PY" -c "import json,sys;
try:
  d=json.load(sys.stdin); print('yes' if 'dinomem-open-notes' in d.get('hooks',{}).get('eligible',[]) else 'no')
except Exception: print('unknown')" 2>/dev/null)"
  if [ "$_elig" = "yes" ]; then
    sc_pass "dinomem-open-notes hook is eligible (gateway can load it)"
  else
    _ghd="${OPENCLAW_DIR:-$HOME/.openclaw}/hooks"
    if [ -d "$SKILL_DIR/hooks/dinomem-open-notes" ]; then
      mkdir -p "$_ghd"
      # dino-guard-rmrf: only remove the global-fallback hook if it is ours
      # (marker file names this agent) OR unmarked (legacy). Never blow away
      # another agent that also fell back to the shared global hooks dir.
      _hmk="$_ghd/dinomem-open-notes/.dino-agent"
      if [ ! -e "$_ghd/dinomem-open-notes" ] || [ ! -f "$_hmk" ] || [ "$(cat "$_hmk" 2>/dev/null)" = "$AGENT_ID" ]; then
        rm -rf "$_ghd/dinomem-open-notes"
        cp -r "$SKILL_DIR/hooks/dinomem-open-notes" "$_ghd/dinomem-open-notes"
        printf "%s" "$AGENT_ID" > "$_ghd/dinomem-open-notes/.dino-agent" 2>/dev/null || true
      else
        echo "  [warn] global-fallback hook owned by a different agent ($(cat "$_hmk" 2>/dev/null)); not overwriting"
      fi
      openclaw hooks enable dinomem-open-notes >/dev/null 2>&1 || true
      sc_warn "dinomem-open-notes not eligible from \$WS/hooks/ — copied to global $_ghd/ + re-enabled; RESTART OpenClaw, then verify: openclaw hooks check --json"
    else
      sc_warn "dinomem-open-notes not eligible and source missing — check the install"
    fi
  fi
fi
if [ "$SC_FAIL" = 0 ]; then
  printf '  \033[32m== self-check: all green ==\033[0m\n'
else
  printf '  \033[33m== self-check: some items need attention (see WARN above) ==\033[0m\n'
fi
echo ""

# HARD-FAIL on a REQUIRED-cron gap so it can never hide under green output again.
# In --repair-cron mode we still exit nonzero (the caller/auto-repair reads it),
# but we do NOT print the long "done / next steps" banner for a cron-only run.
if [ "$REQUIRED_CRON_GAP" = 1 ]; then
  printf '\033[31m  ✖ REQUIRED cron lane(s) missing — the note lifecycle will NOT self-drive.\033[0m\n'
  printf '    Fix with:  bash %s/scripts/install.sh --workspace %s --agent-id %s --repair-cron\n' "$SKILL_DIR" "$WS" "$AGENT_ID"
  exit 1
fi

# repair-cron mode: crons are green, nothing else to do — skip the full install banner.
if [ "$REPAIR_CRON" = 1 ]; then
  hr "repair-cron done"
  echo "  Required crons registered/verified for agent: $AGENT_ID"
  echo "  Restart OpenClaw to activate:  openclaw gateway restart"
  exit 0
fi

hr "done"
# ── Tiered "what works now" verdict (noob-smooth) ────────────────────────────
# Same rationale as base: lead with plain-language status so a first-timer knows
# they're good, not broken, even when OPTIONAL layers (TEI/Docker, KB search tools)
# are off. Core memory + learning lanes work regardless.
echo "  \033[1;32m✔ dinomem-neuron is installed. Your agent remembers AND learns now.\033[0m"
echo "    Core memory + the note-lifecycle crons (advance/improve/retire) are active."
if command -v docker >/dev/null 2>&1 && curl -fsS http://localhost:8080/health >/dev/null 2>&1; then
  echo "    \033[32m✔ Fast semantic recall (TEI) is running.\033[0m"
else
  echo "    \033[2m○ Optional: faster semantic recall (TEI/Docker) is off — memory still works without it.\033[0m"
fi
if python3 -c 'import langchain_chroma' >/dev/null 2>&1; then
  echo "    \033[32m✔ KB vector-search tools (docs/session/semantic_search) are ready.\033[0m"
else
  echo "    \033[2m○ Optional: KB vector-search tools need Python packages that didn't install—\033[0m"
  echo "    \033[2m      core memory_search is unaffected. Enable later: pip install langchain-chroma langchain-huggingface\033[0m"
fi
echo ""
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
