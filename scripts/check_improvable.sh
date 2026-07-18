#!/usr/bin/env bash
# check_improvable.sh — zero-LLM pre-gate for COMPONENT 2 (the project improver).
#
# Exit 0  => at least one project note is status: done, NOT verified, and not freshly
#            claimed by another job → Component 2 has work; the LLM turn should run.
# Exit 1  => nothing to improve → Component 2 must stop immediately, no LLM cost.
#
# A note qualifies when ALL of:
#   - has  type: project
#   - has  status: done
#   - does NOT have  verified: true   (C2 stamps this once it's improved+validated)
#   - is not FRESHLY claimed by another job (live-session <120m, other <30m). Stale = ok.
#     claimed_at is ALWAYS UTC ISO8601. Windows MUST match Advancer + C1 + C2 exactly.
#
# Portable: workspace from $OPENCLAW_WORKSPACE (install.sh sets it per-installer;
# generic fallback for any host).
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
MEM="$WS/memory"
[ -d "$MEM" ] || exit 1

now_epoch=$(date -u +%s)

for f in "$MEM"/_note_*.md; do
  [ -e "$f" ] || continue
  # Scan anchored fields (no leading whitespace, i.e. real frontmatter/top-level
  # lines, not indented prose/steps) across the WHOLE FILE rather than a fixed
  # head -n 15 or a "stop at first blank line" heuristic. Both are unsafe here:
  # claim_note.sh writes claimed_by/claimed_at at the END of a note's bare
  # frontmatter, which can land well past line 15 on notes with long
  # steps:/resume_state: blocks (same bug class fixed in note-claim-refresh
  # 2026-07-10) -- AND some project notes have legitimate blank lines inside
  # free-form addendum sections BEFORE the real claim lines, so "first blank
  # line" also under-scans. Anchored (^field:) grep on the whole file is safe:
  # these bare tokens only ever appear once, at column 0, in real frontmatter --
  # prose/steps text never starts a line with them verbatim.

  grep -qE '^type:\s*project\s*$' "$f"   || continue
  grep -qE '^status:\s*done\s*$' "$f"    || continue
  # already verified → not improvable (Component 1 will delete it)
  grep -qE '^verified:\s*true\s*$' "$f"  && continue

  # fresh-claim check: skip if another job is actively working it
  claimed_by=$(grep -E '^claimed_by:' "$f" | tail -n1 | sed -E 's/^claimed_by:\s*//' | tr -d '\r' | xargs || true)
  claimed_at=$(grep -E '^claimed_at:' "$f" | tail -n1 | sed -E 's/^claimed_at:\s*//' | tr -d '\r' | xargs || true)
  if [ -n "$claimed_by" ] && [ -n "$claimed_at" ]; then
    if claim_epoch=$(date -u -d "$claimed_at" +%s 2>/dev/null); then
      age=$(( now_epoch - claim_epoch ))
      case "$claimed_by" in
        live-session*) window=7200 ;;   # 120 min (2h) live-session grace
        *)             window=1800 ;;    # 30 min other-claimant grace
      esac
      # fresh claim → another job owns it now → not our work this run
      [ "$age" -lt "$window" ] && continue
    fi
  fi

  # found an improvable note
  exit 0
done

exit 1
