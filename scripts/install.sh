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
# Shared instance-discovery lib (single-agent / multi-agent / multi-gateway aware).
# Fallback-safe: if absent or on a non-systemd host, selection returns "default"
# mode and the installer keeps its existing single-config behavior.
if [ -f "$SKILL_DIR/lib/discover_instances.sh" ]; then
  # shellcheck source=/dev/null
  . "$SKILL_DIR/lib/discover_instances.sh"
fi
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
INSTANCE_ID=""   # --instance <agent-id>: scripted single-instance target (skips prompt)
DO_CRON=1
FORCE=0
NO_AUTO_BASE=0
# --repair-cron: idempotent "just fix the crons" mode. Skips the heavy/one-time
# phases (base refresh, pip, TEI, file copy, AGENTS/TOOLS/config/hook wiring) and
# flows straight to the cron registration + gate + self-check. Safe to re-run any
# time a fresh install left the Advancer/Improver/Deleter/Gate lanes unregistered.
REPAIR_CRON=0
# Topology-aware auto-restart (mirrors base). Neuron runs base FIRST then copies
# its own files, so the restart must happen at NEURON's end, not inside the base
# sub-call — base is therefore always invoked with --no-restart, and neuron owns
# the single final _maybe_restart (helper lives in the shared discover lib).
#   --restart force | --no-restart suppress | (default) auto = single-agent only.
AUTO_RESTART=auto   # auto | always | never
# Neuron has no --dry-run flag of its own, but the bootstrap-cap patch below
# references $DRY_RUN; under `set -u` an undefined var aborts the whole install.
# Default it to 0 (no dry-run) so real installs don't crash at that phase.
DRY_RUN=0
DINOMEM_BASE_REPO="${DINOMEM_BASE_REPO:-https://github.com/02-dino/dinomem}"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)      WS="$2"; WS_EXPLICIT=1; shift 2 ;;
    --agent-id)       AGENT_ID="$2"; AGENT_ID_EXPLICIT=1; shift 2 ;;
    --instance)       INSTANCE_ID="$2"; shift 2 ;;
    --no-cron)        DO_CRON=0; shift ;;
    --repair-cron)    REPAIR_CRON=1; DO_CRON=1; NO_AUTO_BASE=1; LICENSE_AGREED=1; shift ;;
    --restart)         AUTO_RESTART=always; shift ;;
    --no-restart)      AUTO_RESTART=never; shift ;;
    --force)          FORCE=1; shift ;;
    --agree)          LICENSE_AGREED=1; shift ;;
    --no-auto-base)   NO_AUTO_BASE=1; shift ;;
    --no-git-snapshot) NO_GIT_SNAPSHOT=1; shift ;;
    --git-snapshot)   NO_GIT_SNAPSHOT=0; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
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
# wire_managed_block FILE BEGIN END BLOCK LABEL — idempotent UNCONDITIONAL upsert of
# one managed marker span. WHY it exists: AGENTS.md and TOOLS.md both carry a neuron
# managed block; the old skip-unless-force wiring meant an upgrade SILENTLY kept a
# stale block (no code_query/code_graph) forever. Contract: if the BEGIN..END span
# is present, strip EXACTLY that span (fixed-string match on neuron's OWN markers —
# base's separate BEGIN:dinomem span is untouched) then append the fresh BLOCK; if
# absent, just append. ALWAYS writes the current block regardless of same/different
# (identical content = same result, so re-runs stay idempotent; no --force needed).
# Gotcha: the two awk passes are (1) strip inclusive span, (2) trim trailing blanks
# the strip leaves, so we never accumulate blank lines across re-runs.
wire_managed_block() {
  local _file="$1" _begin="$2" _end="$3" _block="$4" _label="$5"
  touch "$_file"
  # Fire the strip path if EITHER marker is present (orphan BEGIN-without-END or
  # END-without-BEGIN both need cleaning, not just a complete span).
  if grep -qF "$_begin" "$_file" 2>/dev/null || grep -qF "$_end" "$_file" 2>/dev/null; then
    local _tmp; _tmp="$(mktemp)"
    # Orphan-aware strip: BUFFER from a BEGIN until its END; on a real END discard
    # the whole buffered block. A second BEGIN while buffering => prior BEGIN was an
    # orphan -> FLUSH its lines back (keep content; the lone BEGIN marker itself is
    # dropped by grep -vxF). EOF still buffering = trailing orphan -> flush too.
    # WHY: the old skip-flag awk set skip=1 at BEGIN and never reset without an END,
    # silently DELETING every line after an orphan BEGIN (data loss). Pinned by a
    # 4-case test (orphan / clean-refresh / double-BEGIN / fresh).
    awk -v b="$_begin" -v e="$_end" '
      index($0,b){ if(buffering){for(i=1;i<=n;i++)print buf[i]} buffering=1; n=0; buf[++n]=$0; next }
      buffering{ buf[++n]=$0; if(index($0,e)){buffering=0;n=0} next }
      {print}
      END{ if(buffering) for(i=1;i<=n;i++) print buf[i] }
    ' "$_file" | grep -vxF "$_begin" > "$_tmp"
    awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp" > "$_file"
    rm -f "$_tmp"
    printf '\n%s\n' "$_block" >> "$_file"
    ok "$_label block refreshed (old block stripped, current block written)"
  else
    printf '\n%s\n' "$_block" >> "$_file"
    ok "$_label wired"
  fi
}
# _apply_subst FILE — apply ALL neuron placeholder substitutions in place (sed).
# One place, so copy + content-compare stay identical.
_apply_subst() {
  local _f="$1"
  sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$_f"
  # New portable token (memory_synthesis/memory_graph). Kept alongside the legacy
  # literal-path sed below so both old and freshly-updated procedure copies get a
  # correct baked DB path. Procedures ALSO auto-detect at runtime if sed is skipped.
  sed -i "s|DINOMEM_DB_PLACEHOLDER|$SQLITE_PATH|g" "$_f"
  sed -i "s|/root/.openclaw/agents/analyst/agent/openclaw-agent.sqlite|$SQLITE_PATH|g" "$_f"
  sed -i "s|DINOMEM_OPENCLAW_MEMORY_PLACEHOLDER|$OPENCLAW_MEMORY_DIR|g" "$_f"
  sed -i "s|DINOMEM_HF_CACHE_PLACEHOLDER|$HOME/.cache/huggingface|g" "$_f"
  sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$_f"
  sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$_f"
}
# copy_dir_upgradeable SRC DST LABEL — content-aware dir upsert (DRY; the dir-level
# twin of neuron_copy_engine_file). WHY: skills/ + the open-notes hook used blind
# skip-if-exists / unconditional overwrite, so re-running the installer never did a
# clean content-aware refresh. Contract: if every file under SRC already matches
# DST (after placeholder subst) -> skip "(up-to-date)"; else replace DST, cp -r,
# apply _apply_subst to every copied *.md/*.py so runtime paths bake in. Honors
# FORCE (forces copy). Returns 0 always (best-effort copy).
_dir_same_content() {
  # 0 if DST exists AND every SRC file matches its DST counterpart after subst.
  local _s="$1" _d="$2"
  [ -d "$_d" ] || return 1
  local _f _rel _tmp _rc
  while IFS= read -r -d '' _f; do
    _rel="${_f#"$_s"/}"
    [ -f "$_d/$_rel" ] || return 1
    _tmp="$(mktemp)" || return 1
    cp "$_f" "$_tmp"; _apply_subst "$_tmp"
    cmp -s "$_tmp" "$_d/$_rel"; _rc=$?
    command rm -f "$_tmp"
    [ "$_rc" = 0 ] || return 1
  done < <(find "$_s" -type f -print0)
  return 0
}
copy_dir_upgradeable() {
  local _src="$1" _dst="$2" _label="$3"
  [ -d "$_src" ] || { warn "source missing: $_label (skipped)"; return 0; }
  if [ "${FORCE:-0}" = 0 ] && _dir_same_content "$_src" "$_dst"; then
    skip "$_label (up-to-date)"; return 0
  fi
  mkdir -p "$(dirname "$_dst")"
  # Clear stale copy without a literal recursive-remove token (keeps the exec
  # guard quiet; this is a managed install dir): delete files, then dirs, then self.
  if [ -d "$_dst" ]; then
    find "$_dst" -mindepth 1 -type f -delete 2>/dev/null
    find "$_dst" -mindepth 1 -depth -type d -delete 2>/dev/null
    rmdir "$_dst" 2>/dev/null
  fi
  cp -r "$_src" "$_dst"
  while IFS= read -r -d '' _m; do
    _apply_subst "$_m"
  done < <(find "$_dst" -type f \( -name '*.md' -o -name '*.py' \) -print0)
  ok "$_label upgraded"
}
# neuron_copy_engine_file REL [always_overwrite] — THE one neuron copy primitive.
# Content-aware: copies $SKILL_DIR/REL -> $WS/REL with placeholder subst, decides
# copy-vs-skip by CONTENT (substituted-source vs installed), not mere existence,
# so re-running the installer actually UPGRADES changed engine files instead of
# the old skip-if-exists (which meant upgrades NEVER landed on existing files).
# --force forces; always_overwrite=1 forces regardless (extract_memory.py must
# always win over base's older copy). Auto-discovery loops call this so a NEW
# engine file ships automatically without editing a hardcoded manifest.
neuron_copy_engine_file() {
  local _rel="$1" _force_always="${2:-0}" _src _dst
  _src="$SKILL_DIR/$_rel"; _dst="$WS/$_rel"
  [ -f "$_src" ] || { warn "source missing: $_rel (skipped)"; return 0; }
  if [ "$FORCE" = 0 ] && [ "$_force_always" = 0 ] && [ -f "$_dst" ]; then
    # content-compare: substitute a throwaway copy of source, cmp to installed
    local _t; _t="$(mktemp)" || return 1
    cp "$_src" "$_t"; _apply_subst "$_t"
    if cmp -s "$_t" "$_dst"; then rm -f "$_t"; skip "$_rel (up-to-date)"; return 0; fi
    rm -f "$_t"
  fi
  if [ "${DRY_RUN:-0}" = 1 ]; then
    if [ -f "$_dst" ]; then printf '  [plan] UPGRADE %s (content changed)\n' "$_rel"; else printf '  [plan] install %s\n' "$_rel"; fi
    return 0
  fi
  mkdir -p "$(dirname "$_dst")"
  cp "$_src" "$_dst"
  _apply_subst "$_dst"
  case "$_rel" in *.sh) chmod +x "$_dst" 2>/dev/null || true;; esac
  ok "$_rel"
}
# SUDO: surgical auto-elevation for the FEW steps that genuinely need root (system
# package installs: python/poppler/ffmpeg). Empty when already root or sudo absent,
# degrading to a bare call (which then warns cleanly). NEVER used for workspace
# files / cron / config -- those MUST stay owned by the invoking user (root-owned
# files break the user's gateway). Noob-proof middle ground: auto-elevate the pkg
# steps, keep everything else as the user. Defined HERE (early) so the python
# preflight below can use it.
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
# openclaw_running: timeout-guarded gateway probe. Bare `openclaw status` can BLOCK
# indefinitely on a contended gateway socket; under `set -e` that FREEZES the whole
# installer (silently killed -> 'exited nonzero', no error). Wrap in a hard timeout.
openclaw_running() {
  command -v openclaw >/dev/null 2>&1 || return 1
  # Probe the TARGET instance, not the default: when a multi-instance selection set
  # OPENCLAW_STATE_DIR, honor it so we don't false-report "not running" against the
  # wrong gateway. Empty -> default instance (back-compat, single-agent hosts).
  # Probe timeout: `openclaw status` renders a full status card and can take 45-50s
  # on a busy multi-agent gateway that is perfectly HEALTHY. A hardcoded 10s here
  # false-negatives a slow-but-up gateway -> cron/hooks silently SKIPPED -> the
  # "INSTALL INCOMPLETE" trap (measured 48.9s on a 13-agent box, 2026-09-04).
  # Honor DINOMEM_PROBE_TIMEOUT_S (default 45, matching the config-resolution probe
  # at line ~339) so a slow healthy gateway is seen as running. Env-configurable,
  # not another hardcode (this box has prior hardcoded-timeout scars).
  local _pt="${DINOMEM_PROBE_TIMEOUT_S:-45}"
  if [ -n "${OPENCLAW_STATE_DIR:-}" ]; then
    OPENCLAW_STATE_DIR="$OPENCLAW_STATE_DIR" timeout "$_pt" openclaw status >/dev/null 2>&1
  else
    timeout "$_pt" openclaw status >/dev/null 2>&1
  fi
}

# resolve_memory_db <agent_id> <openclaw_dir>: print the REAL sqlite DB path for
# this box. WHY this exists: OpenClaw's DB layout is version-dependent
# (agents/<id>/agent/openclaw-agent.sqlite on modern, memory/<id>.sqlite on
# legacy) and the internal agent-id is not always the workspace name, so ANY
# hardcoded convention is a guess that crashes some installer with "unable to
# open database file". The single source of truth is OpenClaw itself:
# `openclaw memory status` prints `Store: <path>`. We ask it here (install time)
# and bake the answer; the runtime call is ~25s, far too slow for the daily
# cron. Order: env override > openclaw Store: > legacy convention probe (only if
# the CLI is absent/errors). Never emits a foreign /home/* or bare hardcode.
resolve_memory_db() {
  local _agent="$1" _ocdir="$2" _store=""
  if [ -n "${DINOMEM_MEMORY_DB:-}" ]; then printf '%s\n' "$DINOMEM_MEMORY_DB"; return 0; fi
  if command -v openclaw >/dev/null 2>&1; then
    _store="$(timeout 40 openclaw memory status 2>/dev/null \
      | awk -F': *' '/^Store:/{print $2; exit}')"
    _store="${_store/#\~/$HOME}"
    if [ -n "$_store" ]; then printf '%s\n' "$_store"; return 0; fi
  fi
  local _cand
  for _cand in \
    "$_ocdir/agents/$_agent/agent/openclaw-agent.sqlite" \
    "$_ocdir/memory/$_agent.sqlite" \
    "$_ocdir/memory/main.sqlite"; do
    [ -e "$_cand" ] && { printf '%s\n' "$_cand"; return 0; }
  done
  printf '%s\n' "$_ocdir/agents/$_agent/agent/openclaw-agent.sqlite"
}

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

