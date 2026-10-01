#!/usr/bin/env bash
# gate_refire_guard_test.sh — floor TEST for scripts/_gate_refire_guard.sh.
#
# Runnable standalone (from any cwd):  bash test/gate_refire_guard_test.sh
# Prints PASS/FAIL per test; exits non-zero if ANY assertion FAILed.
#
# WHY THIS FILE EXISTS (the coverage hole it closes):
# neuron ships NO gate_lib.sh of its own — this shim normally REBINDS
# refire_should_fire to base's `guard_composite`, which base's own
# test/gate_lib_test.sh covers. But the shim also carries an INLINE FALLBACK
# definition for the case where base's lib cannot be resolved, and that fallback
# was shipped with ZERO tests. It is the copy that runs on a neuron install whose
# base lib is missing/older, i.e. exactly the config nobody exercises locally.
#
# A real bug escaped through that hole: the same-cycle re-entry grace was first
# written UNBOUNDED (any re-entry inside the window fired). On a */15 gate that is
# invisible; on a faster operator gate (*/2, */5) it would have fired EVERY tick
# and silently destroyed the anti-spam floor. Base's test caught it in
# guard_composite. Nothing would have caught it here. Hence T2.
#
# CONTRACT UNDER TEST (must match base's guard_composite exactly):
#   1. fresh / changed content / elapsed floor      -> fire   (exit 0)
#   2. immediate re-entry, same body hash           -> fire ONCE (same-cycle grace)
#   3. further re-entries on that same stamp        -> suppress (grace is one-shot)
#   4. claim-line-only edit                         -> suppress (v1 loop fix)

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM="$TEST_DIR/../scripts/_gate_refire_guard.sh"

FAILED=0; PASSED=0; CURRENT="???"
_pb() { printf 'PASS  [%s]\n' "$CURRENT"; PASSED=$((PASSED+1)); }
_fb() { printf 'FAIL  [%s]\n' "$CURRENT"; FAILED=$((FAILED+1)); }
begin() { CURRENT="$(printf '%s' "$1" | tr -d '[]')"; }

[ -f "$SHIM" ] || { echo "FATAL: shim not found at $SHIM" >&2; exit 2; }

MKT="$(mktemp -d)"
trap 'rm -rf "$MKT"' EXIT

# ─────────────────────────────────────────────────────────────────────────────
# Force the INLINE FALLBACK path. The shim resolves base's lib from 4 candidates,
# all relative to its OWN dir (BASH_SOURCE) except one that reads
# ${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}. Copying the shim into a bare
# temp dir AND pointing HOME + OPENCLAW_WORKSPACE at that same empty dir makes
# every candidate miss -> guard_composite undefined -> inline fallback binds.
# (Hermetic: no dependency on whether a real base install exists on this box.)
ISO="$MKT/iso"; mkdir -p "$ISO"
cp "$SHIM" "$ISO/_gate_refire_guard.sh"

export HOME="$ISO" OPENCLAW_WORKSPACE="$ISO"
# shellcheck source=../scripts/_gate_refire_guard.sh
source "$ISO/_gate_refire_guard.sh" >/dev/null 2>&1

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 0: the inline fallback is what we are actually testing ==="
begin "T0 fallback bound"
# Guard against a false-green suite: if this ever silently rebinds to
# guard_composite, the rest of the file would be testing BASE's code and the
# neuron fallback would go back to being untested while still showing PASS.
if ! declare -F refire_should_fire >/dev/null 2>&1; then
  _fb; printf '  refire_should_fire not defined after sourcing the shim\n'
elif declare -f refire_should_fire | grep -q 'guard_composite'; then
  _fb; printf '  rebound to guard_composite — isolation failed, fallback NOT under test\n'
else
  _pb
fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 1: same-cycle re-entry fires (the Advancer deadlock fix) ==="
begin "T1 same-cycle re-entry"
# The deadlock: ONE dispatch cycle calls this twice — the gate fires and STAMPS,
# then the worker it just woke re-runs the same check as its Step 0 self-veto.
# Observed live: gate stamped 23:45:06Z, worker woke 23:45:59Z, self-vetoed exit 1
# -> the worker stopped before doing any work, every cycle, silently.
T1="$MKT/t1"; mkdir -p "$T1"
printf 'type: project\nstatus: in_progress\nbody\n' > "$T1/n.md"
st1="$T1/state"
refire_should_fire "$st1" 3600 "$T1/n.md"; g1=$?   # gate: fresh -> fire
refire_should_fire "$st1" 3600 "$T1/n.md"; w1=$?   # worker Step 0 -> must fire
if [ "$g1" -eq 0 ] && [ "$w1" -eq 0 ]; then _pb
else _fb; printf '  gate=%s worker=%s; want 0 0 (worker self-veto = deadlock)\n' "$g1" "$w1"; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 2: the grace is ONE-SHOT (anti-spam floor on fast gates) ==="
begin "T2 one-shot grace bound"
# Pins the bound that the unbounded first cut would have broken: N immediate
# calls on unchanged content => exactly 2 fires (1 real + 1 grace), never N.
T2="$MKT/t2"; mkdir -p "$T2"
printf 'type: project\nstatus: in_progress\nbody\n' > "$T2/n.md"
st2="$T2/state"; fires=0
for _ in 1 2 3 4 5 6; do
  if refire_should_fire "$st2" 3600 "$T2/n.md"; then fires=$((fires+1)); fi
done
if [ "$fires" -eq 2 ]; then _pb
else _fb; printf '  6 immediate calls -> %s fires; want exactly 2 (1 real + 1 one-shot)\n' "$fires"; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 3: claim-line-only edit still suppresses (v1 loop fix intact) ==="
begin "T3 claim-line exclusion"
# The body hash EXCLUDES claimed_by/claimed_at. Without that, a claim refresh
# looked like a content change and the gate re-fired itself forever (v1 bug).
# Checked AFTER the grace is spent, so a pass here cannot be the grace leaking.
T3="$MKT/t3"; mkdir -p "$T3"
printf 'type: project\nstatus: in_progress\nclaimed_at: A\nbody\n' > "$T3/n.md"
st3="$T3/state"
refire_should_fire "$st3" 3600 "$T3/n.md" >/dev/null 2>&1   # real fire
refire_should_fire "$st3" 3600 "$T3/n.md" >/dev/null 2>&1   # consume grace
printf 'type: project\nstatus: in_progress\nclaimed_at: B\nbody\n' > "$T3/n.md"
refire_should_fire "$st3" 3600 "$T3/n.md"; c3=$?
if [ "$c3" -eq 1 ]; then _pb
else _fb; printf '  claim-only edit -> exit %s; want 1 (suppress)\n' "$c3"; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 4: real content change fires ==="
begin "T4 real change fires"
printf 'type: project\nstatus: in_progress\nclaimed_at: B\nbody REAL\n' > "$T3/n.md"
refire_should_fire "$st3" 3600 "$T3/n.md"; c4=$?
if [ "$c4" -eq 0 ]; then _pb
else _fb; printf '  real content change -> exit %s; want 0 (fire)\n' "$c4"; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 5: interval floor still fires (no starvation) ==="
begin "T5 floor lapse fires"
# A note frozen at a safety gate must still get a periodic pass, and the grace
# sidecar must not latch it shut.
T5="$MKT/t5"; mkdir -p "$T5"
printf 'type: project\nstatus: in_progress\nbody\n' > "$T5/n.md"
st5="$T5/state"
printf '%s %s\n' "$(( $(date -u +%s) - 200000 ))" "staleHash" > "$st5"
refire_should_fire "$st5" 3600 "$T5/n.md"; c5=$?
if [ "$c5" -eq 0 ]; then _pb
else _fb; printf '  stale stamp past floor -> exit %s; want 0 (fire)\n' "$c5"; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 6: no qualifying input -> no fire ==="
begin "T6 empty input"
refire_should_fire "$MKT/t6state" 3600; c6=$?
if [ "$c6" -eq 1 ]; then _pb
else _fb; printf '  zero notes -> exit %s; want 1\n' "$c6"; fi

# ─────────────────────────────────────────────────────────────────────────────
printf '\n%s passed, %s failed\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]
