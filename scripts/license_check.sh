#!/usr/bin/env bash
# dinomem-neuron — soft license expiry check
#
# Reads .license_expiry (present only on time-limited builds) and refuses if the
# license term has ended. This is a SOFT, client-side check for honest
# contract-end enforcement — it is intentionally simple and not anti-piracy.
#
# Exit codes: 0 = valid / perpetual, 1 = expired.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXP_FILE="$REPO_DIR/.license_expiry"

# Perpetual license (no expiry file) — always valid.
[ -f "$EXP_FILE" ] || { exit 0; }

EXPIRES="$(grep -E '^expires=' "$EXP_FILE" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
LICENSEE="$(grep -E '^licensee=' "$EXP_FILE" | head -1 | cut -d= -f2 | tr -d '[:space:]')"

[ -n "$EXPIRES" ] || { exit 0; }

TODAY="$(date -u +%Y-%m-%d)"
EXP_EPOCH="$(date -u -d "$EXPIRES" +%s 2>/dev/null || echo 0)"
TODAY_EPOCH="$(date -u -d "$TODAY" +%s 2>/dev/null || echo 0)"

if [ "$EXP_EPOCH" -ne 0 ] && [ "$TODAY_EPOCH" -gt "$EXP_EPOCH" ]; then
  echo "" >&2
  echo "  ────────────────────────────────────────────────────────────" >&2
  echo "  dinomem-neuron LICENSE EXPIRED" >&2
  echo "  ────────────────────────────────────────────────────────────" >&2
  echo "  This license (licensee: ${LICENSEE:-unknown}) ended on $EXPIRES." >&2
  echo "  To renew and continue using dinomem-neuron, contact the seller:" >&2
  echo "  https://t.me/dinotlgrm" >&2
  echo "" >&2
  exit 1
fi

exit 0