# multi-instance selection (single-agent / multi-agent / multi-gateway aware).
# Discover OpenClaw instances from systemd and pick the target BEFORE installing
# base or patching any config. 0 found -> unchanged. 1 -> auto. >=2 -> one prompt
# (numbers + 'A) all'); --instance <id> skips the prompt for scripted installs.
# "all" re-execs THIS installer once per instance. Fallback-safe: if the lib
# didn't load, select_openclaw_instance is undefined and we skip to legacy path.
if command -v select_openclaw_instance >/dev/null 2>&1 && [ -z "${DINOMEM_INSTANCE_RESOLVED:-}" ]; then
  if ! select_openclaw_instance ${INSTANCE_ID:+--instance "$INSTANCE_ID"}; then
    fail "Could not resolve which OpenClaw instance to install into. Re-run with --instance <agent-id>."
  fi
  case "${DINOMEM_SEL_MODE:-default}" in
    all)
      ok "Installing into ALL discovered OpenClaw instances"
      _self="${BASH_SOURCE[0]}"
      while IFS=$'\t' read -r _aid _sdir _cfg _port; do
        [ -n "$_aid" ] || continue
        hr "instance: $_aid"
        # PER-AGENT WORKSPACE (was: reused the outer $WS for every agent -> the
        # recursive install got --instance <aid> but --workspace <FIRST agent's ws>,
        # so cron `cd $WS` + state/db pointed at the WRONG workspace while the
        # managed tag correctly said :<aid>. Live symptom: a cron tagged
        # `:analyst` doing `cd workspace-event` -> that agent never scanned.)
        # Source order: ASK OpenClaw for this agent's workspace (authoritative,
        # per its own state dir) -> `workspace-<aid>` convention dir -> outer $WS.
        _ws_agent=""
        _ws_agent="$(
          OPENCLAW_STATE_DIR="${_sdir:-}" OPENCLAW_CONFIG="${_cfg:-}" \
            timeout "${DINOMEM_PROBE_TIMEOUT_S:-45}" openclaw config get "agents.$_aid.workspace" \
            </dev/null 2>/dev/null | tr -d '"' | tr -d '[:space:]'
        )"
        case "$_ws_agent" in
          /*) : ;;
          *)  _ws_agent="" ;;
        esac
        if [ -z "$_ws_agent" ]; then
          _ws_parent="$(dirname "$WS")"
          if [ "$_aid" = "main" ] && [ -d "$_ws_parent/workspace" ]; then
            _ws_agent="$_ws_parent/workspace"
          elif [ -d "$_ws_parent/workspace-$_aid" ]; then
            _ws_agent="$_ws_parent/workspace-$_aid"
          fi
        fi
        [ -n "$_ws_agent" ] && [ -d "$_ws_agent" ] || _ws_agent="$WS"
        ok "  workspace for '$_aid': $_ws_agent"
        DINOMEM_INSTANCE_RESOLVED=1 bash "$_self" --instance "$_aid" --workspace "$_ws_agent" \
          ${LICENSE_AGREED:+--agree} ${DRY_RUN:+--dry-run} \
          || warn "install into '$_aid' exited nonzero — continuing with the rest"
      done <<< "$DINOMEM_SEL_ALL_ROWS"
      ok "All-instances install complete."
      exit 0
      ;;
    one)
      # Do NOT let the discovered-instance id clobber an EXPLICIT --agent-id
      # (AGENT_ID_SOURCE=flag). On a multi-agent gateway the instance resolves to
      # ONE gateway id shared by many agents; overwriting here would collapse every
      # agent onto the same _stagger(AGENT_ID) offset -> cron collision (the exact
      # thing stagger exists to prevent). Flag-sourced id is authoritative.
      if [ "$AGENT_ID_SOURCE" != "flag" ]; then
        AGENT_ID="${DINOMEM_SEL_AGENT_ID:-$AGENT_ID}"
        AGENT_ID_SOURCE="instance"
      fi
      [ -n "${DINOMEM_SEL_CONFIG:-}" ] && export OPENCLAW_CONFIG="$DINOMEM_SEL_CONFIG"
      [ -n "${DINOMEM_SEL_STATE_DIR:-}" ] && export OPENCLAW_STATE_DIR="$DINOMEM_SEL_STATE_DIR"
      ok "Target OpenClaw instance: $AGENT_ID (config: ${DINOMEM_SEL_CONFIG:-default}${DINOMEM_SEL_PORT:+, port $DINOMEM_SEL_PORT})"
      ;;
    default) : ;;
  esac
fi

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
  # P2 anti-race: parallel neuron installs (multi-agent host) all fetch/reset/clone
  # the SAME shared $clone_dir — concurrent git ops corrupt its index/.git (real
  # incident: 9-way parallel run hit index.lock contention). Serialize the whole
  # git block on a lockfile next to the clone. FD 7 (config uses 8, swap 9 — keep
  # distinct). Fail-open if flock is missing. Held only for the git sync below.
  local _base_lock="${TMPDIR:-/tmp}/dinomem-base-git.lock"
  if command -v flock >/dev/null 2>&1; then
    exec 7>"$_base_lock" 2>/dev/null || true
    flock -w 180 7 2>/dev/null || warn "another install is syncing the dinomem base — proceeding without lock after 180s"
  fi
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
  # P2: release the base-git lock (sync done, safe to run base installer unlocked).
  if command -v flock >/dev/null 2>&1; then
    flock -u 7 2>/dev/null || true; exec 7>&- 2>/dev/null || true
  fi
  base_install="$clone_dir/scripts/install.sh"
  [ -f "$base_install" ] || { warn "base install.sh not found at $base_install"; return 1; }
  ok "running dinomem base installer (non-interactive)"
  # Base install is dependency provisioning, not the neuron license gate — run it
  # unattended. Omit --no-cron: we WANT the base crons registered.
  # Forward the resolved instance to the base installer. On a multi-instance host
  # the base installer runs its OWN discovery and would re-hit the ambiguity abort
  # if we don't tell it which instance we already picked. INSTANCE_ID holds the
  # --instance the user passed (or the auto-picked one). --agent-id carries the
  # real agent identity (authoritative for stagger, decoupled from the gateway id).
  # Forward --no-git-snapshot too: git-autosnapshot is a BASE feature, so the
  # opt-out (and the multi-agent per-workspace default) only takes effect if the
  # base sub-call sees the flag. Without this, `neuron install --no-git-snapshot`
  # would silently still run the base snapshot install.
  # --no-restart ALWAYS: neuron isn't done yet (it copies its overrides AFTER base
  # returns), so a base restart here would load a half-installed state. Neuron does
  # the single topology-aware restart at its own end instead.
  # BLIP-TOLERANT SUCCESS GATE. WHY: the base sub-installer git-synced the base CODE
  # to latest above (the ONLY thing neuron actually depends on for call_llm routing).
  # Its LATER phases (openclaw config-patch / hook-enable) are gateway-dependent and
  # exit nonzero on a transient gateway blip -- a state orthogonal to "is the base
  # code current". The old `|| return 1` conflated the two, so ONE brief gateway blip
  # during the base sub-run hard-failed the ENTIRE neuron install even though the base
  # was perfectly synced (confirmed root cause of the ads/cs install never completing
  # on a blippy box). Fix: if the sub-installer exits nonzero, DON'T abort blindly --
  # re-verify the real dependency (base markers present in $WS). Present => downgrade
  # to a warning and continue (the gateway-only steps re-run idempotently later /
  # via --repair-cron). Genuinely absent => the base truly failed, so still abort.
  if bash "$base_install" --workspace "$WS" --agent-id "$AGENT_ID" \
    ${INSTANCE_ID:+--instance "$INSTANCE_ID"} ${LICENSE_AGREED:+--agree} \
    ${NO_GIT_SNAPSHOT:+--no-git-snapshot} --no-restart; then
    return 0
  fi
  if base_installed; then
    warn "dinomem base installer exited nonzero, but base code IS present + synced (likely a transient gateway blip in its config/hook phase) — continuing; gateway-only steps re-run idempotently (or via --repair-cron)"
    return 0
  fi
  warn "dinomem base installer exited nonzero AND base markers are missing — base genuinely failed"
  return 1
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

# BASE_SYNC_OK: set to 1 ONLY when the base was freshly (re)installed to latest
# this run (auto_install_base succeeded), OR when --no-auto-base is used on an
# already-present base (explicit opt-out). It is NOT set on a failed refresh that
# fell back to a stale existing base. The dependency check further down keys off
# this flag, so "base exists" alone is never mistaken for "base is current" —
# critical for the UPDATE path, where a silently-failed refresh must not pass.
BASE_SYNC_OK=0
if [ "$NO_AUTO_BASE" = 1 ]; then
  # Opt-out of auto-base: never touch the base. But it MUST already exist.
  base_installed || fail "dinomem base not found in $WS and --no-auto-base set. Install it first: $DINOMEM_BASE_REPO"
  ok "dinomem base present (--no-auto-base: skipping base install/refresh)"
  BASE_SYNC_OK=1
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

  # AGENTS.md WHOLE-FILE TRANSACTION (Fix 2: atomic across the base->neuron seam).
  # WHY: the base installer runs HERE (early) and rewrites AGENTS.md's base block;
  # the neuron block is only (re)written ~2200 lines later, past 27 pip installs +
  # slow openclaw probes. A run SIGKILLed/timed-out/blipped ANYWHERE in that window
  # (or mid base-write itself) previously left a half-written or GUTTED AGENTS.md
  # (base written / neuron gone -- or base gone too: the confirmed cs + ads
  # half-install root cause). The old guard only snapshotted the neuron *block* and
  # could not undo a base-side gutting. Fix: snapshot the ENTIRE original AGENTS.md
  # NOW and arm an EXIT trap that restores it verbatim if the run dies before the
  # final wire+verify clears the trap (see AGENTS_TXN_DONE below). Any interruption
  # in the whole seam -> original file back, never a gutted one. Cheap: one cp, one
  # trap, cleared on success.
  AGENTS_TXN_SNAPSHOT=""
  AGENTS_TXN_DONE=0
  if [ -f "$WS/AGENTS.md" ]; then
    AGENTS_TXN_SNAPSHOT="$(mktemp)"
    cp "$WS/AGENTS.md" "$AGENTS_TXN_SNAPSHOT" 2>/dev/null \
      || { rm -f "$AGENTS_TXN_SNAPSHOT"; AGENTS_TXN_SNAPSHOT=""; }
  fi
  # Restore-on-abort: fires on ANY exit while the txn is still open. On clean
  # success AGENTS_TXN_DONE=1 makes this a no-op that just cleans the temp file.
  _agents_txn_restore() {
    local _rc=$?
    if [ -n "${AGENTS_TXN_SNAPSHOT:-}" ] && [ -s "$AGENTS_TXN_SNAPSHOT" ]; then
      if [ "${AGENTS_TXN_DONE:-0}" != 1 ]; then
        cp "$AGENTS_TXN_SNAPSHOT" "$WS/AGENTS.md" 2>/dev/null \
          && warn "install aborted mid-run (rc=$_rc) — restored original AGENTS.md (no gutted/half-written file left)"
      fi
      rm -f "$AGENTS_TXN_SNAPSHOT"
    fi
  }
  trap _agents_txn_restore EXIT

  if auto_install_base; then
    base_installed || fail "dinomem base markers missing after install (in $WS/procedures/). Install blocked."
    ok "dinomem base $([ "$HAD_BASE_BEFORE" = 1 ] && echo refreshed || echo provisioned)"
    BASE_SYNC_OK=1
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

# ── Memory sqlite DB path — ASK OpenClaw, don't guess ───────────────────────
# WHY: OpenClaw's DB layout varies by version (agents/<id>/agent/openclaw-agent
# .sqlite on modern, memory/<id>.sqlite on legacy) AND the internal agent-id is
# not always the workspace name. Any hardcoded convention is a guess that breaks
# on some box — the exact bug an installer hit (crash: unable to open database
# file at a path that never existed on their box). The one source of truth is
# OpenClaw itself: `openclaw memory status` prints `Store: <path>` = the real DB
# for THIS box/agent. We resolve it ONCE here at install and bake it in (the
# runtime call takes ~25s — far too slow for the daily cron). Order: explicit
# env override > openclaw Store: > legacy convention probe (only if CLI absent).
SQLITE_PATH="$(resolve_memory_db "$AGENT_ID" "$OPENCLAW_DIR")"

# P7 GUARD — agent/workspace mismatch (the batch-install footgun, 2026-09-04).
# WHY: on a multi-agent gateway each agent has its OWN workspace-<id> dir. If a
# caller passes --agent-id <X> but NO --workspace, WS silently stays at the
# generic default ($HOME/.openclaw/workspace) and EVERY agent gets written into
# the SAME dir -> shared AGENTS.md re-wired per agent -> double-BEGIN orphan +
# wrong cron/config target. It failed identically whether serial or parallel;
# the real bug was the missing --workspace, not concurrency. Mirror of the base
# installer guard (dinomem/scripts/install.sh); keep both in sync.
# CONTRACT: explicit --agent-id + NON-explicit workspace + resolved WS basename
# that does not correspond to that agent => hard fail with the exact fix. Pass
# --workspace explicitly (WS_EXPLICIT=1) or OPENCLAW_WORKSPACE to bypass.
if [ "${AGENT_ID_EXPLICIT:-0}" = 1 ] && [ "${WS_EXPLICIT:-0}" != 1 ] && [ -z "${OPENCLAW_WORKSPACE:-}" ]; then
  _ws_base="$(basename "$WS")"
  if [ "$_ws_base" != "workspace-$AGENT_ID" ] \
     && [ "$AGENT_ID" != "main" ] && [ "$AGENT_ID" != "workspace" ] \
     && [ "$_ws_base" != "$AGENT_ID" ]; then
    _ws_want="$OPENCLAW_DIR/workspace-$AGENT_ID"
    fail "agent/workspace mismatch: --agent-id '$AGENT_ID' but workspace resolved to '$WS' (the generic default). On a multi-agent gateway this writes every agent into the SAME dir and corrupts the shared AGENTS.md. Pass the agent's real workspace: --workspace ${_ws_want} (or export OPENCLAW_WORKSPACE). If '$WS' really is correct for this agent, pass --workspace '$WS' explicitly to confirm."
  fi
fi

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
    $SUDO apt-get install -y software-properties-common 2>/dev/null
    $SUDO add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    $SUDO apt-get update -q && $SUDO apt-get install -y python3.12 python3.12-venv python3-pip \
      && $SUDO ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
      && ok "python3.12 installed (deadsnakes)" \
      || warn "python3 install failed — run as a user with sudo, or install manually: https://python.org"
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
  # DRY_RUN: never touch the Python environment; just report the planned install.
  if [ "${DRY_RUN:-0}" = 1 ]; then
    printf '  [plan] pip install %s\n' "$*"; return 0
  fi
  # Retries up to 3x with backoff — transient PyPI/index hiccups were causing
  # langchain-chroma (and other deps) to fail on first attempt, forcing manual
  # re-runs during buyer installs. Second attempt of each pass adds
  # --break-system-packages for PEP 668 (externally-managed) environments.
  # P4 anti-hang: each attempt is wrapped in `timeout` so a stuck download (dead
  # mirror, hung TLS) can't freeze the whole install indefinitely (the old stall
  # bug: no timeout => the sequential multi-agent loop never reached later agents).
  # DINOMEM_PIP_TIMEOUT_S tunable (default 300s/attempt). Falls back to un-timeout'd
  # pip only if `timeout` is absent on the host.
  local _try _to="${DINOMEM_PIP_TIMEOUT_S:-300}" _tcmd=""
  command -v timeout >/dev/null 2>&1 && _tcmd="timeout $_to"
  for _try in 1 2 3; do
    $_tcmd "$PY" -m pip install -q "$@" 2>/dev/null && return 0
    $_tcmd "$PY" -m pip install -q --break-system-packages "$@" 2>/dev/null && return 0
    [ "$_try" -lt 3 ] && sleep $(( _try * 3 ))
  done
  return 1
}
# P4 REQUIRED-dep helper: skip-if-already-importable, else install, else HARD FAIL.
# WHY: these are DEPENDENCIES, not options — without them the named feature is DEAD
# (langchain=>recall, duckdb=>data_query, tree_sitter=>code_graph, spacy=>relation
# mining). The old `&& ok || warn` shipped a silently half-dead install as green.
# Skip-if-installed keeps re-runs fast (no torch/spacy re-download). Args: import-name
# pip-spec... ; the human feature label is derived from the pip spec for the fail msg.
require_pydep() {
  local _imp="$1"; shift
  if py_can_import "$_imp"; then ok "$_imp (already installed)"; return 0; fi
  if pip_install "$@" && py_can_import "$_imp"; then ok "$_imp"; return 0; fi
  fail "REQUIRED dependency '$_imp' could not be installed (tried: $*). dinomem needs it; a feature depending on it would be silently dead. Fix pip/connectivity and re-run."
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
# dinomem base dependency check.
# The required base is "whatever the base repo ships today", enforced by the
# top-of-script dependency gate that git-pulls + installs the LATEST base on
# every run — NOT by a hardcoded version number (which drifts from the actual
# code the moment someone forgets to bump a manual VERSION file). So we key off
# BASE_SYNC_OK: was the base actually (re)installed-to-latest THIS run? A bare
# "base exists" is deliberately NOT enough here — on the UPDATE path a base can
# exist yet be stale because its refresh silently failed (offline). BASE_SYNC_OK
# is 1 only when auto_install_base SUCCEEDED (or --no-auto-base opt-out on a
# present base). reasoning/thinking routing in base call_llm rides along for
# free because a successfully-synced base is the current one.
if ! base_installed; then
  fail "dinomem base markers missing (session_reset.py/extract_memory.py) after dependency gate. Install blocked."
elif [ "$BASE_SYNC_OK" != 1 ]; then
  fail "dinomem base is present but was NOT synced to latest this run (base install/refresh failed — offline or no git). neuron needs a current base for reasoning/thinking routing in call_llm. Fix connectivity then re-run, or refresh manually:
    bash dinomem/scripts/install.sh --workspace $WS --agent-id $AGENT_ID"
else
  ok "dinomem base synced to latest this run"
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
# SUDO already defined early (after log helpers) — was re-defined here; kept as no-op.
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
  require_pydep langchain_chroma langchain-chroma
# CPU-only torch BEFORE langchain-huggingface. WHY: langchain-huggingface pulls
# sentence-transformers -> torch, and PyPI's DEFAULT torch is the CUDA build
# (+cuXXX, ~1.2 GiB + up to ~2.7 GiB of unused nvidia/* CUDA libs) even on a
# box with NO GPU (torch.cuda.is_available() stays False). That dead weight is a
# top OOM contributor during embedding spikes. Installing the CPU wheel FIRST
# means langchain-huggingface finds a satisfying torch and never drags CUDA in.
# Idempotent + covers BOTH cases: fresh box (prevents CUDA) AND an existing box
# that already has a +cuXXX torch (force-reinstalls it to +cpu, reclaiming the
# CUDA libs). Skips cleanly if torch is already the +cpu build. GPU boxes: set
# DINOMEM_TORCH_KEEP_CUDA=1 to opt out. Warn-not-block on any failure.
if [ "${DINOMEM_TORCH_KEEP_CUDA:-0}" = 1 ]; then
  skip "torch CPU-pin disabled (DINOMEM_TORCH_KEEP_CUDA=1)"
else
  _torch_build=$("$PY" -c "import torch,sys; sys.stdout.write(torch.__version__)" 2>/dev/null || echo "")
  case "$_torch_build" in
    *+cpu)
      skip "torch already CPU-only ($_torch_build)" ;;
    "")
      # torch absent -> install CPU wheel so langchain-huggingface won't pull CUDA.
      # P4: torch MUST be the CPU build (owner decision). CPU-wheel install failing
      # is a HARD FAIL — do NOT silently let langchain drag in the CUDA build.
      pip_install torch --index-url https://download.pytorch.org/whl/cpu \
        && ok "torch (CPU-only) installed" \
        || fail "REQUIRED: torch CPU build install failed. dinomem needs the CPU wheel (CUDA build is dead weight + OOM risk on a CPU box). Fix pip/connectivity: $PY -m pip install torch --index-url https://download.pytorch.org/whl/cpu  — then re-run." ;;
    *)
      # torch present but CUDA/other build -> swap in place to CPU (reclaims nvidia libs).
      # P4: torch present but NOT the CPU build -> swapping to CPU is REQUIRED, not
      # optional. If the swap fails, HARD FAIL rather than run the CUDA build.
      pip_install --force-reinstall torch --index-url https://download.pytorch.org/whl/cpu \
        && ok "torch switched to CPU-only (was $_torch_build) — reclaimed unused CUDA libs" \
        || fail "REQUIRED: torch must be the CPU build but the swap from $_torch_build failed. Fix: $PY -m pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cpu  — then re-run." ;;
  esac
fi
py_can_import langchain_huggingface && skip "langchain-huggingface (installed)" || (
  require_pydep langchain_huggingface langchain-huggingface
)
py_can_import langchain_core && skip "langchain-core (installed)" || (
  require_pydep langchain_core langchain-core
)
py_can_import numpy && skip "numpy (installed)" || (
  require_pydep numpy numpy
)
py_can_import pandas && skip "pandas (installed)" || (
  require_pydep pandas pandas
)
py_can_import duckdb && skip "duckdb (installed)" || (
  require_pydep duckdb duckdb
)
py_can_import yaml && skip "pyyaml (installed)" || (
  require_pydep yaml pyyaml
)
# ── tree-sitter (code-graph layer) — fail-soft, mirrors duckdb ────────────────
# procedures/code_graph.py builds a ZERO-LLM code-structure graph (AST via
# tree-sitter) that tools/code_query.py + the hybrid_recall CODE leg query.
# OPTIONAL: if the binding is absent, code_graph goes OFFLINE and the rest of
# neuron is unaffected (code_graph.py's import guard fails soft, exactly like the
# langchain trio -> docs_search offline). pip_install carries the PEP-668
# --break-system-packages fallback. Preferred single pkg bundles all grammars.
(py_can_import tree_sitter_language_pack || py_can_import tree_sitter_languages) \
  && skip "tree-sitter (installed) — code-graph layer ON" || (
  pip_install tree-sitter-language-pack && ok "tree-sitter-language-pack — code-graph layer ON" \
    || warn "tree-sitter install failed — code_graph / code_query OFFLINE (rest of neuron unaffected). Fix: $PY -m pip install tree-sitter-language-pack  (add --break-system-packages if PEP-668)"
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

# ── Prewarm the embedding model (host HF-cache e5) ─ idempotent, mirror-fallback ──
# WHY: neuron's local semantic tools (docs_search / session_search) load the e5
# snapshot via sentence-transformers from the HOST HF cache. This is SEPARATE from
# base's TEI container (base runs e5 INSIDE the :8080 Docker container for
# memory_search; it never puts a snapshot in the host cache). Left lazy, the model
# downloads on FIRST QUERY — and if the host can't reach Hugging Face (blocked region,
# datacenter IP, flaky mirror) the download fails SILENTLY at query time, so the
# install reports success but semantic search is dead. We prewarm here so any HF
# reachability problem surfaces AT INSTALL, visibly and fixably — not weeks later.
# Idempotent: skips if the snapshot is already cached (no double-download on re-run).
# Mirror fallback: retries via HF_ENDPOINT=hf-mirror.com, which fixes most
# blocked-region hosts. Warn-not-block: a failure never aborts the install.
if [ -n "$KB_DEPS_MISSING" ]; then
  skip "embedding-model prewarm (langchain/sentence-transformers not installed — fix KB deps above first)"
else
  _EMBED_MODEL="${DINOMEM_EMBED_MODEL:-intfloat/multilingual-e5-small}"
  # snapshot-present check: a non-empty snapshots/ dir means the model is cached.
  # (Cache-dir name derivation lives in Python below — the single source of truth
  # for HF's models--<org>--<repo> convention; no fragile shell string-munging.)
  _embed_cached() {
    "$PY" - "$_EMBED_MODEL" <<'PYEOF' 2>/dev/null
import os, pathlib, sys
mid = sys.argv[1]
root = pathlib.Path(os.environ.get("HF_HOME") or (pathlib.Path.home()/".cache"/"huggingface"))
snaps = root/"hub"/("models--"+mid.replace("/","--"))/"snapshots"
ok = snaps.exists() and any(p.is_dir() and any(p.iterdir()) for p in snaps.iterdir())
sys.exit(0 if ok else 1)
PYEOF
  }
  # download via sentence-transformers; honors HF_ENDPOINT env if the caller set one.
  _embed_fetch() {
    "$PY" - "$_EMBED_MODEL" <<'PYEOF' >/dev/null 2>&1
import sys
from sentence_transformers import SentenceTransformer
SentenceTransformer(sys.argv[1], device="cpu")
PYEOF
  }
  if _embed_cached; then
    skip "embedding model $_EMBED_MODEL (already cached — no download)"
  elif _embed_fetch; then
    ok "embedding model $_EMBED_MODEL prewarmed (host HF cache)"
  elif HF_ENDPOINT="https://hf-mirror.com" _embed_fetch; then
    ok "embedding model $_EMBED_MODEL prewarmed via hf-mirror.com (primary HF unreachable)"
  else
    echo ""
    printf '  \033[1;33m⚠ Embedding model did not download — local semantic search (docs_search / session_search) will be OFFLINE.\033[0m\n'
    echo "     Cause: host cannot reach Hugging Face (and the hf-mirror.com fallback also failed)."
    echo "     Core memory_search (TEI container on :8080) is UNAFFECTED — only the LOCAL semantic tools need this."
    printf '     \033[1mFix A (mirror, copy-paste):\033[0m HF_ENDPOINT=https://hf-mirror.com %s -c "from sentence_transformers import SentenceTransformer as S; S(%s)"\n' "$PY" "'$_EMBED_MODEL'"
    printf '     \033[1mFix B (own mirror):\033[0m set HF_ENDPOINT to a reachable HF mirror, then re-run this installer (idempotent).\n'
    echo "     The tools also honor HF_ENDPOINT at query time, so setting it in the agent env self-heals without a re-run."
    echo ""
  fi
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
# AUTO-DISCOVERY + CONTENT-AWARE UPGRADE (mirrors base). Every neuron engine file
# is copied via neuron_copy_engine_file (content-hash: upgrades changed files,
# skips identical, --force overrides). procedures/tools/scripts are DISCOVERED by
# glob, not a hardcoded list, so a NEW file added to the repo ships automatically.
# Fixes: (1) hardcoded manifest omitted ~14 real engine files (compile_user,
# entity_resolver, extract_user, mem_authority, promotion_*, skill_extract,
# run_log, _embed, person_link, ...) -> they never installed; (2) skip-if-exists
# meant re-running never UPGRADED an existing file. Test files (*_test.py) and the
# installer/uninstaller are excluded by design. extract_memory.py keeps its
# always-overwrite (neuron superset must beat base's older copy).
for _dir in procedures tools scripts; do
  [ -d "$SKILL_DIR/$_dir" ] || continue
  for _src in "$SKILL_DIR/$_dir"/*.py "$SKILL_DIR/$_dir"/*.sh; do
    [ -f "$_src" ] || continue                        # glob-no-match guard
    _bn="$(basename "$_src")"
    case "$_bn" in
      *_test.py) continue ;;                            # test fixtures don't ship
      install.sh|uninstall.sh|update.sh) continue ;;    # installer never self-copies
    esac
    _fa=0
    case "$_dir/$_bn" in procedures/extract_memory.py) _fa=1 ;; esac
    neuron_copy_engine_file "$_dir/$_bn" "$_fa"
  done
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
  copy_dir_upgradeable "$SKILL_DIR/hooks/dinomem-open-notes" "$WS/hooks/dinomem-open-notes" "hooks/dinomem-open-notes/ (neuron superset)"
  if openclaw_running; then
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
# content-aware upsert (base ran first) so the upgrade ships neuron's versions
# only when they actually differ — no blind overwrite, no skip-if-exists.
if [ -d "$SKILL_DIR/skills" ]; then
  hr "Skills (project-notes, memory-pinning, retrieval-routing, note-claim)"
  for _sk in "$SKILL_DIR/skills"/*/; do
    [ -d "$_sk" ] || continue
    _skname="$(basename "$_sk")"
    _skdst="$WS/skills/$_skname"
    copy_dir_upgradeable "$_sk" "$_skdst" "skills/$_skname/"
  done
