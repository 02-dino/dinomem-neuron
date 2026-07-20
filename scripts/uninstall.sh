#!/usr/bin/env bash
# dinomem-neuron — uninstall script
# Removes: cron jobs added by neuron.
# Does NOT touch dinomem base config or memory data, UNLESS --with-base is passed.
#
# Usage:
#   bash scripts/uninstall.sh --workspace DIR
#   bash scripts/uninstall.sh --workspace DIR --purge          # also remove scripts
#   bash scripts/uninstall.sh --workspace DIR --purge-data     # remove kb/, topics/ (NOT memory)
#   bash scripts/uninstall.sh --workspace DIR --purge-memory   # ⚠️  WIPES memory/_permanent.md — irreversible
#   bash scripts/uninstall.sh --workspace DIR --with-base      # ALSO uninstall the dinomem base (neuron is an upgrade layer on top of it)
#   bash scripts/uninstall.sh --workspace DIR --force          # skip all confirmation prompts (for AI agent use)
#
# --with-base: neuron install auto-installs the base first, so a full teardown of
# neuron may reasonably want the base gone too. Opt-in (not default) so users who
# still want plain dinomem keep it. Any --purge/--purge-data/--purge-memory/--force
# flags are forwarded to the base uninstaller.
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
PURGE=0
PURGE_DATA=0
PURGE_MEMORY=0
WITH_BASE=0
FORCE=0
DINOMEM_BASE_REPO="${DINOMEM_BASE_REPO:-https://github.com/02-dino/dinomem}"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)     WS="$2"; shift 2 ;;
    --agent-id)      AGENT_ID="$2"; shift 2 ;;
    --purge)         PURGE=1; shift ;;
    --purge-data)    PURGE_DATA=1; shift ;;
    --purge-memory)  PURGE_MEMORY=1; shift ;;
    --with-base)     WITH_BASE=1; shift ;;
    --force)         FORCE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

OPENCLAW_DIR="$(dirname "$WS")"

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

echo; hr "dinomem-neuron uninstall -> $WS"

# ── Cron jobs ─────────────────────────────────────────────────────────────────
hr "Cron jobs"
CRON_PATTERNS=(
  "memory_graph.py"
  "memory_synthesis.py"
  "contradiction_check.py"
  "memory_promote.py"
  "generate_topic_index.py"
  "docs_ingest.py"
  "calendar_sync.py"
  "dinomem-neuron:"
)
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CURRENT_CRON"
for pattern in "${CRON_PATTERNS[@]}"; do
  if echo "$NEW_CRON" | grep -qF "$pattern"; then
    NEW_CRON="$(echo "$NEW_CRON" | grep -vF "$pattern")"
    ok "removed cron: $pattern"
  else
    skip "cron not found: $pattern"
  fi
done
echo "$NEW_CRON" | crontab - 2>/dev/null || true

# ── Purge scripts (optional) ──────────────────────────────────────────────────
if [ "$PURGE" = 1 ]; then
  hr "Purge scripts"
  for f in \
    procedures/memory_graph.py \
    procedures/memory_synthesis.py \
    procedures/memory_promote.py \
    procedures/contradiction_check.py \
    procedures/generate_topic_index.py \
    procedures/docs_ingest.py \
    procedures/calendar_sync.py \
    tools/docs_search.py \
    tools/cache_utils.py \
    tools/calendar_tool.py \
    scripts/calendar_oauth_setup.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done

  # Skills: remove neuron-EXCLUSIVE skills always. The shared-name skill
  # (memory-pinning) is a neuron SUPERSET that overwrote base's copy, so only
  # remove it when --with-base is set (otherwise leave it — base still uses it,
  # and base's uninstaller owns its removal). Same principle for the
  # dinomem-open-notes hook below.
  for _sk in project-notes retrieval-routing note-claim; do
    [ -d "$WS/skills/$_sk" ] && rm -rf "$WS/skills/$_sk" && ok "removed skills/$_sk/" || skip "skills/$_sk/ not found"
  done
  if [ "$WITH_BASE" = 1 ]; then
    [ -d "$WS/skills/memory-pinning" ] && rm -rf "$WS/skills/memory-pinning" && ok "removed skills/memory-pinning/ (shared with base, --with-base)" || skip "skills/memory-pinning/ not found"
  else
    [ -d "$WS/skills/memory-pinning" ] && skip "skills/memory-pinning/ preserved (shared with base; use --with-base to remove)"
  fi

  # dinomem-open-notes hook: neuron's SUPERSET overwrote base's. Only remove it
  # when --with-base (base still relies on the hook otherwise). Disable before
  # removing so OpenClaw drops the registration cleanly.
  if [ "$WITH_BASE" = 1 ] && [ -d "$WS/hooks/dinomem-open-notes" ]; then
    if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
      openclaw hooks disable dinomem-open-notes >/dev/null 2>&1 \
        && ok "dinomem-open-notes hook disabled" \
        || warn "could not disable dinomem-open-notes hook (remove manually if needed)"
    fi
    rm -rf "$WS/hooks/dinomem-open-notes" && ok "removed hooks/dinomem-open-notes/ (shared with base, --with-base)" || warn "could not remove hooks/dinomem-open-notes/"
  elif [ -d "$WS/hooks/dinomem-open-notes" ]; then
    skip "hooks/dinomem-open-notes/ preserved (shared with base; use --with-base to remove)"
  fi
