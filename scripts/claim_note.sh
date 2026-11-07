#!/usr/bin/env bash
# claim_note.sh — atomic note-claim helper for the shared claim protocol.
#
# WHY: the Advancer / Improver / Deleter crons all READ claimed_by + claimed_at
# and back off when a note is freshly claimed (live-session 30min, other 30min).
# But the WRITE side was documented-only — no actor actually stamped a claim, so
# a live human editing a note and a cron could still double-write it (proven race
# 2026-07-08: hand-edited notes all session, Improver saw them unclaimed, grabbed
# one, hit the external-push gate, re-nagged). This script is the missing WRITER:
# one atomic command any actor (a live agent before editing, or a wrapper) calls
# to stamp / refresh / release / check a claim. Makes the protocol enforceable
# instead of aspirational.
#
# Frontmatter contract (YAML block at top of the _note_, delimited by --- / ---):
#   claimed_by: <claimant>      e.g. live-session-analyst | advancer-<id> | component2-improver
#   claimed_at: <UTC ISO8601 Z> e.g. 2026-07-08T03:30:00Z
#
# USAGE:
#   claim_note.sh claim   <note> <claimant>   # take/refresh claim IF free or already yours or stale; exit 0 ok, 3 if held by someone else (fresh)
#   claim_note.sh refresh <note> <claimant>   # bump claimed_at (must already be yours); exit 0 ok, 3 if not yours
#   claim_note.sh release <note>              # clear claimed_by + claimed_at; exit 0
#   claim_note.sh check   <note>              # exit 0 free/stale/none, 3 if freshly held by another; prints holder
#
# STALENESS (mirrors the cron rule so writer + readers agree):
#   live-session-* : 30 min ;  any other claimant : 30 min ;  future-dated : invalid/stale
#   (live window was 120min; shrunk to 30 so a crashed live session's claim goes
#    stale fast. The note-claim-refresh hook bumps claimed_at on ANY edit to a
#    held note, so a genuinely-live worker easily re-refreshes inside 30min;
#    a dead worker stops editing -> stale in 30min -> Advancer can resume.)
#
# Atomicity: writes via a temp file + mv (atomic rename) and a per-note flock, so
# two concurrent callers can't interleave a half-written frontmatter.
#
# Zero external deps: pure bash + date + awk. Safe to run from cron or a live agent.
set -euo pipefail

LIVE_WINDOW_MIN=30
OTHER_WINDOW_MIN=30

die() { echo "claim_note: $*" >&2; exit 2; }

cmd="${1:-}"; note="${2:-}"; claimant="${3:-}"
[ -n "$cmd" ] || die "usage: claim_note.sh <claim|refresh|release|check> <note> [claimant]"
[ -n "$note" ] || die "missing <note> path"
# Accept either a full path OR a bare _note_ filename: if <note> isn't a file as
# given, retry it under the memory dir. Makes callers robust to the memory/ prefix
# (a bare '_note_foo.md' resolves the same as 'memory/_note_foo.md').
if [ ! -f "$note" ]; then
  _memdir="DINOMEM_WORKSPACE_PLACEHOLDER/memory"
  if [ -f "$_memdir/$note" ]; then
    note="$_memdir/$note"
  else
    die "note not found: $note (tried '$note' and '$_memdir/$note')"
  fi
fi

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_epoch() { date -u +%s; }

# Parse current claim from frontmatter. Handles TWO formats:
#  (1) YAML fenced:  first line ---, block ends at next ---.
#  (2) project-note: first line `# Heading`, bare `key: value` frontmatter
#      that ends at the first blank line (matches check_projects.sh / resolve_done_notes.py).
read_claim() {
  awk '
    NR==1 && $0=="---" { infm=1; fenced=1; next }   # format 1: fenced
    NR==1 && /^#/       { infm=1; next }             # format 2: heading -> bare frontmatter
    NR==1 { exit }                                   # neither -> no frontmatter
    infm && fenced && $0=="---" { exit }             # end of fenced block
    infm && !fenced && NF==0 { exit }                # end of bare block (blank line)
    infm && /^claimed_by:/  { sub(/^claimed_by:[ \t]*/,""); by=$0 }
    infm && /^claimed_at:/  { sub(/^claimed_at:[ \t]*/,""); at=$0 }
    END { print by "\t" at }
  ' "$note"
}