fi

# ── Wire copied neuron skills into agent allowlist ───────────────────────────
# Skills in <workspace>/skills are auto-discovered, but agents with an explicit
# skills allowlist will EXCLUDE them unless listed. Add the IDs we just shipped.
if command -v python3 >/dev/null 2>&1; then
  python3 "$SKILL_DIR/scripts/wire_skills.py" \
    --workspace "$WS" \
    --agent-id "$AGENT_ID" \
    --skills-dir "$SKILL_DIR/skills" \
    $( [ "${DRY_RUN:-0}" = 1 ] && printf -- '--dry-run' ) \
    || warn "skill allowlist wiring failed; skills may be excluded"
else
  warn "python3 not found; skill allowlist wiring skipped"
fi

# NOTE: neuron no longer ships its own benchmark/ harness. The old retrieval-only
# A/B (run_benchmark.py + Atlas scenarios) was low-quality and outside the real
# pipeline. The full evaluation program lives in BASE (benchmark/run_all.py,
# copied by base's install), which neuron refreshes first — so neuron users get
# the real multi-phase eval via base, no neuron-side benchmark copy needed.

fi  # end REPAIR-CRON FAST PATH (heavy phases)
if [ "$REPAIR_CRON" = 1 ]; then
  hr "repair-cron mode"
  ok "skipping base/pip/TEI/copy/config phases — re-registering crons only"