fi

# ── Purge data (optional, explicit) ───────────────────────────────────────────
if [ "$PURGE_DATA" = 1 ]; then
  hr "Purge data"
  warn "This will permanently delete neuron knowledge data."
  if [ "$FORCE" = 1 ]; then
    confirm="yes"
  else
    read -r -p "  Type 'yes' to confirm: " confirm
  fi
  if [ "$confirm" = "yes" ]; then
    [ -d "$WS/kb/memory_neuron" ]  && rm -rf "$WS/kb/memory_neuron"  && ok "removed kb/memory_neuron/"
    [ -d "$WS/topics" ]            && rm -rf "$WS/topics"            && ok "removed topics/"
    [ -d "$WS/kb/vector_db_docs" ] && rm -rf "$WS/kb/vector_db_docs" && ok "removed kb/vector_db_docs/"
    [ -d "$WS/docs" ]              && warn "docs/ preserved — delete manually if needed"
    warn "memory/_permanent.md preserved — use --purge-memory to wipe it"
  else
    skip "purge-data cancelled"
  fi
fi

# ── Purge memory (explicit, irreversible) ───────────────────────────────────
if [ "$PURGE_MEMORY" = 1 ]; then
  hr "Purge memory"
  printf '  \033[31m[warn]\033[0m ⚠️  This will PERMANENTLY DELETE promoted memory:\n'
  printf '         memory/_permanent.md\n'
  printf '         This cannot be undone.\n'
  if [ "$FORCE" = 1 ]; then
    confirm="wipe memory"
  else
    read -r -p "  Type 'wipe memory' to confirm: " confirm
  fi
  if [ "$confirm" = "wipe memory" ]; then
    [ -f "$WS/memory/_permanent.md" ] && rm "$WS/memory/_permanent.md" && ok "removed memory/_permanent.md" || skip "memory/_permanent.md not found"
  else
    skip "purge-memory cancelled"
  fi
fi

# ── AGENTS.md block ───────────────────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
BEGIN="<!-- BEGIN:dinomem-neuron (managed) -->"
END="<!-- END:dinomem-neuron -->"
if [ -f "$AGENTS" ] && grep -qF "$BEGIN" "$AGENTS"; then
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} $0==e{skip=0; next} !skip{print}' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
  ok "AGENTS.md neuron block removed"
else
  skip "AGENTS.md neuron block not found"
fi

# ── note-claim-refresh hook plugin ────────────────────────────────────
hr "Note-claim-refresh plugin"
PLUGIN_DST="$WS/plugins/note-claim-refresh"
if [ -d "$PLUGIN_DST" ]; then
  rm -rf "$PLUGIN_DST" && ok "removed $PLUGIN_DST" || warn "could not remove $PLUGIN_DST"
else
  skip "plugin dir not found: $PLUGIN_DST"
fi
OPENCLAW_JSON="${OPENCLAW_CONFIG:-$HOME/.openclaw/openclaw.json}"
[ -f "$OPENCLAW_JSON" ] || OPENCLAW_JSON="$OPENCLAW_DIR/openclaw.json"
if [ -f "$OPENCLAW_JSON" ]; then
  DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_PLUGIN_DST="$PLUGIN_DST" python3 - <<'PYEOF'
import json, os
path = os.environ["DINOMEM_OPENCLAW_JSON"]
plugin_dst = os.environ["DINOMEM_PLUGIN_DST"]
PID = "note-claim-refresh"
with open(path) as f:
    cfg = json.load(f)
changed = []
plugins = cfg.get("plugins", {})
allow = plugins.get("allow")
if isinstance(allow, list) and PID in allow:
    allow.remove(PID); changed.append("plugins.allow -= " + PID)
load = plugins.get("load", {})
paths = load.get("paths")
if isinstance(paths, list) and plugin_dst in paths:
    paths.remove(plugin_dst); changed.append("plugins.load.paths -= " + plugin_dst)
pconf = plugins.get("config")
if isinstance(pconf, dict) and PID in pconf:
    del pconf[PID]; changed.append("plugins.config." + PID + " removed")
if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print("  \033[32m[ok]\033[0m   unwired: " + c)
    print("  \033[33m[warn]\033[0m Restart OpenClaw to unload the plugin: openclaw gateway restart")
else:
    print("  \033[33m[skip]\033[0m note-claim-refresh not wired in openclaw.json")
PYEOF
else
  skip "openclaw.json not found — plugin config left as-is"
fi