# epoch of an ISO8601 Z timestamp, or empty on parse failure.
iso_epoch() {
  local ts="$1"; [ -n "$ts" ] || { echo ""; return; }
  date -u -d "$ts" +%s 2>/dev/null || echo ""
}

# Is the existing claim stale/absent? echoes: free | stale | mine | held
claim_state() {
  local cur_by="$1" cur_at="$2" me="$3"
  [ -n "$cur_by" ] || { echo free; return; }
  local ce; ce="$(iso_epoch "$cur_at")"
  [ -n "$ce" ] || { echo stale; return; }          # unparseable -> stale
  local ne; ne="$(now_epoch)"
  if [ "$ce" -gt "$ne" ]; then echo stale; return; fi   # future-dated -> invalid
  local win_min="$OTHER_WINDOW_MIN"
  case "$cur_by" in live-session-*) win_min="$LIVE_WINDOW_MIN";; esac
  local age_min=$(( (ne - ce) / 60 ))
  if [ "$age_min" -ge "$win_min" ]; then echo stale; return; fi
  if [ -n "$me" ] && [ "$cur_by" = "$me" ]; then echo mine; return; fi
  echo held
}

# Temp-file hygiene. WHY: mktemp'd "<note>.claim.XXXXXX" files used to be left behind
# whenever the process died between mktemp and mv (measured 2026-11-07: 71 orphans in
# memory/, Oct 6 -> Nov 6, which also made Daily Note Review see phantom notes).
# EXIT trap removes ours (rm is a no-op once mv consumed it); the sweep reaps orphans
# from SIGKILLed runs, where no trap can fire. 60min >> any live write (ms), so an
# in-flight sibling is never touched.
_TMPS=()
_cleanup_tmps() { [ "${#_TMPS[@]}" -gt 0 ] && rm -f "${_TMPS[@]}" 2>/dev/null; return 0; }
trap _cleanup_tmps EXIT
# Sets global TMP_NEW instead of echoing: callers must NOT use $(mk_tmp) -- command
# substitution runs in a subshell, so the _TMPS append would be lost and the EXIT trap
# would have nothing to remove (caught by the failure-path test, 2026-11-07).
mk_tmp() { TMP_NEW="$(mktemp "${note}.claim.XXXXXX")" || return 1; _TMPS+=("$TMP_NEW"); }
sweep_stale_tmps() {
  find "$(dirname "$note")" -maxdepth 1 -type f -name "$(basename "$note").claim.??????" -mmin +60 -delete 2>/dev/null || true
}