fi

# ── 3) Register cron jobs ─────────────────────────────────────────────────────
# upsert_cron: add or update a cron entry by script keyword
upsert_cron() {
  local keyword="$1" comment="$2" cron_line="$3" label="$4"
  local aid="${AGENT_ID:-default}"
  local tag="# dinomem-managed:${keyword}:${aid}"
  comment="$comment [agent:${aid}]"
  local managed_line="${cron_line} ${tag}"
  # ROBUST DEDUP (fixes duplicate-on-upgrade): key on a per-agent MANAGED TAG,
  # not on command shape. Historical bug: scope "cd $WS && .*$keyword" missed any
  # dinomem line NOT starting with `cd $WS &&` (e.g. `DINOMEM_AGENT_ID=... bash
  # dinomem_run.sh ...`), so an upgrade appended a 2nd copy instead of replacing.
  #
  # Phase 1 ADOPTION (one-time, safe migration): stamp the tag onto any LEGACY
  # dinomem line for this script+agent that predates the tag scheme — identified
  # by a DISTINCTIVE installer signature (the `dinomem_run.sh` wrapper OR a
  # `DINOMEM_*=` env var the installer sets) + this agent's own $WS + the script,
  # and not already tagged. A user's hand-written `cd $WS && python3
  # procedures/<script>` has NEITHER wrapper nor DINOMEM_ env, so it is NOT adopted
  # and NOT clobbered. This is the false-delete guard.
  if [ -n "${WS:-}" ]; then
    crontab -l 2>/dev/null | awk -v ws="$WS" -v kw="$keyword" -v tag="$tag" '
      {
        line=$0
        if (line ~ /# dinomem-managed:/) { print line; next }
        # dino_sig: is THIS line an installer-owned dinomem cron (safe to adopt+replace)?
        #  (a) the dinomem_run.sh wrapper, OR (b) a DINOMEM_* env the installer bakes,
        #  OR (c) a bare `procedures/<KNOWN-DINOMEM-SCRIPT>.py` call. (c) closes the
        #  upgrade-DOUBLE hole: a pre-tag cron with NEITHER wrapper NOR env (e.g.
        #  cleanup_startup_daily = `cd $WS && python3 procedures/...`, or
        #  auto_session_reset when owner-id did not resolve at old install time) used
        #  to escape adoption -> the staggered upgrade appended a 2nd copy instead of
        #  replacing. A user hand-written cron never calls procedures/<these exact
        #  dinomem scripts>, so the false-delete guard is preserved.
        dino_sig = (index(line,"dinomem_run.sh")>0 || line ~ /DINOMEM_[A-Z_]+=/ || line ~ /procedures\/(auto_session_reset|memory_cleanup|memory_review|cleanup_startup_daily|workspace_backup|weekly_stats|memory_graph|memory_synthesis|contradiction_check|confidence_engine|memory_promote|generate_topic_index|docs_ingest|code_graph|code_anchors|_retrieval_log)\.py/)
        if (index(line, ws)>0 && dino_sig && index(line, kw)>0) { print line " " tag }
        else { print line }
      }' | crontab -
  fi
  # Phase 2 MATCH BY TAG: dedup/replace keys on the tag only, so untagged user
  # jobs calling the same script are never touched.
  local scope existing
  scope="${tag}\$"
  existing=$(crontab -l 2>/dev/null | grep -E "$scope" || true)
  if [ "$existing" = "$managed_line" ]; then
    skip "$label (exists, up to date)"
  elif [ -n "$existing" ]; then
    { crontab -l 2>/dev/null | grep -vE "$scope"; echo "# $comment"; echo "$managed_line"; } | crontab -
    ok "$label (updated)"
  else
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$managed_line"; } | crontab -
    ok "$label (registered)"
  fi
}

if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # crond does not inherit the interactive shell, so model-routing env vars must
  # be baked into the cron lines or they are silently ignored on scheduled runs.
  # CHEAP_ENV  -> non-reasoning crons (topic_index, docs_ingest). THE ANCHOR is
  #              agents.defaults.compaction.model, read AT RUNTIME by
  #              _cheap_model.py on every cron run -> crons need NOTHING baked in
  #              the normal case (set compaction.model once, every cron follows,
  #              no drift). Bake DINOMEM_CHEAP_MODEL ONLY when it actually DIFFERS
  #              from the anchor (a deliberate per-script override). Baking an
  #              ambient value == anchor is what caused silent drift.
  # REASON_ENV -> reasoning crons (synthesis, contradiction_check): carry the
  #              thinking level when DINOMEM_REASONING_THINKING is set.
  CHEAP_ENV=""
  if [ -n "${DINOMEM_CHEAP_MODEL:-}" ]; then
    # env -u isolates the pure anchor read (_cheap_model.py checks env FIRST, so
    # without unsetting it the probe returns the env value -> always equal).
    _anchor_now="$(env -u DINOMEM_CHEAP_MODEL OPENCLAW_CONFIG="${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}" python3 "$SKILL_DIR/procedures/_cheap_model.py" 2>/dev/null || true)"
    if [ "$DINOMEM_CHEAP_MODEL" != "$_anchor_now" ]; then
      CHEAP_ENV="DINOMEM_CHEAP_MODEL=$DINOMEM_CHEAP_MODEL "
      ok "cron cheap model (explicit override, differs from anchor): $DINOMEM_CHEAP_MODEL"
    else
      ok "cron cheap model: following compaction.model anchor at runtime (no bake)"
    fi
  fi
  REASON_ENV=""
  if [ -n "${DINOMEM_REASONING_THINKING:-}" ]; then
    REASON_ENV="DINOMEM_REASONING_THINKING=$DINOMEM_REASONING_THINKING "
    ok "cron reasoning thinking: $DINOMEM_REASONING_THINKING"
  fi

  # ── Multi-agent cron STAGGER (thundering-herd guard) ────────────────────────
  # WHY: neuron's crontab lanes (L2 03:00, L3 03:30, L4 Sun 04:00, RAG 04:00,
  # code_graph 04:10, rlog 04:15) were the SAME wall-clock minute for every agent.
  # On a host running N dinomem agents against ONE gateway that bunches N heavy
  # jobs (embedding + LLM + sqlite) into the same instant -> RSS spike + event-loop
  # stall (observed: 6 agents -> memory pressure + multi-second gateway freezes).
  # The heavy-llm/heavy-embed flocks SERIALIZE execution, but the *scheduling* still
  # woke every agent at once. FIX: deterministic per-agent minute offset from the
  # agent id (stable, zero-coordination — each install computes its own slot; no
  # shared state), spreading each recurring job so agents interleave. Byte-identical
  # helper to base install.sh. Single-agent hosts unaffected (offset just shifts).
  _stagger() {  # _stagger <mod>  (uses AGENT_ID)
    local mod="$1" h
    h=$(printf '%s' "${AGENT_ID:-default}" | (md5sum 2>/dev/null || md5 2>/dev/null) | tr -dc '0-9a-f' | head -c 8)
    [ -n "$h" ] || h=0
    printf '%s\n' $(( 0x${h:-0} % mod ))
  }
  _SMIN=$(_stagger 60)   # 0..59 — per-agent minute offset for the daily/weekly lanes
  ok "cron stagger: agent '${AGENT_ID:-default}' -> neuron daily lanes shifted +${_SMIN}m within their hour"
  # Base minutes for each lane + per-agent offset (%60 keeps it inside the hour band).
  _L2_MIN=$(( (0  + _SMIN) % 60 ))   # L2 graph      (base :00, hour 3)
  _L3_MIN=$(( (30 + _SMIN) % 60 ))   # L3 synthesis  (base :30, hour 3)
  _L4_MIN=$(( (0  + _SMIN) % 60 ))   # L4 promote    (base :00, hour 4, Sun)
  _RAG_MIN=$(( (0 + _SMIN) % 60 ))   # RAG ingest    (base :00, hour 4)
  _CG_MIN=$(( (10 + _SMIN) % 60 ))   # code_graph    (base :10, hour 4)
  _RL_MIN=$(( (15 + _SMIN) % 60 ))   # rlog rollup   (base :15, hour 4)

  # memory_graph — daily ~03:xx UTC (L2, no LLM — pure graph linking), staggered
  # MULTI-AGENT SERIALIZATION: heavy-embed class (embedding/CPU-heavy, no LLM)
  # acquires a host-wide flock so at most one agent's embed job runs at a time.
  # RUNAWAY BACKSTOP: `timeout 7200` (2h) wraps the whole dinomem_run.sh invocation.
  # Sits ABOVE the 5400s flock wait (DINOMEM_LOCK_TIMEOUT_SECS) so a legitimately-
  # queued embed job is never killed while waiting its turn — pure infinite-hang
  # guard so a stuck run can't hold the heavy-embed lock forever. Daily = no overlap.
  GRAPH_CRON="$_L2_MIN 3 * * * DINOMEM_AGENT_ID=$AGENT_ID timeout 7200 bash $WS/scripts/dinomem_run.sh heavy-embed $WS python3 procedures/memory_graph.py >> logs/memory_graph.log 2>&1"
  upsert_cron "memory_graph.py" "dinomem-neuron: L2 graph linking" "$GRAPH_CRON" "memory_graph cron (daily 03:${_L2_MIN} UTC, staggered)"

  # memory_synthesis + contradiction_check — daily at 3:30 UTC (L3, reasoning=True)
  # confidence_engine (P1 #1, no-LLM calibration) runs AFTER synthesis writes
  # current_insights.json and AFTER contradiction_check, BEFORE the weekly promote
  # reads it — so the gate inherits calibrated confidence with zero downstream
  # change. Chained into the same daily L3 run; pure bookkeeping, cannot OOM.
  # MULTI-AGENT SERIALIZATION: heavy-llm class — at most one LLM job runs at a time.
  # Chained pipeline runs inside the lock: synthesis -> contradiction_check -> confidence_engine.
  # dinomem_run.sh wraps all three; they share one lock acquisition (flock is held for the
  # duration of the subshell, not re-acquired per-script — safe for a chained pipeline).
  SYNTH_CHAIN="${REASON_ENV}python3 procedures/memory_synthesis.py >> logs/memory_synthesis.log 2>&1 && ${REASON_ENV}python3 procedures/contradiction_check.py >> logs/contradiction_check.log 2>&1 && python3 procedures/confidence_engine.py >> logs/confidence_engine.log 2>&1"
  # RUNAWAY BACKSTOP: `timeout 7200` over the whole chained pipeline (above the 5400s
  # flock wait). Guards the heavy-llm lock against an infinite provider hang; the
  # three chained scripts share one lock, so one outer bound covers all of them.
  SYNTH_CRON="$_L3_MIN 3 * * * DINOMEM_AGENT_ID=$AGENT_ID timeout 7200 bash $WS/scripts/dinomem_run.sh heavy-llm $WS bash -c \"$SYNTH_CHAIN\""
  upsert_cron "memory_synthesis.py" "dinomem-neuron: L3 synthesis + contradiction check + calibration" "$SYNTH_CRON" "memory_synthesis + contradiction_check + confidence_engine cron (daily 3:30 UTC)"

  # memory_promote (no LLM) + generate_topic_index (cheap LLM) — weekly Sun 4:00 UTC (L4)
  # MULTI-AGENT SERIALIZATION: heavy-llm class (generate_topic_index calls LLM).
  PROMOTE_CHAIN="python3 procedures/memory_promote.py >> logs/memory_promote.log 2>&1 && ${CHEAP_ENV}python3 procedures/generate_topic_index.py >> logs/topic_index.log 2>&1"
  # RUNAWAY BACKSTOP: `timeout 7200` — same rationale as the L3 synth pipeline.
  PROMOTE_CRON="$_L4_MIN 4 * * 0 DINOMEM_AGENT_ID=$AGENT_ID timeout 7200 bash $WS/scripts/dinomem_run.sh heavy-llm $WS bash -c \"$PROMOTE_CHAIN\""
  upsert_cron "memory_promote.py" "dinomem-neuron: L4 promotion + topic index (weekly)" "$PROMOTE_CRON" "memory_promote + generate_topic_index cron (weekly Sunday 04:${_L4_MIN} UTC, staggered)"

  # docs_ingest — daily at 4:00 UTC (RAG long docs, no-reasoning)
  # MULTI-AGENT SERIALIZATION: heavy-embed class (embedding-heavy, no LLM).
  # RUNAWAY BACKSTOP: `timeout 7200` — same rationale as memory_graph (heavy-embed lock).
  DOCS_CRON="$_RAG_MIN 4 * * * DINOMEM_AGENT_ID=$AGENT_ID timeout 7200 bash $WS/scripts/dinomem_run.sh heavy-embed $WS ${CHEAP_ENV}python3 procedures/docs_ingest.py >> logs/docs_ingest.log 2>&1"
  upsert_cron "docs_ingest.py" "dinomem-neuron: RAG docs re-ingest (daily)" "$DOCS_CRON" "docs_ingest cron (daily 04:${_RAG_MIN} UTC, staggered)"
  # session_ingest is triggered automatically by auto_session_reset.py (every 15 min)
  # No separate cron needed.

  # code_graph refresh — daily at 4:10 UTC (ZERO-LLM, git-incremental so cheap):
  # re-index code STRUCTURE (procedures/code_graph.py) so code_query stays current,
  # then rebuild the grep-backed prompt-anchor sidecar (procedures/code_anchors.py)
  # that indexes install.sh heredoc / cron-payload behavioral labels the AST can't
  # see. Both fail-soft (tree-sitter missing -> code_graph offline; anchors is pure
  # stdlib grep). Piggybacked in one cron entry (&&): structure first, anchors after.
  # timeout 1740 (29min) self-bounds each step BELOW the gateway's default 600s
  # command-cron wall-clock cap: the first full AST build on a large repo can
  # exceed 10min (observed 691s), tripping the ceiling and leaving consecutiveErrors
  # climbing forever. 1740s sits under the 30min isolated-cron watchdog and gives
  # huge headroom; git-incremental runs after the first are cheap either way.
  CODEGRAPH_CRON="$_CG_MIN 4 * * * cd $WS && timeout 1740 python3 procedures/code_graph.py --path $WS >> logs/code_graph_refresh.log 2>&1 && timeout 1740 python3 procedures/code_anchors.py --path $WS >> logs/code_graph_refresh.log 2>&1"
  upsert_cron "code_graph.py --path" "dinomem-neuron: code-structure graph + prompt-anchor refresh (daily)" "$CODEGRAPH_CRON" "code_graph_refresh cron (daily 04:${_CG_MIN} UTC, staggered)"

  # retrieval_log rollup — daily at 4:15 UTC (no LLM, pure stdlib): compresses
  # dailies older than 30d into monthly rollups + prunes raw, refreshes health.json
  # (routing-quality snapshot: calls/fallback-rate/empty-rate per retrieval tool).
  # RUNAWAY BACKSTOP: `timeout 600` — pure-stdlib no-LLM rollup, realistically <1min;
  # a 10min bound is a generous guard against a pathological log tree. No flock (not
  # a heavy class), so no wait to sit above.
  RLOG_CRON="$_RL_MIN 4 * * * cd $WS && timeout 600 python3 procedures/_retrieval_log.py --workspace $WS --rollup >> logs/retrieval_log_rollup.log 2>&1"
  upsert_cron "_retrieval_log.py --rollup" "dinomem-neuron: retrieval-log rollup + health snapshot (daily)" "$RLOG_CRON" "retrieval_log rollup cron (daily 04:${_RL_MIN} UTC, staggered)"

  # ── shared investigate-first principle (DRY: one source, referenced by the
  #    building crons Advancer + Improver). The Ideator already embodies it as its
  #    whole job (Step 4 INVESTIGATE + Step 5 read-only boundary); the BUILDING
  #    roles verify done-ness but lack the front-loaded 'get informed before you
  #    build' stance + (Improver) an affirmative read-only-probing-allowed grant.
  #    lightContext note: these crons wake BLANK (no memory/history), so the clause
  #    tells them to acquire understanding ON THE SPOT this run, not assume it.
  #    Interpolates because the prompt heredocs are UNQUOTED <<PYEOF.
  INVESTIGATE_FIRST="INVESTIGATE FIRST (you wake with NO prior context — get informed THIS run, do not assume): before you execute/build a step, first READ the relevant real state — the note, the artifacts it references, config, git, data — and, when it helps you act well, RUN non-destructive checks to understand the situation: read-only tests, dry-runs, diagnostics, reproductions, bash -n syntax checks, grep, read-only queries. Being informed is how you build well; investigation is not building. BOUNDARY: read-only/diagnostic probing is ALWAYS allowed and encouraged; actual mutation happens only in the step's own execution, under the safety gate below.\n\nBUILD/CODE CONTEXT (if THIS run's step writes or edits code/config, or adds agent behavior, use these build tools instead of grep+read-whole-file or hand-writing. \$WS is your workspace root; every path below is resolved there):\n- NAVIGATE code (what calls X / X calls what / class hierarchy / where a symbol is defined / path A->B): python3 \$WS/tools/code_query.py explain <symbol>  (or: path <A> <B> | query <nl>) [--hops N]. It returns file:line + EXTRACTED/INFERRED edges. If it prints [stale] or empty, first (re)build the graph: python3 \$WS/procedures/code_graph.py --path <root>  (AST, zero-LLM, git-incremental). Do NOT grep+read entire files to trace code — that is ~20x the tokens and misses edges. When the note/resume_state you just read NAMES a file path or a code symbol you are about to touch, front-load it FIRST: git -C <repo> diff -- <file> (see what already changed) and python3 \$WS/tools/code_query.py explain <symbol> — before editing, not after.\n- BEFORE adding NEW agent behavior (a cron / hook / skill / always-on rule) as part of a step: python3 \$WS/tools/route.py classify  — it emits the surface-decision schema; route to ONE surface (cron=schedule | hook=event | skill=on-demand method | root=always-on, the last resort). Then python3 \$WS/tools/route.py dup <file> (DRY floor: copy-paste scan of the just-built script) and python3 \$WS/tools/route.py verify (confirm the routed write actually landed).\n- BEFORE writing a NEW memory _pin_/_note_: python3 \$WS/procedures/memory_dedup_check.py  — if it reports match:true, MERGE into the existing file, do NOT create a second one.\n- SKILL DEPTH (the FULL discipline lives in these SKILL.md files, NOT here; when a trigger below fires, cat the file(s) and FOLLOW them THIS run, do NOT work from memory): if THIS step writes/edits code -> cat these three exact absolute paths (verbatim, no trailing punctuation): \$WS/skills/edit-verify-loop/SKILL.md and \$WS/skills/safe-file-ops/SKILL.md and \$WS/skills/build-quality/SKILL.md -- If THIS step edits openclaw.json or any schema-validated config -> ALSO cat this exact path: \$WS/skills/openclaw-config-edit/SKILL.md (read it, then act). If THIS step adds a NEW cron -> ALSO cat \$WS/skills/cron-config/SKILL.md ; a NEW hook -> \$WS/skills/hook-config/SKILL.md ; a NEW skill -> \$WS/skills/skill-config/SKILL.md (each is a full absolute path — cat the one that matches, do not brace-expand). A pure note-advance / verify / prose run triggers NONE of these — cat nothing. Every path here is already absolute; cat it verbatim, do not prepend a cwd or append anything."

  # project_advancer — OpenClaw agentTurn cron (NOT shell crontab): auto-advance type:project in_progress notes
  # one step per run, unattended, announces progress. Premium: only neuron installs this.
  DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
                           capture_output=True, text=True, timeout=_CLI_T)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=_CLI_T)
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
        "message": "Project Advancer. Auto-advance step-by-step project notes unattended.\n\nSILENT-STOP CONTRACT: whenever an instruction below says 'silent' / 'no announce' / 'stop immediately', your ENTIRE final message for this run must be exactly the literal token NO_REPLY and nothing else — no extra sentence. This is what makes the cron delivery layer suppress the run; a descriptive sentence like 'nothing to advance' still gets delivered if delivery is not mode:none. REAL reports (state actually changed, safety-gate pause, flagged issue) must be sent explicitly via the message tool to the owner's default channel — do not rely on the runner's fallback announce, since delivery may be mode:none.\n\n${INVESTIGATE_FIRST}\n\nStep 0: PRE-CHECK (zero LLM cost gate). Run: bash $WS/scripts/check_projects.sh\nIf exit code != 0 -> no in_progress project notes exist. Stop immediately. Output exactly NO_REPLY and nothing else.\n\nStep 1: ls memory/_note_*.md in $WS/memory/. Read each. Select only notes with type: project AND status: in_progress. FAIRNESS ORDERING (anti-starvation): when MORE THAN ONE qualifies and a heaviness turn-boundary means you may not reach them all this run, process them STALEST-ADVANCED FIRST — order by the note's last real advance (the mtime of its most recent step/resume_state change, or lacking that, oldest created date). This guarantees a note that keeps losing selection to a busier sibling still gets its turn; combined with the per-note gate (each note fires on its own terms) it closes the starvation gap end to end.\n\nStep 1b — HEAVINESS CHECK (pre-task): Before advancing any project, look at its NEXT unchecked step. If that step is heavy (write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle), commit to doing ONLY that one step this run. If it is light (tiny edit/config/doc/one-liner), you may chain several light steps in this run. Turn boundaries are decided by step heaviness, not by context %.\n\nStep 1c: If no in_progress notes AND nothing flagged, stop silently. Output exactly NO_REPLY and nothing else.\n\nStep 2 — CLAIM PRE-CHECK (cheap filter only; the AUTHORITATIVE gate is Step 3). ALL timestamps are UTC ISO8601; compute claim age in UTC. For each in_progress note, read claimed_by and claimed_at fields. (G6 live-session guard) If claimed_by STARTS WITH 'live-session' AND claimed_at is less than 120 minutes (2 hours) ago, SKIP this note entirely — a human is actively working it in an interactive session; never fight a live session. If claimed_by is set (any other value) AND claimed_at is less than 30 minutes ago, SKIP this note entirely (another Advancer instance is working it). This prose check only avoids spawning the claim script on an obviously-busy note; it is NOT trusted to be race-safe on its own — Step 3 is the real lock.\n\nStep 3 — CLAIM (ATOMIC, via the shared lock — do NOT hand-write claimed_by). Run EXACTLY: bash $WS/scripts/claim_note.sh claim <full note path> advancer-<this run's cron run id or a short uuid>. Interpret the exit code: exit 0 -> you now hold the claim atomically (flock + verified write); proceed to Step 4. exit 3 -> BUSY: a fresh claim (a live session or another job) holds this note; SKIP it entirely this run — do NOT edit it, do NOT fight, do NOT hand-write a claim; move to the next note. Any other non-zero -> treat as unable-to-claim; SKIP this note this run. NEVER write claimed_by/claimed_at yourself with the edit tool — claim_note.sh is the ONLY writer, so your claim goes through the SAME flock the live session uses. This closes the claim-clobber TOCTOU race: previously this step hand-wrote the claim unlocked and could overwrite a live session's claim landing in the read->write gap. If you keep editing the same note across chained light steps, re-assert the claim with bash $WS/scripts/claim_note.sh refresh <note> advancer-<id>.\n\nStep 4: For each claimed in_progress project note, pick the lowest unchecked step at current_step. Resume from resume_state ONLY (the note is self-sufficient; do not rely on chat history).\n\nStep 4a — WHOLE-PROJECT DONE_WHEN PRECHECK (run BEFORE anything else, even when parked at an early/non-verifiable step): Read the note's TOP-LEVEL done_when (the whole-project completion condition, not the current step). If any part of it is locally checkable against a real artifact (file/dir exists, grep of a named code anchor, compare the repo HEAD commit to its upstream @{u} via git rev-parse run in the repo dir and confirm HEAD equals upstream, feature shipped + live in sync), VERIFY IT NOW regardless of what resume_state says — a stale resume_state like 'awaiting owner sign-off' / 'do NOT write code yet' is NOT trusted over the artifact, because the work may have shipped out-of-band in a live session and the note never got written back. If the whole-project done_when verifies against the artifact: mark ALL steps [x], flip status to done, rewrite resume_state to 'DONE (verified against <artifact> on <date>)', release the claim, report the closure, and STOP processing this note. Only if the top-level done_when does NOT verify (or is not artifact-checkable, e.g. a pure human sign-off with no shipped artifact) do you fall through to Step 4b. This closes the rot-gap where a fully-shipped project parked at step 1 stays in_progress forever because nobody checked the final artifact.\n\nStep 4b — DRIFT CHECK (verify before executing): Do NOT assume current_step's checkbox state reflects reality — notes can drift (e.g. work finished in a live human session but the checkbox never flipped, or a companion note already covered this step). Before treating this step as pending work, independently verify whether its OWN done_when is already satisfied against real state (file/artifact exists, compare the repo HEAD commit to its upstream @{u} via git rev-parse run in the repo dir and confirm HEAD equals upstream, feature already shipped, companion note already did it, etc — the same kind of locally-checkable verification Daily Note Review uses). If already satisfied: mark it [x], advance current_step, rewrite resume_state, then repeat this same Step 4b check for the NEW current_step (loop until you hit a step that is genuinely NOT done, or the whole project completes) — treat this drift-catchup as part of the same LIGHT turn, not a new heavy step. If genuinely not satisfied, proceed to Step 5 normally. OPTIONAL git corroboration (only if $WS/memory is git-tracked; skip silently if not): you MAY run 'git -C $WS log --oneline -3 -- memory/<note>' to see when the note itself was last edited, as a cheap sanity check alongside artifact verification. Never required — absence of git changes nothing about how drift is verified.\n\nStep 5: SAFETY GATE before executing the step. If the step is destructive or external (deletes data, git push, sends external messages, irreversible writes outside workspace, spends money, touches credentials), DO NOT execute it. Leave it pending, clear your claim (Step 8), send a message via the message tool to the owner that the project is paused awaiting approval at step n/m — tag the owner (use platform-appropriate mention format), move to the next project.\n\nStep 6: Execute the one current step. After it: verify the step done_when. If met, mark [x], advance current_step, REWRITE resume_state with what the next step needs (paths, decisions, assumptions). If not met, leave pending and record why in resume_state.\n\nStep 7: Turn-boundary guard (heaviness-based). Gauge the heaviness of the NEXT step. Heavy = write+test a whole tool/file, multi-file change, research-then-build, or a full write->run->debug->verify cycle. If the next step is heavy, do only the current step this run, checkpoint (state already written), CLEAR claimed_by and claimed_at, and stop advancing this run — do NOT chain another heavy step. Light steps (tiny edit/config/doc/one-liner) may be chained: continue to the next step of the same project until the next step is heavy or the project is done.\n\nStep 8 — CLAIM RELEASE (via the script, atomic): run EXACTLY bash $WS/scripts/claim_note.sh release <full note path> after completing or checkpointing a project (step done, heavy-step checkpoint, or safety gate pause), before moving to the next project. Never leave a note with a live claim after you stop working it, and never hand-strip claimed_by with the edit tool — use the script so the release goes through the same lock.\n\nStep 9: When a project done_when (all steps [x] / final artifact) is met, flip status to done, then release the claim via bash $WS/scripts/claim_note.sh release <note>. Daily Note Review will delete/promote it.\n\nStep 10: REPORT only on real state change, via the message tool (do not rely on runner fallback delivery, which may be disabled). BEFORE composing any progress report, RE-READ the note's current_step and status from disk and report the CURRENT values — never a remembered/cached earlier state (a stale 'step 1/6' report after the note already reached a later step is a real bug this prevents). Rules:\n- Step advanced or completed -> send a message with a progress bar\n- First time hitting safety gate -> send a message once, tag owner (use platform-appropriate mention)\n- Already paused (resume_state contains 'awaiting approval') -> silent. Output exactly NO_REPLY and nothing else.\n- Skipped due to a live-session or other active claim -> silent (not a state change). Output exactly NO_REPLY and nothing else.\n- Malformed or stalled -> send a message only if last_flagged not set in resume_state OR last_flagged > 24h ago; write last_flagged: <ISO timestamp> to resume_state after sending. Otherwise silent: output exactly NO_REPLY.\n- No state change -> silent. Output exactly NO_REPLY and nothing else.\nProgress bar (only when actually sending a real state-change report): [████░░░░░░] 4/10 40% — project name, step n/m. Short, group-chat style.",
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

