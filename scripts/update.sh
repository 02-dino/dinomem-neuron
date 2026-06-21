#!/usr/bin/env bash
# dinomem-neuron — update script
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pull latest from GitHub before updating
if [ -d "$REPO_DIR/.git" ]; then
  echo "Pulling latest dinomem-neuron from GitHub..."
  git -C "$REPO_DIR" pull --ff-only || echo "[warn] git pull failed — running update with local version"
else
  echo "[warn] Not a git repo — skipping pull, running update with local version"
fi

exec bash "$SCRIPT_DIR/install.sh" --force "$@"
