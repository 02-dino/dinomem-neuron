#!/usr/bin/env bash
# check_projects.sh — pre-check before Project Advancer LLM call
# Exits 0 if there are in_progress project notes (LLM should run)
# Exits 1 if nothing to do (skip LLM)
WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace-analyst}"
grep -rl "type: project" "$WS/memory/_note_"*.md 2>/dev/null | xargs grep -l "status: in_progress" 2>/dev/null | grep -q . && exit 0 || exit 1
