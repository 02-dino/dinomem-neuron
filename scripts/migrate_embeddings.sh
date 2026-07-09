#!/usr/bin/env bash
# dinomem-neuron — safe one-shot migration of vector stores to a new embedding model.
#
# WHY: when the embedding model changes (tools/_embed.py default or DINOMEM_EMBED_MODEL),
# every EXISTING Chroma store is stale — its vectors came from the OLD model and will
# silently misalign with new-model query vectors. This script migrates an existing
# install to the current model SAFELY and IDEMPOTENTLY, then hands the actual rebuild
# to scripts/reindex_all.sh (single source of rebuild+gate logic — not duplicated here).
#
# SAFETY MODEL (nothing is ever destroyed without an explicit opt-in):
#   • Idempotency marker  kb/.embed_model  records the model the stores are built with.
#       marker == current model  ->  nothing to do, exit 0.
#   • Disk preflight: refuse to start if free disk can't hold a backup copy of the stores.
#   • Backup-BY-RENAME: each store dir is RENAMED to <dir>.bak-<timestamp> (mv, never rm),
#       so a failed rebuild is fully recoverable. Backups are KEPT by default.
#   • Delegated rebuild: reindex_all.sh does the sequential rebuild + per-store query gate.
#       If it fails, we RESTORE the .bak dirs (mv back) and abort loud — the install is
#       left exactly as it was found.
#   • Resumable: a re-run after a crash sees the marker still on the OLD model and existing
#       .bak dirs; it will NOT clobber a good backup (aborts and tells you to resolve).
#   • Purge is OPT-IN ONLY: --purge-backups deletes .bak dirs AFTER a verified-good migrate.
#       Default keeps them; nothing here ever auto-deletes.
#
# Stores migrated (Chroma / embeddings only — DuckDB kb/data_store/data.duckdb is exact-SQL,
# has NO embeddings, and is NEVER touched):
#   kb/vector_db_docs, kb/vector_db_sessions, and kb/data_store/text_index (prose indexes)
#
# Usage:
#   bash scripts/migrate_embeddings.sh            # migrate to current model (safe)
#   bash scripts/migrate_embeddings.sh --dry-run  # print the plan, touch nothing
#   bash scripts/migrate_embeddings.sh --purge-backups   # migrate, then delete .bak on success
#   DINOMEM_EMBED_MODEL=<id> bash scripts/migrate_embeddings.sh
#
# Exit: 0 = migrated (or already current / nothing to migrate); non-zero = aborted, stores
#       restored to the pre-run state.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO" || { echo "migrate_embeddings: cannot cd to repo root ($REPO)"; exit 1; }

PY="${PYTHON:-python3}"
MARKER="kb/.embed_model"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DRY_RUN=0
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)        DRY_RUN=1 ;;
    --purge-backups)  PURGE=1 ;;
    -h|--help)
      sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "migrate_embeddings: unknown arg '$arg' (use --dry-run / --purge-backups)"; exit 2 ;;
  esac
done

log()  { echo "[migrate_embeddings] $*"; }
warn() { echo "[migrate_embeddings] WARN: $*" >&2; }
err()  { echo "[migrate_embeddings] ERROR: $*" >&2; }

# ── resolve the current model id from the single source of truth (tools/_embed.py) ──
CURRENT_MODEL="$("$PY" -c 'import sys; sys.path.insert(0,"tools"); import _embed; print(_embed.model_id())' 2>/dev/null)"
if [ -z "$CURRENT_MODEL" ]; then
  err "could not resolve current embedding model id from tools/_embed.py (is the embed stack importable?)."
  exit 1
fi
log "current embedding model = $CURRENT_MODEL"

# ── read the marker (model the stores are currently built with) ──
OLD_MODEL=""
[ -f "$MARKER" ] && OLD_MODEL="$(tr -d '[:space:]' < "$MARKER")"
if [ -n "$OLD_MODEL" ]; then
  log "existing stores built with = $OLD_MODEL (from $MARKER)"
else
  log "no $MARKER marker found — stores' model is UNKNOWN (first migrate, or pre-marker install)"
fi

# ── idempotency: already on the current model -> nothing to do ──
if [ -n "$OLD_MODEL" ] && [ "$OLD_MODEL" = "$CURRENT_MODEL" ]; then
  log "marker already == current model. Nothing to migrate."
  if [ "$PURGE" -eq 1 ]; then
    log "(--purge-backups) cleaning any leftover *.bak-* store backups…"
    if [ "$DRY_RUN" -eq 1 ]; then
      log "DRY-RUN: would rm -rf kb/*.bak-* kb/data_store/*.bak-* (if any)"
    else
      rm -rf kb/vector_db_docs.bak-* kb/vector_db_sessions.bak-* kb/data_store/text_index.bak-* 2>/dev/null
      log "leftover backups purged."
    fi
  fi
  exit 0
fi

# ── enumerate stores that actually exist on disk ──
STORES=()
[ -d "kb/vector_db_docs" ]          && STORES+=("kb/vector_db_docs")
[ -d "kb/vector_db_sessions" ]      && STORES+=("kb/vector_db_sessions")
[ -d "kb/data_store/text_index" ]   && STORES+=("kb/data_store/text_index")