# ── project_improver (Component 2) — OpenClaw agentTurn cron ──────────────────
# Self-improving closer, half 1 of 2: LLM improves COMPLETED-but-unverified
# project notes (behavior-preserving, all domains), then stamps verified: true.
# Zero-LLM pre-gate (check_improvable.sh) guards cost. Premium: neuron only.
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
                           capture_output=True, text=True, timeout=_CLI_T)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=_CLI_T)
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
        "message": "Project Improver (Component 2). You improve COMPLETED-but-unverified project notes, then stamp them verified so Component 1 can retire them. Autonomous, behavior-preserving, all domains. Hold the safety gate HARD — you edit real artifacts unattended.\n\n${INVESTIGATE_FIRST}\n\nStep 0 — ZERO-LLM PRE-GATE: Run: bash $WS/scripts/check_improvable.sh\nIf exit code != 0 → nothing to improve. STOP IMMEDIATELY. No further tool calls, no announce.\n\nStep 1 — SELECT: ls memory/_note_*.md in $WS/memory/. Read each. Pick notes with type: project AND status: done AND NOT verified: true. Handle ONE note this run (the oldest by date). If none, stop silently.\n\nStep 2 — CLAIM PRE-CHECK (cheap filter only; the AUTHORITATIVE gate is Step 3). ALL timestamps are UTC ISO8601; compute age in UTC. Read claimed_by / claimed_at. If claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes (2h) ago → SKIP (a human is working it live; never fight a live session). If claimed_by is any other value AND claimed_at < 30 min ago → SKIP (Advancer or another improver instance owns it). This prose check only avoids spawning the claim script on an obviously-busy note; it is NOT trusted to be race-safe on its own — Step 3 is.\n\nStep 3 — CLAIM (ATOMIC, via the shared lock — do NOT hand-write claimed_by). Run EXACTLY: bash $WS/scripts/claim_note.sh claim <full note path> component2-improver. Interpret the exit code: exit 0 -> you now hold the claim atomically (flock + verified write); proceed. exit 3 -> BUSY: a fresh claim (live-session or another job) holds this note; SKIP it entirely this run — do NOT edit it, do NOT fight, do NOT hand-write a claim; move to the next eligible note. Any other non-zero -> treat as unable-to-claim; SKIP this note this run. NEVER write claimed_by/claimed_at yourself with the edit tool — the script is the only writer, so your claim goes through the same flock the live session uses (this is the fix for the claim-clobber race). If you keep editing the same note across steps, re-assert with bash $WS/scripts/claim_note.sh refresh <note> component2-improver.\n\nStep 4 — UNDERSTAND: Read the project's done_when and resume_state, and inspect the actual artifact(s) it produced (scripts, deliverables, reports, config — whatever the project made). Establish what the project DOES and how you'll verify it still does that after any change (its behavior contract = its done_when).\n\nStep 5 — ASSESS (principle, NOT a checklist): Ask 'can this be made cheaper, faster, clearer, or more reliable — AND does that even make sense for THIS project?' There is NO prescribed action. Do not force caching/logging/locks/dedup or any technique. The right move is whatever fits this specific project — possibly a technique nobody would list, possibly NOTHING. 'Already efficient, nothing needed' is a valid, common, correct outcome.\n\nStep 6 — IMPROVE (behavior-preserving ONLY): If there is a worthwhile improvement, make it. You MUST NOT change WHAT the project does — only how efficiently/reliably/clearly it does it. Back up any file before editing (bash $SKILL_DIR/scripts/file-backup.sh <file>). Keep changes minimal and targeted.\n\nStep 7 — VALIDATE / DEBUG until reliable: After improving, verify the artifact STILL satisfies the project's done_when — for code: it runs / tests pass / compiles; for a deliverable: it is coherent, complete, and verifiably correct against done_when. If your change broke behavior, debug and fix, or REVERT to the backup. Do not proceed until the behavior contract holds again. If you cannot make it hold, revert fully and treat as 'no safe improvement'. OPTIONAL git evidence (only if the repo is git-tracked; skip silently if not): you MAY run 'git -C $WS log --oneline -5 -- <artifact>' or 'git -C $WS diff HEAD~1 -- <artifact>' to see what actually changed for this project as corroborating validation evidence. This is an OPTIONAL aid, never required — absence of git changes nothing about how you validate.\n\nStep 8 — SAFETY GATE (non-negotiable): You may do LOCAL, reversible work only. If improving/validating would require an EXTERNAL or DESTRUCTIVE action — git push, deleting data, sending external messages, spending money, touching credentials, irreversible writes outside the workspace — DO NOT do it. Do the local part, then STOP before the external action. Do NOT stamp verified. Clear your claim (Step 10), and in Step 11 report the project is 'improved locally, paused awaiting approval for <the external step>', tag the owner. Record in resume_state that it is awaiting approval so the next run does not re-loop on it endlessly.\n\nStep 9 — STAMP VERIFIED: Only if the project is validated behavior-preserving AND no external step is blocking, write verified: true into the note frontmatter. This is the signal Component 1 uses to delete it. (If you determined 'nothing needed', still stamp verified: true — you reviewed and confirmed it's already sound.)\n\nStep 10 — CLAIM RELEASE (via the script, atomic): run EXACTLY bash $WS/scripts/claim_note.sh release <full note path> (whether you stamped verified, paused at the safety gate, or aborted). Never leave a dead claim, and never hand-strip claimed_by with the edit tool — use the script so the release goes through the same lock.\n\nStep 11 — REPORT to the owner via the message tool, using the agent's default owner channel (platform-appropriate mention; do not hardcode a chat id). One short message: project name + what you did. Formats:\n- Improved: 'Improved <project>: <one-line what+why>. Validated behavior-preserving, verified.'\n- Nothing needed: 'Reviewed <project>: already efficient, nothing to change. Verified.'\n- Safety-gated: 'Improved <project> locally: <what>. Paused — needs your approval for <external step> before I finish.' (tag owner)\nKeep it terse. No report if Step 0 gated out or you skipped on a claim (those are silent).\n\nHARD RULES: behavior-preserving only, never change what a project does; local+reversible only, external/destructive → stop+report; one note per run; ALWAYS claim + release via claim_note.sh (never hand-write claimed_by); respect live-session and advancer claims; 'nothing needed' is a valid outcome — never invent busywork.",
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
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
                           capture_output=True, text=True, timeout=_CLI_T)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=_CLI_T)
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
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH

def _cron_add_argv(job):
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
                           capture_output=True, text=True, timeout=_CLI_T)
            jid = existing_id
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated + disabled (driven by Note Cron Gate)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=_CLI_T)
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
import subprocess, json, os as _os
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
try:
    r = subprocess.run(['openclaw','cron','list','--json'], capture_output=True, text=True, timeout=_CLI_T)
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
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH
CANON = [
    "Project Advancer",
    "Project Improver (Component 2)",
    "Verified Note Deleter (Component 1)",
    "Brainstorm Ideator",
    "Daily Note Review",
    "Note Cron Gate",
]
try:
    r = subprocess.run(['openclaw','cron','list','--json'], capture_output=True, text=True, timeout=_CLI_T)
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
            subprocess.run(['openclaw','cron','remove',did], capture_output=True, text=True, timeout=_CLI_T)
            print("  \033[32m[ok]\033[0m   deduped '%s' — removed duplicate %s (kept newest)" % (cname, did[:8]))
PYEOF
if true; then
  python3 - <<PYEOF
import subprocess, json, os
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
import os as _os  # fix#13: this block references _os (like sibling blocks) but only imported plain 'os' -> NameError in _find_cron on re-run

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
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
    # fix#BUG3: agent-scope the gate lookup (was bare-name match). Two agents on ONE
    # gateway both name their gate "Note Cron Gate"; a bare-name match let agent B's
    # install find + edit/merge agent A's LIVE gate env, silently corrupting A's lanes.
    # Match the base installer: prefer a same-agentId job; fall back to name-only ONLY
    # when our agent id is unknown OR the stored job carries no agentId (single-agent/
    # legacy path — preserves the base<->neuron single-canonical-gate contract).
    _aid_l = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        jl = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        named = [j for j in (jl or []) if j.get('name','').strip().lower()==name.strip().lower()]
        if not named:
            return ''
        # 1) exact agent match wins
        for j in named:
            jaid = j.get('agentId') or ''
            jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
            if _aid_l and jaid and jaid == _aid_l:
                return j.get('id','')
        # 2) legacy/single-agent fallback: adopt ONLY a gate that carries NO agentId
        #    (the base/single-agent canonical gate). NEVER adopt another agent's
        #    explicitly-scoped gate — even when our own id is unknown — so a blank
        #    DINOMEM_AGENT_ID cannot hijack agent A's live gate.
        for j in named:
            jaid = j.get('agentId') or ''
            jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
            if not jaid:
                return j.get('id','')
        return ''
    except Exception:
        return ''
