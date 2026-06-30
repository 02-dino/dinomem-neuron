#!/usr/bin/env bash
# dinomem-neuron — update script
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Update path depends on how this copy was delivered.
if [ -d "$REPO_DIR/.git" ]; then
  # Git install (maintainer / collaborator): pull latest before re-running install.
  echo "Pulling latest dinomem-neuron from GitHub..."
  git -C "$REPO_DIR" pull --ff-only || echo "[warn] git pull failed — running update with local version"
else
  # Zip install (buyer copy): no git remote. Updates are delivered by the seller.
  echo "This is a zip install (no git remote)."
  echo "Updates for dinomem-neuron are delivered directly by the seller."
  echo "To get the latest version, contact: https://t.me/dinotlgrm"
  echo ""
  echo "Re-applying current local version..."
fi

exec bash "$SCRIPT_DIR/install.sh" --force "$@"