# ── OpenClaw crons (gate + all neuron-registered workers) ──────────────────
# Neuron registers OpenClaw crons (NOT crontab lines): the project-executor trio
# (Advancer / Improver / Deleter) plus the shared "Note Cron Gate" and its Daily/
# Pending worker lanes. The crontab-pattern removal above never touches these, so
# remove them here BY NAME. Exact case-insensitive allowlist so unrelated user
# crons are never removed.
#
# Gate handling: if --with-base, we remove the gate here (whole stack going away).
# Without --with-base the base survives, and the base's Daily/Pending lanes share
# the same "Note Cron Gate" — so we LEAVE the gate + base lanes and remove ONLY
# neuron's exclusive trio, so base keeps working. (Legacy "Project Cron Gate" is
# always removed — it's neuron-exclusive and deprecated.)
hr "OpenClaw crons"
DINOMEM_WITH_BASE="$WITH_BASE" python3 - <<PYEOF
import subprocess, json, os
with_base = os.environ.get("DINOMEM_WITH_BASE", "0") == "1"
# Neuron-EXCLUSIVE worker crons (always safe to remove on a neuron uninstall).
NEURON_EXCLUSIVE = {
    "project advancer",
    "project improver (component 2)",
    "verified note deleter (component 1)",
    "project cron gate",   # legacy deprecated neuron gate name — always remove
}
# Shared with base — only remove when the base is going too (--with-base).
SHARED_WITH_BASE = {
    "note cron gate",
    "daily note review",
    "pending note reminder",
}
targets_names = set(NEURON_EXCLUSIVE)
if with_base:
    targets_names |= SHARED_WITH_BASE
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    job_list = jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])
    targets = [j for j in job_list if (j.get('name','') or '').strip().lower() in targets_names]
    if not targets:
        print("  \033[33m[skip]\033[0m no neuron OpenClaw crons found")
    else:
        for j in targets:
            jid = j.get('id') or j.get('jobId')
            rr = subprocess.run(['openclaw', 'cron', 'remove', jid], capture_output=True, text=True, timeout=10)
            if rr.returncode == 0:
                print(f"  \033[32m[ok]\033[0m   removed OpenClaw cron: {j.get('name')} ({jid})")
            else:
                print(f"  \033[33m[warn]\033[0m could not remove {j.get('name')} ({jid}): {rr.stderr.strip()[:120]}")
    if not with_base:
        print("  \033[33m[note]\033[0m 'Note Cron Gate' + base lanes preserved (base still installed; use --with-base to remove).")
except Exception as e:
    print(f"  \033[33m[warn]\033[0m Could not remove OpenClaw crons: {e}")
PYEOF

# -- Base uninstall (opt-in) --------------------------------------------------
if [ "$WITH_BASE" = 1 ]; then
  hr "dinomem base uninstall (--with-base)"
  NEURON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  BASE_UNINSTALL=""
  for cand in "$OPENCLAW_DIR/dinomem/scripts/uninstall.sh" "$(dirname "$NEURON_DIR")/dinomem/scripts/uninstall.sh"; do
    [ -f "$cand" ] && { BASE_UNINSTALL="$cand"; break; }
  done
  if [ -z "$BASE_UNINSTALL" ] && command -v git >/dev/null 2>&1; then
    clone_dir="$OPENCLAW_DIR/dinomem"
    if [ ! -e "$clone_dir" ]; then
      git clone --depth 1 "$DINOMEM_BASE_REPO" "$clone_dir" >/dev/null 2>&1 \
        && ok "cloned base repo for its uninstaller -> $clone_dir" || warn "could not clone base repo"
    fi
    [ -f "$clone_dir/scripts/uninstall.sh" ] && BASE_UNINSTALL="$clone_dir/scripts/uninstall.sh"
  fi
  if [ -n "$BASE_UNINSTALL" ]; then
    base_args=(--workspace "$WS")
    [ -n "$AGENT_ID" ] && base_args+=(--agent-id "$AGENT_ID")
    [ "$PURGE" = 1 ]        && base_args+=(--purge)
    [ "$PURGE_DATA" = 1 ]   && base_args+=(--purge-data)
    [ "$PURGE_MEMORY" = 1 ] && base_args+=(--purge-memory)
    [ "$FORCE" = 1 ]        && base_args+=(--force)
    ok "running dinomem base uninstaller: $BASE_UNINSTALL"
    bash "$BASE_UNINSTALL" "${base_args[@]}" || warn "base uninstaller exited nonzero"
  else
    warn "base uninstaller not found (looked in $OPENCLAW_DIR/dinomem) -- base left as-is"
  fi
fi

echo; hr "done"
if [ "$WITH_BASE" = 1 ]; then
  echo "  dinomem neuron + base removed. Run 'openclaw gateway restart' if needed."
else
  echo "  dinomem base remains installed (pass --with-base to remove it too). Run 'openclaw gateway restart' if needed."
fi
