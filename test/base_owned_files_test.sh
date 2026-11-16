#!/usr/bin/env bash
# base_owned_files_test.sh — files that dinomem BASE owns must never ship from
# neuron again. WHY (2026-11-16): neuron's installer copies scripts/*.sh|*.py by
# glob AFTER base, so a neuron copy silently overwrites base's on every install.
# cron_gate.sh drifted that way (Pending Reminder + Auto Improve lanes went dead),
# and the Stall Rescuer would have been installed and registered twice. One owner,
# one writer: this test fails if a base-owned file or a second registration of a
# base-owned cron comes back.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rc=0
for f in scripts/cron_gate.sh scripts/stall_watch.py scripts/check_stalls.sh \
         templates/stall_rescuer_prompt.md test/stall_watch_test.sh; do
  if [ -e "$R/$f" ]; then echo "FAIL neuron ships base-owned $f"; rc=1; else echo "PASS $f not shipped"; fi
done
# The installer may NAME the job in comments, but must never register it.
if grep -nE '"name": *"Stall Rescuer"|GATE_STALL_RESCUER_ID"\)' "$R/scripts/install.sh" >/dev/null; then
  echo "FAIL scripts/install.sh registers Stall Rescuer (base owns it)"; rc=1
else
  echo "PASS install.sh does not register Stall Rescuer"
fi
exit $rc
