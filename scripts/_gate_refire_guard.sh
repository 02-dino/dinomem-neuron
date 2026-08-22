#!/usr/bin/env bash
# _gate_refire_guard.sh — THIN COMPAT-SHIM (v1).
#
# The work-once / re-fire guard that used to live here has MOVED DOWN to base's
# `scripts/lib/gate_lib.sh` as `guard_composite`. This file is kept as a one-line
# compatibility shim so the 5 neuron gates that source it (check_projects /
# check_improvable / check_deletable / check_ideator / check_daily_notes) keep
# working with ZERO changes:
#
#   source "$SCRIPTS_DIR/_gate_refire_guard.sh"   # unchanged
#   if refire_should_fire ... ; then ...          # unchanged — now guard_composite
#
# We resolve base's lib relative to THIS script (install-on-top puts base's
# scripts/lib/ in the SAME scripts dir as neuron's scripts), falling back to a
# sibling base install, and only as a last resort re-define an equivalent guard
# inline (fail-open so a missing lib can never break a gate). Behavior is
# behavior-identical to the old inline guard: hash EXCLUDES the volatile
# claimed_by/claimed_at lines (the v1 self-perpetuating-loop fix).

# Locate base's gate_lib.sh. Candidate 1..n in priority order. Use BASH_SOURCE
# (this file's own path), NOT $0 — these gates `source` this file, so $0 is the
# CALLING script, not the shim. BASH_SOURCE[0] always resolves to the shim dir.
_gl_dir="$(dirname "${BASH_SOURCE[0]}")"
_gl_src="$_gl_dir/lib/gate_lib.sh"                                          # install-on-top: same scripts dir
for _gl_cand in \
  "$_gl_src" \
  "$_gl_dir/../../dinomem/scripts/lib/gate_lib.sh" \
  "${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/scripts/lib/gate_lib.sh" \
  "$_gl_dir/../lib/gate_lib.sh"; do
  if [ -f "$_gl_cand" ]; then
    # shellcheck source=/dev/null
    source "$_gl_cand" 2>/dev/null
    break
  fi
done
unset _gl_dir _gl_src _gl_cand

# If guard_composite is available from base's lib, alias it. Otherwise fall back
# to an inline-equivalent definition so the 5 gates never break (defensive only —
# per spec base's lib is ALWAYS present on a neuron box).
# Per-note guard alias (anti-starvation). If base's lib exposes
# guard_composite_pernote, surface it under a stable name for the gates; else
# FAIL-OPEN to the aggregate guard so an old base lib never breaks a gate (just
# reverts to the pre-fix behaviour).
if command -v guard_composite_pernote >/dev/null 2>&1 \
   || declare -F guard_composite_pernote >/dev/null 2>&1; then
  refire_should_fire_pernote() { guard_composite_pernote "$@"; }
else
  # fallback: no per-note primitive available -> behave like the set guard.
  refire_should_fire_pernote() { refire_should_fire "$@"; }
fi

if ! command -v guard_composite >/dev/null 2>&1 \
   && ! declare -F guard_composite >/dev/null 2>&1; then
  refire_should_fire() {
    local state_file="$1" min_interval="$2"; shift 2
    local -a notes=("$@")
    [ "${#notes[@]}" -gt 0 ] || return 1
    local now_epoch; now_epoch=$(date -u +%s)
    local agg_hash
    agg_hash=$(
      for f in "${notes[@]}"; do
        printf '%s %s\n' "$(basename "$f")" \
          "$(grep -vE '^claimed_(by|at):' "$f" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1)"
      done | sort | sha256sum | cut -d' ' -f1
    )
    local last_run=0 last_hash=""
    if [ -f "$state_file" ]; then
      read -r last_run last_hash < "$state_file" 2>/dev/null || true
      case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
    fi
    local changed=1
    if [ "$last_run" -gt 0 ] && [ -n "$last_hash" ]; then
      [ "$agg_hash" != "$last_hash" ] && changed=0
    else
      changed=0
    fi
    local age=$(( now_epoch - last_run ))
    local due=1
    [ "$age" -ge "$min_interval" ] && due=0
    if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ]; then
      printf '%s %s\n' "$now_epoch" "$agg_hash" > "$state_file" 2>/dev/null || true
      return 0
    fi
    return 1
  }
else
  refire_should_fire() { guard_composite "$@"; }
fi
