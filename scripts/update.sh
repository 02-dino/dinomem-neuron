#!/usr/bin/env bash
# dinomem-neuron — update script
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Update path depends on how this copy was delivered.
if [ -d "$REPO_DIR/.git" ]; then
  # Git clone: pull latest before re-running install.
  echo "Pulling latest dinomem-neuron from GitHub..."
  git -C "$REPO_DIR" pull --ff-only || echo "[warn] git pull failed — running update with local version"
else
  # No git metadata (e.g. downloaded archive): can't pull, re-apply local copy.
  echo "No git checkout here — re-applying current local version."
  echo "For the latest: git clone https://github.com/02-dino/dinomem-neuron"
fi

exec bash "$SCRIPT_DIR/install.sh" --force "$@"