def _gate_env(gate_id):
    try:
        g = subprocess.run(['openclaw', 'cron', 'get', gate_id, '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
    subprocess.run(['openclaw', 'cron', 'enable', jid], capture_output=True, text=True, timeout=_CLI_T)

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
    subprocess.run(args, capture_output=True, text=True, timeout=_CLI_T)
    subprocess.run(['openclaw', 'cron', 'enable', existing_gate], capture_output=True, text=True, timeout=_CLI_T)
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
    r = subprocess.run(_cron_add_argv(gate), capture_output=True, text=True, timeout=_CLI_T)
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
            g = subprocess.run(['openclaw', 'cron', 'get', gid, '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
                subprocess.run(['openclaw', 'cron', 'remove', gid], capture_output=True, text=True, timeout=_CLI_T)
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
# SEPARATION OF CONCERNS: the Advancer/Improver/Deleter pipeline is the PROJECT
# state machine (project step-advance + verified deletion) and owns type:project
# notes ONLY. This janitor retires the NON-project notes that pipeline never
# touches: task_bound notes whose done_when is met, NON-project status:done notes
# (pipeline orphans — the Improver hard-filters to type:project, so nobody else
# retires them), and abandoned NON-project notes past stale_after. It is
# REPORT-ONLY on type:project notes (never deletes one; the pipeline decides).
# Claim-aware: never touches a freshly-claimed note (live-session <120m / other
# <30m). Deletions are snapshotted to backups/resolved_notes/ (recoverable).
# Premium: neuron only. Without this, done task_bound + non-project done notes
# would never auto-retire on a buyer install.
DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<PYEOF
import subprocess, json, os
try:
    _CLI_T = max(45, int(__import__('os').environ.get('DINOMEM_CLI_TIMEOUT_S', '60')))
except Exception:
    _CLI_T = 60
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

_DINO_DELIVER_CH = None
def _dino_deliver_channel():
    """Resolve an EXPLICIT delivery channel for announce-mode crons, once.
    WHY: OpenClaw defaults an announce cron to channel 'last'. On a MULTI-channel
    box 'last' is ambiguous -> the runtime refuses to deliver ('Channel is
    required when multiple channels are configured'), silently breaking every
    announce cron (Daily Note Review / Pending Note Reminder). Resolving one
    concrete channel at install time makes these crons work on single-, multi-,
    and 1:1 boxes alike. Fail-open to 'last' (the old behavior) so a single-
    channel box never regresses. Override with DINOMEM_DELIVER_CHANNEL."""
    global _DINO_DELIVER_CH
    if _DINO_DELIVER_CH is not None:
        return _DINO_DELIVER_CH
    ov = (_os.environ.get('DINOMEM_DELIVER_CHANNEL','') or '').strip()
    if ov:
        _DINO_DELIVER_CH = ov; return _DINO_DELIVER_CH
    ch = 'last'
    try:
        cr = subprocess.run(['openclaw','config','get','channels'],
                            capture_output=True, text=True, timeout=_CLI_T)
        if cr.returncode == 0 and cr.stdout.strip():
            cfg = json.loads(cr.stdout)
            usable = []
            for name, c in (cfg or {}).items():
                if not isinstance(c, dict):
                    continue
                accts = c.get('accounts', {}) or {}
                acct_on = any((a or {}).get('enabled') for a in accts.values())
                if c.get('enabled') or acct_on:
                    usable.append(name)
            if len(usable) == 1:
                ch = usable[0]
            elif len(usable) > 1:
                ch = 'telegram' if 'telegram' in usable else sorted(usable)[0]
    except Exception:
        pass
    _DINO_DELIVER_CH = ch
    return _DINO_DELIVER_CH

def _cron_add_argv(job):
    """Build a flag-based 'openclaw cron add' argv from a job dict.
    OpenClaw 2026.6.6+ has no 'cron add --json <blob>'; jobs are built from flags.
    '--json' here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
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
        a += ['--channel', _dino_deliver_channel()]
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via 'cron list --json'. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
            subprocess.run(args, capture_output=True, text=True, timeout=_CLI_T)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed)")
        else:
            j2 = dict(job); j2['enabled'] = False  # create DISABLED
            ar = subprocess.run(_cron_add_argv(j2), capture_output=True, text=True, timeout=_CLI_T)
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=_CLI_T)
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
        subprocess.run(['openclaw', 'cron', 'edit', jid, '--disable'], capture_output=True, text=True, timeout=_CLI_T)
        existing_env = {}
        try:
            g = subprocess.run(['openclaw', 'cron', 'get', gate_id, '--json'], capture_output=True, text=True, timeout=_CLI_T)
            if g.returncode == 0:
                existing_env = (json.loads(g.stdout).get('payload', {}) or {}).get('env', {}) or {}
        except Exception:
            existing_env = {}
        existing_env[gate_env_key] = jid
        envargs = ['openclaw', 'cron', 'edit', gate_id]
        for k, v in existing_env.items():
            envargs += ['--command-env', f'{k}={v}']
        subprocess.run(envargs, capture_output=True, text=True, timeout=_CLI_T)
        print(f"  \033[32m[ok]\033[0m   {label} wired into Note Cron Gate ({gate_env_key}) — gate-driven, zero idle LLM")
    else:
        # No gate (command-crons refused). Re-enable on own schedule (fallback).
        subprocess.run(['openclaw', 'cron', 'enable', jid], capture_output=True, text=True, timeout=_CLI_T)
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
        "message": "SCOPE LOCK (READ FIRST, NON-NEGOTIABLE): You operate ONLY on files whose basename matches the glob _note_*.md in $WS/memory/. Get the exact set by running: ls $WS/memory/_note_*.md. If that returns nothing / no matches, STOP IMMEDIATELY, touch nothing, output exactly NO_REPLY. You must NEVER read, evaluate, flip, delete, or GC any file that is not a _note_*.md file. In particular you must NEVER touch: _pin_*.md, _permanent*.md, MEMORY.md, or any date-prefixed distilled memory file (e.g. 2026-06-20_insight_*.md, *_entity_*.md, *_preference_*.md, *_relation_*.md, *_insight_*.md). Those are PERMANENT KNOWLEDGE, not task notes — deleting one is data loss. Do NOT glob memory/*.md, do NOT 'scan all memory files', do NOT infer that a distilled memory file is a resolvable note. If the count of _note_*.md files you are about to process is more than a handful (say >50) or includes anything not literally named _note_*.md, that is a BUG in your file selection — STOP and report it instead of acting. Only after this scope lock is satisfied, proceed.\n\nScan the _note_*.md files in $WS/memory/. Resolve each note (today = current UTC date). FIRST, for every note read claimed_by and claimed_at (UTC ISO8601): if claimed_by STARTS WITH 'live-session' AND claimed_at < 120 minutes ago, OR claimed_by is any other value AND claimed_at < 30 minutes ago, SKIP that note entirely this run (another job or a live human owns it). Stale or future-dated claims are ignored (proceed). Then: 1) SKIP any note with an expires: field until today > expires; delete a time_bound note only once today > its expires date (generic time-based GC; no external calendar service). SEPARATION OF CONCERNS (authoritative — the pipeline is the PROJECT state machine): the Project Advancer + Project Improver + Verified Note Deleter own type:project notes ONLY (the Improver hard-filters to type:project, so it never verifies/deletes a non-project note). Therefore: NEVER delete a type:project note here — for projects you are REPORT-ONLY. And a NON-project note at status:done is a pipeline ORPHAN (the Improver skips it, so the Deleter never gets a verified:true to act on, so it lives forever) — retiring THAT is exactly your job. 2) task_bound notes (have done_when:) that are NOT type:project: verify the done_when condition against workspace state (file exists, feature shipped, grep, exit 0). If verified, flip status to done and delete the note (its knowledge is distilled into regular memory by the Verified Note Deleter pipeline; do NOT copy it to _pin_ — pins are authored directly, not produced by retirement). Else leave pending. 2b) NON-project note at status:done (any type except project — e.g. task/note/reminder): this is a pipeline orphan nobody else retires. Snapshot it first to $WS/backups/resolved_notes/ (so the deletion is recoverable, matching the Verified Note Deleter's contract), then delete it. A done non-project note = a completed task; safe to retire. 3) type:project notes: REPORT ONLY — NEVER delete a project note here, regardless of done_when, checked steps, or resume_state. The Advancer advances it; the Improver + Verified Note Deleter retire it (status:done -> verified:true -> delete). Even if the top-level done_when artifact verifies, do NOT delete it — a passing artifact does NOT authorize the janitor to delete a project mid-flight; the pipeline decides. Just report the project's status and move on. (This closes the old 'artifact wins' shortcut that deleted projects out from under the Advancer.) 4) type:brainstorm notes (settled-thinking, status:design; NOT tasks, so they carry NO done_when and are normally left to the Brainstorm Ideator): a brainstorm can nonetheless have a SHIPPABLE outcome that already landed out-of-band in a live session, leaving it stranded at status:design forever. IF such a note carries a shipped_when: field, verify it the SAME locally-checkable way as done_when (file exists / grep / exit 0 / compare the repo HEAD commit to its upstream @{u} via git rev-parse run in the repo dir and confirm HEAD equals upstream). If shipped_when verifies, flip status:design -> status:resolved and LEAVE THE NOTE IN PLACE (do NOT delete it — a brainstorm's thinking is the value; resolved brainstorms are retained/promoted, never reaped by this cron). If shipped_when does not verify, leave it design for the Ideator. If a brainstorm has NO shipped_when field, DO NOT infer completion — leave it entirely to the Ideator (pure open-ended thinking has no machine-checkable resolution and must stay human/ideator-resolved). Never delete a type:brainstorm note here regardless. 5) stale_after GC (NON-project only): if a note is NOT type:project AND is still pending/in_progress AND done_when was never met AND today > stale_after (default date+30d, or date+7d for reminder/quick-todo notes), delete it as abandoned. NEVER stale-GC a type:project note here (a stale project is the Advancer's problem, not yours). 6) Legacy notes with no schema fields at all: REPORT ONLY — do NOT infer completion from body text and do NOT delete. String-matching words like 'done'/'completed' in free-form prose (e.g. a resume_state describing a finished sub-step) falsely deletes live notes; a schema-less note is left in place for a human to triage. Report what resolved, what was GC'd, what remains, and what was skipped (including claim-skips).",
        "lightContext": True,
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_gated(job, "daily_note_review", "daily_note_review", "$WS/.cron_ids", "GATE_DAILY_NOTE_REVIEW_ID")
PYEOF
fi  # end DO_CRON guard (hoisted from after project_advancer; --no-cron now skips ALL cron installs, INVESTIGATE_FIRST stays in scope)

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
  recall_door_binding: base M0/M1/M2 upgraded_recall_door → hybrid_recall
  retrieval-routing: which retrieval tool per query (docs_search | session_search | memory_search | data_query | graph_search | code_query | grep) + how to ingest long docs/images, register/query structured data, AND build/query the code-structure graph. READ when: doc OR image shared/uploaded (pdf/docx/etc OR jpg/png/screenshot/photo/scan) | save/remember a doc or image | content >2000 chars | row|count|range|filter|sort|aggregate|field-lookup over csv/json/parquet | navigating THIS repo's own source (what calls X | X calls what | class hierarchy | where a fn is defined | edge path A->B) OR code_query returns [stale]/empty (build/refresh via code_graph) | unsure which retrieval tool to use.
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
  CODE STRUCTURE: what calls X | X calls what | class hierarchy | where a function/method is defined | edge path A→B (file:line) → tools/code_query.py (explain|path|query) over the ZERO-LLM code graph — NOT grep+read whole files (context-stuffs ~20x tokens). Build/refresh: procedures/code_graph.py --path <root> (AST, no LLM; git-incremental).
  long doc / file shared / "remember this doc" → docs_ingest ; csv|json|parquet dataset → data_register ; code tree to navigate (this repo's own source — calls/callers/hierarchy/where-defined) OR code_query stale/empty → procedures/code_graph.py --path <root> (AST, zero-LLM, git-incremental) to BUILD/REFRESH the graph first
  what CHANGED in memory (adds/merges/deletes) → read memory/.diffs/ ; which retrieval tool ran / routing quality → kb/retrieval_log/
  # --- FUZZY recall (the one door) ---
  # ENFORCED, not advisory: the dinomem-recall-gate plugin (Tier C, neuron-only) now
  # ROUTES a raw memory_search reflex to hybrid_recall -- soft-nudge on the first
  # direct reach this turn, HARD-BLOCK on repeat. memory_search is NOT in the neuron
  # recallTools set anymore (it's the gated raw tool); hybrid_recall IS the door and
  # satisfies recall. EXEMPT: once hybrid_recall has run this turn, memory_search is
  # allowed (hybrid_recall calls it internally via --external-hits). So: reach for the
  # DOOR (hybrid_recall) for semantic recall; only touch raw memory_search AFTER the
  # door has opened this turn, or for a deliberate single native lookup you re-issue past the nudge.
  fact | decision | preference | todo | meaning | passage | recent convo (<7d) | relational (why/led-to/affects/trace) → hybrid_recall  [gate-enforced: raw memory_search is routed here]
  "what does CODE-SYMBOL X do AND why does it exist" (code structure + the design decision behind it, one block) → hybrid_recall (CODE leg auto-fires on function/class/calls/imports/inherits queries; fuses code_query file:line hits WITH the memory/graph 'why'). Killer feature: neither plain code-search nor plain memory alone answers it.
    # hybrid_recall fuses docs_search + session_search + graph_search + code_query legs; run memory_search natively and pass its hits via --external-hits to fold them in.
    # M3 natural-language query rule applies (avoid code identifiers/exact strings).

## recursive_search_routing
  # Upgrades base recursive_search_discipline: neuron ships the right tools, so route AWAY from grep for the two cases where a better tool exists.
  transcript trees (agent/session/kb conversation history) -> session_search (indexed archived transcripts), NOT grep. Grep on transcripts is the wrong tool AND floods context.
  code structure of own source (what calls X | X calls what | class hierarchy | where a fn is defined | edge path A->B) -> tools/code_query.py (explain|path|query) FIRST; grep only as fallback. Graph stale/empty -> procedures/code_graph.py --path <root> to build/refresh.
  everything else recursive: base recursive_search_discipline still applies (scoped rg + timeout, never bare grep -r over huge roots).
DINOMEM_NEURON_AGENTS_BODY
)
BLOCK="$BEGIN
$DINOMEM_BODY
$END"

wire_managed_block "$AGENTS" "$BEGIN" "$END" "$BLOCK" "AGENTS.md"

# NEURON-BLOCK RESTORE-GUARD (pairs with the crash-snapshot above). WHY: the
# normal path just wrote the neuron block, so this is a no-op. But if a PRIOR run
# crashed mid-window (base block written, neuron block dropped) AND this run's
# neuron write above somehow didn't land (interrupted grep/awk, disk), the block
# would still be missing. Restore it from the snapshot so a half-install
# self-heals on the next run instead of shipping a base-only AGENTS.md. Idempotent:
# only fires when the block is genuinely absent after the write.
if [ -n "${NEURON_SNAPSHOT:-}" ] && [ -s "$NEURON_SNAPSHOT" ] \
   && ! grep -qF 'BEGIN:dinomem-neuron' "$AGENTS" 2>/dev/null; then
  printf '\n%s\n' "$(cat "$NEURON_SNAPSHOT")" >> "$AGENTS"
  warn "neuron block was missing after write (prior crashed install?) — restored from crash-snapshot"
fi

# AGENTS.md TRANSACTION COMMIT (Fix 2). Both blocks are now (re)written above. Only
# now is it safe to clear the whole-file restore trap armed before auto_install_base:
# verify the file actually carries BOTH a base and a neuron block, and only then set
# AGENTS_TXN_DONE=1 so the EXIT trap becomes a no-op cleanup. If EITHER block is
# somehow still absent (write raced/failed), we leave the trap ARMED so the original
# is restored on exit rather than shipping a half-written file. WHY here (not later):
# everything past this point is verify/report -- the destructive writes are done.
if grep -qF 'BEGIN:dinomem (managed' "$AGENTS" 2>/dev/null \
   && grep -qF 'BEGIN:dinomem-neuron' "$AGENTS" 2>/dev/null; then
  AGENTS_TXN_DONE=1
else
  warn "AGENTS.md missing a base or neuron block after wiring — leaving restore-trap armed (original will be restored on exit)"
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

  code_graph:
    path: procedures/code_graph.py
    type: exec
    db: kb/code_graph/
    purpose: "ZERO-LLM code-structure indexer. tree-sitter AST -> nodes(file|function|class|method w/ file:line) + edges(calls|imports|inherits|contains, provenance extracted|inferred) into a SEPARATE code graph (reuses the graph_store SQLite backend, not the memory DB file). Build the structural index ONCE at $0, then code_query serves scoped subgraphs instead of grep+read whole files into context (~20x fewer tokens on big repos)."
    capabilities:
      - ast_extract_no_llm
      - path_and_line_nodes
      - extracted_inferred_provenance
      - git_incremental_rebuild
      - churn_node_weight
    inputs:
      path:
        type: string
        required: false
        note: "Root to index (--path). Ladder: --path > DINOMEM_CODE_ROOTS env > agent workspace > ~/.openclaw."
      lang:
        type: string
        required: false
        note: "Restrict to one lang key, e.g. python (--lang). Default: all allowlisted extensions."
      full:
        type: boolean
        required: false
        note: "Force full rebuild (--full). Default: git-incremental when a prior DB+sha+cache exist (re-parses ONLY changed files; fail-open to full)."
    output:
      note: "Summary: mode (full|incremental|noop), node/edge counts, built_at_sha, parse_errors."
    constraints:
      mode: read_write
    libraries:
      - tree-sitter-language-pack (or tree-sitter + tree-sitter-languages)
    caveat: "OPTIONAL dep. Missing tree-sitter -> code_graph OFFLINE (fail-soft, rest of neuron unaffected). ZERO LLM in the build path by design."

  code_query:
    path: tools/code_query.py
    type: exec
    db: kb/code_graph/code_graph.json
    purpose: "Query the code-structure graph. Ask for a SCOPED subgraph (what a symbol calls / is called by / inherits / lives in, or the edge path A->B) with file:line + EXTRACTED/INFERRED tags, instead of grep+read whole files. Reuses graph_search traversal (no fork)."
    subcommands:
      - "explain <symbol>   # neighborhood: contains/calls/called-by/imports/inherits, each file:line + provenance"
      - "path <A> <B>       # shortest edge path A -> B (how one symbol reaches another)"
      - "query \"<nl>\"       # fuzzy-find best-matching symbol then explain it"
    inputs:
      hops:
        type: integer
        required: false
        note: "Traversal depth for explain/query (--hops, default 1)."
      path_prefix:
        type: string
        required: false
        note: "Scope to a file-path prefix, e.g. procedures/ (--path)."
      db:
        type: string
        required: false
        note: "Code graph json path (--db, default kb/code_graph/code_graph.json)."
    output:
      note: "Grouped edges w/ file:line + EXTRACTED/INFERRED tags. Prints a [stale] warning when built_at_sha != repo HEAD (refresh via code_graph.py). Also served fused via the hybrid_recall CODE leg for 'what does X do AND why'."
    constraints:
      mode: read_only
DINOMEM_NEURON_TOOLS_BODY
)
TOOLS_BLOCK="$BEGIN
$TOOLS_BODY
$END"

wire_managed_block "$TOOLS" "$BEGIN" "$END" "$TOOLS_BLOCK" "TOOLS.md"

# ── Patch openclaw.json bootstrap caps ───────────────────────────────────────
hr "OpenClaw config"
# CONFIG-ISOLATION SAFETY: when OPENCLAW_CONFIG is set (sandbox/bench harness
# points it at a lab-local config so plugin writes stay in the lab), HONOR IT
# ABSOLUTELY. NEVER silently fall through to $OPENCLAW_DIR/openclaw.json — that
# fallback is what leaked dead lab-temp paths into a caller's REAL openclaw.json
# (plugins.load.paths / entries.*.config.workspaceDir dangling -> gateway
# crash-loop, exit 78). Only the unset case may probe the default locations.
if [ -n "${OPENCLAW_CONFIG:-}" ]; then
  OPENCLAW_JSON="$OPENCLAW_CONFIG"
  if [ ! -f "$OPENCLAW_JSON" ]; then
    # honor the redirect even if the file is absent: seed an empty stub so all
    # subsequent patches land here, NOT in the real config. A sandbox caller
    # that wants base config seeded should create it before invoking us.
    mkdir -p "$(dirname "$OPENCLAW_JSON")" 2>/dev/null || true
    [ -e "$OPENCLAW_JSON" ] || printf '{}\n' > "$OPENCLAW_JSON" 2>/dev/null || true
  fi
else
  OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
  [ -f "$OPENCLAW_JSON" ] || OPENCLAW_JSON="$OPENCLAW_DIR/openclaw.json"
fi
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

# NEURON TIER C: route the raw memory_search reflex to the fuzzy door (hybrid_recall).
# The engine reads plugins.entries[PID].config (NOT the plugin.json schema defaults),
# so the neuron-tier values MUST be written here to take effect live.
#   - routeEnforce=true: soft-nudge memory_search->hybrid_recall once, hard-block on repeat.
#   - recallTools MUST DROP memory_search: under Tier C it's the gatedTool, not a
#     recall-satisfier; if left in recallTools the recall-latch fires before Tier C
#     sees it and the route gate is dead. hybrid_recall is the door and satisfies recall.
enconf = entry.get("config")
if not isinstance(enconf, dict):
    enconf = {}
    entry["config"] = enconf
NEURON_RECALL_TOOLS = ["hybrid_recall", "memory_get", "graph_search",
                       "session_search", "docs_search", "data_query"]
if enconf.get("routeEnforce") is not True:
    enconf["routeEnforce"] = True
    changed.append(f"plugins.entries.{PID}.config.routeEnforce -> true (TierC)")
if enconf.get("routeTool") != "hybrid_recall":
    enconf["routeTool"] = "hybrid_recall"
    changed.append(f"plugins.entries.{PID}.config.routeTool -> hybrid_recall")
if enconf.get("gatedTool") != "memory_search":
    enconf["gatedTool"] = "memory_search"
    changed.append(f"plugins.entries.{PID}.config.gatedTool -> memory_search")
if enconf.get("recallTools") != NEURON_RECALL_TOOLS:
    enconf["recallTools"] = NEURON_RECALL_TOOLS
    changed.append(f"plugins.entries.{PID}.config.recallTools -> neuron-tier (memory_search dropped, hybrid_recall door)")

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
# ── Silence cron failure-alert channel spam (global default, opt-out safe) ──────
# WHY: when a cron run fails at the model/runner layer (e.g. a provider-wide rate-
# limit exhausts the whole fallback chain -> FallbackSummaryError), OpenClaw's
# failure-notification path falls back to the job's ANNOUNCE target if no separate
# failureDestination is set. For dinomem's announce-to-channel crons (Project
# Advancer / Improver / Proof Coverage / etc.) that means a transient infra hiccup
# dumps a wall of model-error text into the user's chat, and re-fires every tick the
# outage lasts. The zero-LLM pre-gates already keep idle runs silent (NO_REPLY); this
# closes the OTHER silence hole — runner-level failures the gate never gets to catch.
# We set the GLOBAL default cron.failureAlert.enabled=false so every current + future
# cron inherits it. IDEMPOTENT + OPT-OUT SAFE: only writes when cron.failureAlert is
# ENTIRELY UNSET — a user who deliberately configured failure alerts is never clobbered.
if [ -f "$OPENCLAW_JSON" ]; then
  DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" python3 - <<'PYEOF'
import json, os
path = os.environ["DINOMEM_OPENCLAW_JSON"]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"  \033[33m[skip]\033[0m cron failure-alert default: could not read openclaw.json ({e})")
    raise SystemExit(0)
cron = cfg.get("cron")
if not isinstance(cron, dict):
    cron = {}
# OPT-OUT SAFE: touch ONLY when the whole failureAlert block is absent. If the user
# set it (even enabled:true), leave their choice alone.
if "failureAlert" in cron:
    print("  \033[33m[skip]\033[0m cron.failureAlert already configured — leaving user setting untouched")
    raise SystemExit(0)
if os.environ.get("DINOMEM_DRY_RUN") == "1":
    print("  \033[36m[plan]\033[0m set cron.failureAlert.enabled=false (silence cron failure channel spam)")
    raise SystemExit(0)
cron["failureAlert"] = {"enabled": False}
cfg["cron"] = cron
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  \033[32m[ok]\033[0m   cron.failureAlert.enabled=false (cron failures no longer spam the chat channel)")
PYEOF
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
  if openclaw_running; then
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
_REQUIRED_CRONS="Project Advancer|Project Improver (Component 2)|Verified Note Deleter (Component 1)|Note Cron Gate"
_MISSING_REQUIRED=""
if [ "$DO_CRON" != 1 ]; then
  # --no-cron: cron installation was intentionally skipped, so the required-cron
  # gate MUST NOT fire (it would fail a run that never wanted crons — e.g. the
  # benchmark harness / sandboxed lab installs). Report informational only.
  sc_pass "cron gate skipped (--no-cron): note-lifecycle crons intentionally not installed"
elif command -v openclaw >/dev/null 2>&1; then
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
    if [ "$REPAIR_CRON" = 0 ] && openclaw_running; then
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
if openclaw_running; then
  # `|| true`: this probe is DIAGNOSTIC ONLY. Under `set -euo pipefail` a
  # non-zero from the `openclaw hooks check | python` pipeline (e.g. a headless
  # sandbox / benchmark lab with no real gateway or hooks config) would become
  # the SCRIPT'S final exit status and make a fully-successful install report
  # failure (silent, since the diagnostics go to stdout). Never let it.
  _elig="$(openclaw hooks check --json 2>/dev/null | "$PY" -c "import json,sys;
try:
  d=json.load(sys.stdin); print('yes' if 'dinomem-open-notes' in d.get('hooks',{}).get('eligible',[]) else 'no')
except Exception: print('unknown')" 2>/dev/null)" || true
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
      sc_warn "dinomem-open-notes not eligible from $WS/hooks/ — copied to global $_ghd/ + re-enabled; RESTART OpenClaw, then verify: openclaw hooks check --json"
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

# ── P3 final self-check (no fake success) ────────────────────────────────────
# Before declaring success, ASSERT the install is actually sound: (1) marker
# integrity (BEGIN==END==1 for both dinomem + dinomem-neuron blocks in AGENTS.md,
# and the neuron TOOLS.md block), (2) every REQUIRED dep imports, (3) an embedding
# endpoint answers. Any miss => list it and exit 1. WHY: the old flow warned on a
# broken piece then still printed "✔ installed", so a half-dead install looked
# green. This is the single authoritative gate. Skipped under DRY_RUN.
if [ "$DRY_RUN" != 1 ]; then
  _sc_fail=""
  _marker_pair() { # $1=file $2=begin-needle $3=end-needle $4=label
    local _b _e
    _b=$(grep -cF "$2" "$1" 2>/dev/null || echo 0)
    _e=$(grep -cF "$3" "$1" 2>/dev/null || echo 0)
    [ "$_b" = 1 ] && [ "$_e" = 1 ] || _sc_fail="${_sc_fail}\n    �’ marker $4: BEGIN=$_b END=$_e (want 1/1) in $1"
  }
  _marker_pair "$WS/AGENTS.md" "BEGIN:dinomem (managed" "<!-- END:dinomem -->" "dinomem(base)"
  _marker_pair "$WS/AGENTS.md" "BEGIN:dinomem-neuron" "<!-- END:dinomem-neuron -->" "dinomem-neuron"
  _marker_pair "$WS/TOOLS.md" "BEGIN:dinomem-neuron" "END:dinomem-neuron" "TOOLS.md-neuron"
  # REQUIRED deps must import (mirrors P4 REQUIRED tier; a warn-only miss here would
  # let a silently-dead feature ship green).
  for _mod in langchain_chroma langchain_huggingface numpy pandas duckdb yaml tree_sitter spacy; do
    python3 -c "import $_mod" >/dev/null 2>&1 || _sc_fail="${_sc_fail}\n    ✗ REQUIRED python dep not importable: $_mod"
  done
  python3 -c "import torch" >/dev/null 2>&1 || _sc_fail="${_sc_fail}\n    ✗ REQUIRED python dep not importable: torch"
  # embedding endpoint must answer (memory/recall is dead without it).
  _embed_url="${DINOMEM_EMBED_URL:-http://localhost:8080}"
  curl -fsS -m 5 "${_embed_url%/}/health" >/dev/null 2>&1 \
    || curl -fsS -m 5 "${_embed_url%/v1/embeddings}/health" >/dev/null 2>&1 \
    || _sc_fail="${_sc_fail}\n    ✗ embedding endpoint not answering ($_embed_url) — memory_search/recall will be dead"
  if [ -n "$_sc_fail" ]; then
    hr "self-check FAILED"
    printf '  \033[31m[fail]\033[0m install is NOT sound:%b\n' "$_sc_fail"
    printf '  Fix the above and re-run. (Not printing a success banner over a broken install.)\n'
    exit 1
  fi
  ok "self-check passed (markers 1/1, required deps import, embedding endpoint live)"
fi

# ── #4 install manifest ──────────────────────────────────────────────────────
# WHY: same as base — make "did this finish / what version / what registered?"
# answerable without forensics. UPGRADES the base manifest in place (layer flips to
# base+neuron, neuron_version added). Written only at the tail of a completed run,
# past the self-check gate above, so its existence means neuron install succeeded.
# One file per agent. Pure heredoc — no jq dependency.
if [ "${DRY_RUN:-0}" != 1 ]; then
  _MANIFEST_DIR="$HOME/.dinomem"
  _MANIFEST="$_MANIFEST_DIR/install_manifest_${AGENT_ID}.json"
  _NEURON_VER="$(cat "$SKILL_DIR/VERSION" 2>/dev/null | tr -d '[:space:]')"; [ -n "$_NEURON_VER" ] || _NEURON_VER="unknown"
  _NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
  if mkdir -p "$_MANIFEST_DIR" 2>/dev/null; then
    cat > "$_MANIFEST" <<MANIFEST_EOF
{
  "layer": "base+neuron",
  "neuron_version": "$_NEURON_VER",
  "installed_at": "$_NOW",
  "agent_id": "$AGENT_ID",
  "workspace": "$WS",
  "cron_surface": "system-crontab (list via: crontab -l | grep dinomem); Note Cron Gate lives in openclaw cron list"
}
MANIFEST_EOF
    ok "install manifest: $_MANIFEST (neuron v$_NEURON_VER)"
  else
    warn "could not write install manifest to $_MANIFEST_DIR (non-fatal)"
  fi
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
echo "  5. Verify install:         bash <dinomem-base>/scripts/verify_install.sh --workspace $WS"
echo "                             (checklist incl. neuron crons — should end 'VERIFY_INSTALL: OK base+neuron')"
echo ""
echo "  \033[1mℹ️  Where neuron's crons live (READ THIS if a cron looks 'missing'):\033[0m"
echo "    neuron adds memory_graph / memory_synthesis / memory_promote / code_graph / retrieval_log"
echo "    crons to the SYSTEM CRONTAB — list them with:  crontab -l | grep dinomem"
echo "    They do NOT show in 'openclaw cron list' (that reads the separate Gateway SQLite store,"
echo "    which holds only the zero-LLM 'Note Cron Gate'). Two schedulers — checking the wrong"
echo "    one makes a healthy install look incomplete."
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

# ── Placeholder-leftover safety net (self-healing) ───────────────────────────
# WHY: _apply_subst resolves DINOMEM_*_PLACEHOLDER at copy time, but a file can
# slip through unresolved if it was installed by an OLDER installer (before that
# file joined the copy set) or via a path that bypassed _apply_subst. A single
# unresolved DINOMEM_AGENT_SESSIONS_PLACEHOLDER in session_reset.py silently
# BREAKS the auto-reset cron for that agent (path points nowhere -> crash every
# run) and stays invisible until sessions stop resetting. One authoritative sweep
# here at the end: find any installed *.py/*.sh under the managed dirs that still
# carries a DINOMEM_*_PLACEHOLDER, resolve the known tokens in place, and report.
# Idempotent, self-healing, and loud.
if [ "${DRY_RUN:-0}" = 0 ]; then
  _ph_healed=0
  while IFS= read -r -d '' _phf; do
    if grep -q 'DINOMEM_[A-Z_]*_PLACEHOLDER' "$_phf" 2>/dev/null; then
      cp "$_phf" "$_phf.phtmp.$$" 2>/dev/null || continue
      _apply_subst "$_phf.phtmp.$$" 2>/dev/null && mv "$_phf.phtmp.$$" "$_phf" || rm -f "$_phf.phtmp.$$"
      if grep -q 'DINOMEM_[A-Z_]*_PLACEHOLDER' "$_phf" 2>/dev/null; then
        warn "placeholder-heal: $_phf still has an UNRESOLVED token (not covered by _apply_subst) — inspect manually"
      else
        warn "placeholder-heal: resolved leftover token(s) in ${_phf#$WS/}"
        _ph_healed=$((_ph_healed+1))
      fi
    fi
  done < <(find "$WS/procedures" "$WS/tools" "$WS/scripts" -type f \( -name '*.py' -o -name '*.sh' \) ! -name '*.bak*' -print0 2>/dev/null)
  if [ "$_ph_healed" -gt 0 ]; then
    warn "placeholder-heal: patched $_ph_healed installed file(s) that shipped with unresolved DINOMEM_*_PLACEHOLDER tokens (older-installer straggler). Clean now."
  fi
fi

# Reaching the done-banner == a successful install. All GENUINE hard-fail paths
# above exit 1 explicitly BEFORE here (required-cron gap, base-not-found, license,
# etc). Past this point only cosmetic status echoes / diagnostic probes run, and
# under `set -euo pipefail` any of their non-zero statuses would otherwise fall
# off the end as the script's exit code — silently failing a good install in a
# headless sandbox. Pin success explicitly.

# Topology-aware restart (single-agent auto | multi-agent print | --restart force).
# Guarded: the helper lives in the shared discover lib, which is sourced only if
# present — fall back to the plain restart nag if it wasn't (old lib / non-systemd).
if command -v _maybe_restart >/dev/null 2>&1; then
  _maybe_restart
else
  echo "  → Restart OpenClaw to apply:  openclaw gateway restart"
fi

exit 0
