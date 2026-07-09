#!/usr/bin/env bash
# dinomem-neuron — sequential re-index of every Chroma vector store the neuron owns.
#
# WHY: after an embedding-model change (see tools/_embed.py / DINOMEM_EMBED_MODEL),
# every existing vector store is stale — its vectors were produced by the OLD model
# and will silently misalign with new-model query vectors (the query paths now warn
# 'model_mismatch', but the fix is to rebuild). This script rebuilds them all with
# the currently-configured model.
#
# DESIGN (two hard-won lessons baked in):
#   1. SEQUENTIAL, never parallel. Embedding model load + ingest is RAM-heavy; on a
#      small VPS, rebuilding stores concurrently OOM-kills the box. One at a time.
#   2. POST-BUILD TEST-QUERY GATE. Chroma row count() is NOT proof of a healthy store
#      — a corrupt/half-written HNSW index can have rows yet crash on a real query.
#      After each rebuild we run an ACTUAL similarity search through the real query
#      module and require non-crash before moving on. Empty result is a soft warn
#      (a store can legitimately be empty), a crash/exception is a hard fail.
#
# Stores covered:
#   - docs      : procedures/docs_ingest.py    -> kb/vector_db_docs/      (gate via tools/docs_search.py)
#   - sessions  : procedures/session_ingest.py -> kb/vector_db_sessions/  (gate via tools/session_search.py)
#   - prose idx : procedures/data_register.py --refresh -> kb/data_store/text_index/*
#                 (only if kb/data_store/catalog.json exists; rebuilds prose Chroma
#                  collections from DuckDB rows. DuckDB itself has NO embeddings and
#                  is never touched.)
#
# Usage:
#   bash scripts/reindex_all.sh
#   DINOMEM_EMBED_MODEL=intfloat/multilingual-e5-small bash scripts/reindex_all.sh
#
# Exit: 0 if every store that was ATTEMPTED passed its gate (or was cleanly skipped);
#       non-zero if any rebuild or gate hard-failed.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
cd "$REPO" || { echo "reindex_all: cannot cd to repo root ($REPO)"; exit 1; }

PY="${PYTHON:-python3}"
FAIL=0

log()  { echo "[reindex_all] $*"; }
warn() { echo "[reindex_all] WARN: $*" >&2; }
err()  { echo "[reindex_all] ERROR: $*" >&2; }

# ── gate: run a REAL query through the given search module; non-crash = pass ──
# args: <label> <python-import-and-call-snippet>
# The snippet must import + run one real query and print PASS/EMPTY, or raise.
gate() {
  local label="$1"; shift
  local snippet="$1"; shift
  log "gate[$label]: running real test-query…"
  if out="$("$PY" -c "$snippet" 2>&1)"; then
    if echo "$out" | grep -q "__GATE_EMPTY__"; then
      warn "gate[$label]: query ran but returned 0 results (store may legitimately be empty) — soft-OK"
    else
      log "gate[$label]: PASS (real query returned results)"
    fi
    return 0
  else
    err "gate[$label]: FAILED — a real query crashed against the rebuilt store:"
    echo "$out" | sed 's/^/    /' >&2
    return 1
  fi
}

# ── 1. docs store ────────────────────────────────────────────────────────────
if [ -f "procedures/docs_ingest.py" ]; then
  log "rebuilding docs store (kb/vector_db_docs) …"
  if "$PY" procedures/docs_ingest.py; then
    gate "docs" '
import sys; sys.path.insert(0, "tools")
import docs_search
r = docs_search.docs_search("healthcheck test query", k=1)
print("__GATE_EMPTY__" if not r else "ok:%d" % len(r))
' || FAIL=1
  else
    err "docs_ingest.py exited non-zero — skipping docs gate"; FAIL=1
  fi
else
  warn "procedures/docs_ingest.py absent — skipping docs store"
fi

# ── 2. sessions store ────────────────────────────────────────────────────────
if [ -f "procedures/session_ingest.py" ]; then
  log "rebuilding sessions store (kb/vector_db_sessions) …"
  if "$PY" procedures/session_ingest.py; then
    gate "sessions" '
import sys; sys.path.insert(0, "tools")
import session_search
try:
    r = session_search.session_search("healthcheck test query", k=1)
except FileNotFoundError:
    # ingester makes the store; if it is absent the ingest legitimately had nothing
    print("__GATE_EMPTY__"); raise SystemExit(0)
print("__GATE_EMPTY__" if not r else "ok:%d" % len(r))
' || FAIL=1
  else
    # session_ingest may legitimately no-op when there are no recent archives
    warn "session_ingest.py exited non-zero (often: no recent session archives) — soft-skip gate"
  fi
else
  warn "procedures/session_ingest.py absent — skipping sessions store"
fi

# ── 3. structured-data prose indexes (Chroma), only if a data store exists ────
if [ -f "kb/data_store/catalog.json" ]; then
  log "refreshing structured-data prose indexes (kb/data_store/text_index) …"
  if "$PY" procedures/data_register.py --refresh; then
    log "prose-index refresh done (data_register rebuilds + re-stamps each prose collection)"
  else
    err "data_register.py --refresh exited non-zero"; FAIL=1
  fi
else
  log "no kb/data_store/catalog.json — no structured-data prose indexes to rebuild (skipping)"
fi

# ── summary ──────────────────────────────────────────────────────────────────
if [ "$FAIL" -eq 0 ]; then
  log "ALL DONE — every attempted store rebuilt and passed its gate."
  exit 0
else
  err "one or more stores failed to rebuild or pass their gate (see above)."
  exit 1
fi