# Rewrite frontmatter with new claimed_by/claimed_at (or strip them when by="").
# Ensures a --- frontmatter block exists; injects one if absent.
write_claim() {
  local by="$1" at="$2"
  local tmp; mk_tmp; tmp="$TMP_NEW"
  if head -1 "$note" | grep -qx -- '---'; then
    # Format 1 (YAML fenced): drop prior claim lines, re-insert before closing ---.
    awk -v by="$by" -v at="$at" '
      NR==1 { print; infm=1; next }
      infm && $0=="---" {
        if (by!="") { print "claimed_by: " by; print "claimed_at: " at }
        print; infm=0; next
      }
      infm && (/^claimed_by:/ || /^claimed_at:/) { next }
      { print }
    ' "$note" > "$tmp"
  elif head -1 "$note" | grep -q '^#'; then
    # Format 2 (project-note: `# Heading` + bare key:value frontmatter until first blank line).
    # Drop any prior claim lines inside the block, then append the fresh claim at the
    # END of the bare frontmatter (just before the first blank line). This keeps the
    # claim where check_projects.sh / resolve_done_notes.py read it (before blank line),
    # and does NOT inject a stray --- fence that would create duplicate frontmatter.
    awk -v by="$by" -v at="$at" '
      NR==1 { print; infm=1; next }
      infm && NF==0 {
        if (by!="") { print "claimed_by: " by; print "claimed_at: " at }
        infm=0; print; next
      }
      infm && (/^claimed_by:/ || /^claimed_at:/) { next }
      { print }
      END {
        # File with no trailing blank line: flush claim if still inside frontmatter.
        if (infm && by!="") { print "claimed_by: " by; print "claimed_at: " at }
      }
    ' "$note" > "$tmp"
  else
    # No recognizable frontmatter: create a minimal fenced one carrying just the claim.
    {
      echo "---"
      if [ -n "$by" ]; then echo "claimed_by: $by"; echo "claimed_at: $at"; fi
      echo "---"
      cat "$note"
    } > "$tmp"
  fi
  mv -f "$tmp" "$note"
}

# ── done_when prose-lint (WARN, non-blocking) ────────────────────────────────
# The project-notes skill requires every done_when to be LOCALLY CHECKABLE
# (file exists / grep / exit 0 / git rev-parse HEAD == @{u}). A NARRATIVE
# ("4 components shipped") done_when cannot be auto-verified by the Daily Note
# Review / Advancer janitors, so they fall back to conservative checkbox-reading
# and a completed note can sit un-resolved. This lint catches that AT CLAIM TIME.
# It only WARNS (never blocks) — matches the soft-warn contract: the human/agent
# MUST see it, but the claim still proceeds. Fires only for type: project notes.
lint_done_when() {
  # Extract the note type + the done_when value (handles both frontmatter formats:
  # fenced --- block, or `# Heading` + bare key:value until first blank line, and
  # also a `done_when:` that opens a YAML block scalar `>` spanning lines).
  local typ dw
  typ="$(awk '
    NR==1 && $0=="---" { infm=1; fenced=1; next }
    NR==1 && /^#/       { infm=1; next }
    NR==1 { exit }
    infm && fenced && $0=="---" { exit }
    infm && !fenced && NF==0 { exit }
    infm && /^type:/ { sub(/^type:[ \t]*/,""); print; exit }
  ' "$note")"
  [ "$typ" = "project" ] || return 0   # lint only applies to project notes

  # Grab the done_when value INCLUDING a `>`/`|` block scalar body (indented lines
  # following `done_when:` until a non-indented key or blank line).
  dw="$(awk '
    /^done_when:/ { grab=1; line=$0; sub(/^done_when:[ \t]*/,"",line); buf=line; next }
    grab==1 {
      if ($0 ~ /^[ \t]+/) { buf=buf " " $0; next }   # indented continuation
      else { exit }                                     # block ended
    }
    END { print buf }
  ' "$note")"

  # Runnable tokens that make a done_when machine-checkable.
  if printf '%s' "$dw" | grep -qiE 'git |grep|exit 0|test -f|rev-parse|@\{u\}|\bls \b|python3|bash |\[ -f|\[ -d|== *@|all steps \[x\]|steps? .*\[x\]'; then
    return 0   # has a runnable/verifiable check -> OK
  fi
  # No runnable token -> WARN (non-blocking).
  printf '\033[33m[warn]\033[0m claim_note: %s has a PROSE done_when (no runnable check).\n' "$note" >&2
  printf '        The Daily Note Review / Advancer janitors cannot auto-verify a\n' >&2
  printf '        narrative done_when and will fall back to checkbox-reading, so a\n' >&2
  printf '        completed note may not self-resolve. Rewrite done_when as a locally\n' >&2
  printf '        checkable condition, e.g.  git -C <repo> rev-parse HEAD == @{u}  or\n' >&2
  printf '        test -f <artifact>  or  grep -q <marker> <file>  (proceeding anyway).\n' >&2
  return 0
}