if [ "${#STORES[@]}" -eq 0 ]; then
  log "no existing vector stores on disk — nothing to back up. Will just (re)build + stamp marker."
fi

# ── guard: refuse to clobber an existing good backup (resumability safety) ──
for s in "${STORES[@]}"; do
  if compgen -G "${s}.bak-*" > /dev/null; then
    err "a previous backup already exists for '$s' (${s}.bak-*)."
    err "This looks like an interrupted earlier migration. Resolve it first:"
    err "  - if the current store is good: remove the stale backup (rm -rf ${s}.bak-*)"
    err "  - if the current store is broken: restore it (mv ${s}.bak-* ${s}) then re-run"
    err "Refusing to overwrite a backup. Aborting (no changes made)."
    exit 1
  fi
done

# ── disk preflight: need room to hold a backup copy of the stores ──
NEED_KB=0
for s in "${STORES[@]}"; do
  sz="$(du -sk "$s" 2>/dev/null | awk '{print $1}')"
  NEED_KB=$(( NEED_KB + ${sz:-0} ))
done
FREE_KB="$(df -Pk "$REPO" | awk 'NR==2{print $4}')"
# rename-backup doesn't consume extra disk for the backup itself, but the rebuild writes a
# fresh store alongside the (renamed) old one, so budget ~1x store size + 20% headroom.
BUDGET_KB=$(( NEED_KB + NEED_KB/5 + 51200 ))   # +20% +50MB floor
log "disk preflight: stores ~${NEED_KB} KB, free ~${FREE_KB} KB, budget ~${BUDGET_KB} KB"
if [ "${FREE_KB:-0}" -lt "$BUDGET_KB" ]; then
  err "insufficient free disk: need ~${BUDGET_KB} KB, have ${FREE_KB} KB. Aborting (no changes)."
  exit 1
fi

# ── PLAN summary ──
log "──────── MIGRATION PLAN ────────"
log "from model : ${OLD_MODEL:-<unknown>}"
log "to   model : $CURRENT_MODEL"
log "stores     : ${STORES[*]:-<none on disk>}"
for s in "${STORES[@]}"; do log "  backup   : $s  ->  ${s}.bak-${STAMP}"; done
log "rebuild    : scripts/reindex_all.sh (sequential + per-store query gate)"
log "on failure : restore backups (mv back), abort, no marker write"
log "backups    : KEPT ($([ "$PURGE" -eq 1 ] && echo 'will purge on success (--purge-backups)' || echo 'use --purge-backups to delete after success'))"
log "marker     : write '$CURRENT_MODEL' -> $MARKER on success"
log "────────────────────────────────"

if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY-RUN: no stores touched, no marker written. Re-run without --dry-run to execute."
  exit 0
fi

# ── backup-by-rename (mv, never rm) ──
BACKED_UP=()
for s in "${STORES[@]}"; do
  if mv "$s" "${s}.bak-${STAMP}"; then
    BACKED_UP+=("$s")
    log "backed up: $s -> ${s}.bak-${STAMP}"
  else
    err "failed to back up '$s' (mv). Restoring any prior backups and aborting."
    for b in "${BACKED_UP[@]}"; do mv "${b}.bak-${STAMP}" "$b" 2>/dev/null && log "restored $b"; done
    exit 1
  fi
done

# ── rebuild via the shared reindex script (single source of rebuild+gate logic) ──
log "rebuilding all stores with the current model via scripts/reindex_all.sh …"
if bash scripts/reindex_all.sh; then
  log "reindex_all succeeded (all attempted stores rebuilt + passed their gate)."
else
  err "reindex_all FAILED. Restoring pre-migration stores from backup …"
  for s in "${BACKED_UP[@]}"; do
    rm -rf "$s" 2>/dev/null           # remove the partially-built new store
    if mv "${s}.bak-${STAMP}" "$s"; then
      log "restored: $s (from ${s}.bak-${STAMP})"
    else
      err "COULD NOT restore '$s' from '${s}.bak-${STAMP}' — MANUAL recovery needed (backup is intact on disk)."
    fi
  done
  err "migration aborted; install restored to pre-run state. Marker NOT written."
  exit 1
fi

# ── success: write the marker ──
mkdir -p kb
echo "$CURRENT_MODEL" > "$MARKER"
log "marker updated: $MARKER = $CURRENT_MODEL"

# ── optional purge (opt-in only) ──
if [ "$PURGE" -eq 1 ]; then
  log "(--purge-backups) deleting backups after verified-good migration …"
  for s in "${BACKED_UP[@]}"; do
    rm -rf "${s}.bak-${STAMP}" && log "purged ${s}.bak-${STAMP}"
  done
else
  if [ "${#BACKED_UP[@]}" -gt 0 ]; then
    log "backups KEPT (${#BACKED_UP[@]}): *.bak-${STAMP}. Delete with --purge-backups once you're happy."
  fi
fi

log "MIGRATION COMPLETE — stores now on '$CURRENT_MODEL'."
exit 0