# Serialize per-note via flock if available (best-effort; degrades gracefully).
lockfile="${note}.claimlock"
run_locked() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$lockfile"
    flock 9
  fi
  sweep_stale_tmps
  "$@"
}

do_claim() {
  [ -n "$claimant" ] || die "claim/refresh require <claimant>"
  local parsed cur_by cur_at st
  parsed="$(read_claim)"; cur_by="${parsed%%$'\t'*}"; cur_at="${parsed#*$'\t'}"
  st="$(claim_state "$cur_by" "$cur_at" "$claimant")"
  case "$st" in
    held)
      echo "claim_note: BUSY — held by '$cur_by' since $cur_at (not yours)"; exit 3 ;;
    free|stale|mine)
      lint_done_when   # WARN (non-blocking) if a project note has a prose done_when
      write_claim "$claimant" "$(now_iso)"
      # verify our write stuck
      parsed="$(read_claim)"; cur_by="${parsed%%$'\t'*}"
      [ "$cur_by" = "$claimant" ] || die "claim write did not stick (got '$cur_by')"
      echo "claim_note: OK — '$note' claimed by '$claimant'"; exit 0 ;;
  esac
}

do_refresh() {
  [ -n "$claimant" ] || die "refresh requires <claimant>"
  local parsed cur_by cur_at
  parsed="$(read_claim)"; cur_by="${parsed%%$'\t'*}"; cur_at="${parsed#*$'\t'}"
  if [ "$cur_by" != "$claimant" ]; then
    echo "claim_note: cannot refresh — held by '$cur_by', not '$claimant'"; exit 3
  fi
  write_claim "$claimant" "$(now_iso)"
  echo "claim_note: OK — refreshed '$note' for '$claimant'"; exit 0
}

# Remove a leading frontmatter block that has become EMPTY (--- immediately
# followed by ---), so releasing a claim on a note that never had frontmatter
# leaves no stray empty block behind. Notes with real frontmatter keys are left
# untouched.
strip_empty_frontmatter() {
  if [ "$(sed -n '1p' "$note")" = "---" ] && [ "$(sed -n '2p' "$note")" = "---" ]; then
    local tmp; mk_tmp; tmp="$TMP_NEW"
    # drop lines 1-2 (the empty block) and a single following blank line if present
    awk 'NR<=2{next} NR==3 && $0==""{next} {print}' "$note" > "$tmp"
    mv -f "$tmp" "$note"
  fi
}

do_release() {
  write_claim "" ""
  strip_empty_frontmatter
  # Remove our own flock lockfile so a released note leaves no orphaned
  # <note>.claimlock behind (an empty stray lock can make the next Daily Note
  # Review re-report a phantom note). Best-effort; harmless if already gone.
  rm -f "$lockfile" 2>/dev/null || true
  echo "claim_note: OK — released '$note'"; exit 0
}

do_check() {
  local parsed cur_by cur_at st
  parsed="$(read_claim)"; cur_by="${parsed%%$'\t'*}"; cur_at="${parsed#*$'\t'}"
  st="$(claim_state "$cur_by" "$cur_at" "${claimant:-}")"
  case "$st" in
    free)  echo "free"; exit 0 ;;
    stale) echo "stale (was '$cur_by' @ $cur_at)"; exit 0 ;;
    mine)  echo "mine ('$cur_by')"; exit 0 ;;
    held)  echo "held by '$cur_by' @ $cur_at"; exit 3 ;;
  esac
}

case "$cmd" in
  claim)   run_locked do_claim ;;
  refresh) run_locked do_refresh ;;
  release) run_locked do_release ;;
  check)   do_check ;;
  *) die "unknown command '$cmd' (claim|refresh|release|check)" ;;
esac
