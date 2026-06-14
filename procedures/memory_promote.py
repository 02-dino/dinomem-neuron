#!/usr/bin/env python3
"""
memory_promote.py — Layer 4 "promotion" for analyst memory (zero-human pipeline).

Consumes the Stage-3 gate output (l3_synthesis/current_insights.json) and decides
which insights graduate into the live memory pool — the step that actually lets the
system rewrite its own behavior WITHOUT a human in the loop. Because that is the
single most dangerous action in the whole stack, every promotion runs on rails:

  PROBATION   A `promotable` insight does NOT go live immediately. It enters a
              provisional store and must be re-surfaced as promotable across
              GRADUATE_REINFORCE separate runs before it counts. One-shot flukes
              never reach behavior.
  REINFORCE   Each re-appearance bumps reinforce_count and resets the decay clock
              (Hebbian: repeated co-activation strengthens the trace).
  DECAY/TTL   Provisional insights not reinforced within TTL_DAYS auto-expire.
              Trusted insights not reinforced within TRUSTED_TTL_DAYS get demoted
              and removed from behavior. Wrong promotions self-heal; nothing that
              stops recurring lives forever.
  RATE LIMIT  At most MAX_GRADUATIONS_PER_RUN graduate per run, so one bad run can
              never flood the behavior memory.
  FIREWALL    Graduated insights are written to memory/auto_promoted.md, which
              memory_graph.py skips (Stage-5 feedback-loop firewall) so synthesis
              can never read its own conclusions back and inflate confidence.
  AUDIT       Every promote / reinforce / graduate / expire / demote action is
              appended to promotion_log.jsonl. Fully reconstructable, reversible.

NO LLM CALLS — pure bookkeeping, so it is cheap and cannot OOM.

Usage:
  python3 memory_promote.py            # run the promotion cycle
  python3 memory_promote.py --dry-run  # show decisions, write nothing
  python3 memory_promote.py --status   # print current provisional/trusted state
"""

import argparse
import json
import hashlib
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

BASE = Path("/root/.openclaw/workspace-analyst")
L3_DIR = BASE / "kb/memory_neuron/l3_synthesis"
L4_DIR = BASE / "kb/memory_neuron/l4_promoted"
TOPICS_DIR = BASE / "topics"
CURRENT_INSIGHTS = L3_DIR / "current_insights.json"

STATE_FILE = L4_DIR / "promotion_state.json"     # {key: entry} provisional+trusted store
CONSUMED_FILE = L4_DIR / ".last_consumed.json"   # last handoff sig consumed (dedup guard)
LOG_FILE = L4_DIR / "promotion_log.jsonl"        # append-only audit trail
TRUSTED_MIRROR = L4_DIR / "TRUSTED.md"           # human-readable mirror of trusted set

# Behavior output: graduated rules land in topics/PERSISTENT_AUTO.md, which
# generate_topic_index.py folds into the Persistent section of MEMORY.md (the
# always-injected root file). topics/ is NOT ingested into the memory DB, and
# MEMORY.md is firewalled from the neuron graph → no feedback loop, by construction.
BEHAVIOR_FILE = TOPICS_DIR / "PERSISTENT_AUTO.md"

# ── Rails (tunable) ──
TTL_DAYS = 14               # provisional expires if not reinforced within this
TRUSTED_TTL_DAYS = 45       # trusted decays/demotes if not reinforced within this
GRADUATE_REINFORCE = 3      # distinct reinforcing runs needed to graduate
MAX_GRADUATIONS_PER_RUN = 2 # rate limit: behavior memory can't be flooded
MAX_PERSISTENT_RULES = 12   # hard cap on injected rules — keep MEMORY.md lean
SAME_RATIO = 0.6            # fuzzy ratio above which two patterns are "the same"


def now_iso():
    return datetime.now().isoformat()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def key_for(pattern):
    """Stable-ish key. Fuzzy matching handles rephrasings; this just seeds ids."""
    return hashlib.sha256(pattern.strip().lower().encode("utf-8")).hexdigest()[:16]


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    L4_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def log_action(action, entry, extra=None):
    L4_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": now_iso(),
        "action": action,
        "pattern": entry.get("pattern", "")[:200],
        "status": entry.get("status"),
        "reinforce_count": entry.get("reinforce_count"),
    }
    if extra:
        rec.update(extra)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def check_contradiction(new_rule, trusted_rules):
    """Live L4 graduation gate → delegates to the single canonical implementation
    in contradiction_check.rules_contradict (one prompt / one logic, no drift).
    Lazy import so the promoter stays light when no graduation happens.
    Returns (contradicts, reason); fail-open inside the shared function."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from contradiction_check import rules_contradict
        return rules_contradict(new_rule, trusted_rules)
    except Exception as ex:
        return (False, f"check skipped ({type(ex).__name__}) — allowed")


def find_match(state, pattern):
    """Return (key, entry) of the best fuzzy match in state, or (None, None)."""
    pat = pattern.strip().lower()
    best_k, best_r = None, 0.0
    for k, e in state.items():
        r = SequenceMatcher(None, pat, e["pattern"].strip().lower()).ratio()
        if r > best_r:
            best_k, best_r = k, r
    if best_k and best_r >= SAME_RATIO:
        return best_k, state[best_k]
    return None, None


def expired(entry, ttl_days):
    last = entry.get("last_reinforced") or entry.get("first_promoted")
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) > timedelta(days=ttl_days)
    except Exception:
        return False


def render_behavior(state):
    """Rewrite the behavior file from the trusted set (idempotent — demotions
    disappear automatically). Emits ONE compact rule line per trusted insight that
    has an actionable rule_line, capped + sorted by reinforcement strength, so the
    Persistent section of MEMORY.md stays lean. generate_topic_index folds this in."""
    trusted = [e for e in state.values()
               if e.get("status") == "trusted" and (e.get("rule_line") or "").strip()]
    # Strongest first (most reinforced), then cap.
    trusted.sort(key=lambda e: e.get("reinforce_count", 0), reverse=True)
    trusted = trusted[:MAX_PERSISTENT_RULES]

    lines = ["## Persistent — Machine-Promoted (auto, L4)"]
    for e in trusted:
        # rule_line grammar: "Title | kw1, kw2 | directive" → prefix as a persistent entry
        lines.append(f"[AUTO] [persist] {e['rule_line'].strip()}")
    body = "\n".join(lines) + "\n"

    BEHAVIOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Empty (no trusted rules) → write empty file so the index shows nothing.
    BEHAVIOR_FILE.write_text(body if trusted else "", encoding="utf-8")

    # Human-readable mirror in the L4 dir (NOT indexed — full provenance).
    L4_DIR.mkdir(parents=True, exist_ok=True)
    mirror = ["# Trusted (Layer 4) — full provenance", ""]
    if not trusted:
        mirror.append("_(none currently trusted with an actionable rule)_")
    for i, e in enumerate(trusted, 1):
        mirror += [
            f"## {i}. {e['rule_line'].strip()}",
            f"- pattern: {e.get('pattern','')}",
            f"- confidence: {e.get('confidence')} · reinforced ×{e.get('reinforce_count')}",
            f"- graduated: {e.get('graduated_on')} · first seen: {e.get('first_promoted')}",
            "",
        ]
    TRUSTED_MIRROR.write_text("\n".join(mirror) + "\n", encoding="utf-8")


def run(dry_run=False):
    if not CURRENT_INSIGHTS.exists():
        print(f"no synthesis handoff at {CURRENT_INSIGHTS} — run memory_synthesis first")
        return
    payload = json.loads(CURRENT_INSIGHTS.read_text(encoding="utf-8"))
    candidates = [i for i in payload.get("insights", []) if i.get("gate_status") == "promotable"]
    state = load_state()
    actions = {"new": 0, "reinforced": 0, "graduated": 0, "expired": 0, "demoted": 0, "blocked": 0}

    # Reinforcement must count DISTINCT synthesis runs, not orchestrator cycles.
    # Synthesis only rewrites the handoff when memory changes (new sig); the promoter
    # runs every cycle. Without this guard a single handoff would be re-counted every
    # 15 min and graduate in under an hour. So intake/reinforcement runs ONLY on a
    # sig we haven't consumed; decay (time-based) always runs.
    handoff_sig = payload.get("sig")
    last_consumed = None
    if CONSUMED_FILE.exists():
        try:
            last_consumed = json.loads(CONSUMED_FILE.read_text(encoding="utf-8")).get("sig")
        except Exception:
            pass
    new_handoff = bool(handoff_sig) and handoff_sig != last_consumed

    # ── Load pinned slugs (insights with a _pin_ file are immortal — skip decay) ──
    MEMORY_DIR = Path(str(WORKSPACE_ROOT)) / "memory"
    pinned_slugs = {f.stem.replace("_pin_", "") for f in MEMORY_DIR.glob("_pin_*.md")} if MEMORY_DIR.exists() else set()

    # ── Decay pass (before reinforcement, so this run's reinforcement can rescue) ──
    for k in list(state.keys()):
        e = state[k]
        # Skip decay if insight has a corresponding _pin_ file
        insight_slug = re.sub(r'[^a-z0-9]+', '-', e.get('pattern', k).lower())[:30].strip('-')
        if any(insight_slug.startswith(slug) or slug.startswith(insight_slug) for slug in pinned_slugs):
            continue
        if e.get("status") == "provisional" and expired(e, TTL_DAYS):
            actions["expired"] += 1
            if not dry_run:
                log_action("expired", e, {"reason": f"no reinforcement in {TTL_DAYS}d"})
                del state[k]
        elif e.get("status") == "trusted" and expired(e, TRUSTED_TTL_DAYS):
            actions["demoted"] += 1
            if not dry_run:
                log_action("demoted", e, {"reason": f"trusted but stale {TRUSTED_TTL_DAYS}d"})
                del state[k]

    # ── Reinforcement / intake pass (only on a not-yet-consumed handoff) ──
    graduations_this_run = 0
    for c in (candidates if new_handoff else []):
        pat = c["pattern"]
        mk, me = find_match(state, pat)
        if me:
            me["reinforce_count"] = me.get("reinforce_count", 1) + 1
            me["last_reinforced"] = now_iso()
            me["confidence"] = c.get("confidence", me.get("confidence"))
            if (c.get("rule_line") or "").strip():
                me["rule_line"] = c["rule_line"]
            actions["reinforced"] += 1
            if not dry_run:
                log_action("reinforced", me)
            # Graduate provisional → trusted once reinforced enough (rate-limited).
            if (me.get("status") == "provisional"
                    and me["reinforce_count"] >= GRADUATE_REINFORCE
                    and graduations_this_run < MAX_GRADUATIONS_PER_RUN):
                # Contradiction gate: a new rule must not negate an existing trusted
                # one (silent self-conflict). Stay provisional if it does — it can
                # graduate later if the conflicting rule decays. Self-healing.
                trusted_rules = [e.get("rule_line", "") for e in state.values()
                                 if e.get("status") == "trusted" and e.get("rule_line")]
                contra, reason = (False, "")
                if (me.get("rule_line") or "").strip():
                    contra, reason = check_contradiction(me["rule_line"], trusted_rules)
                if contra:
                    me["contradiction"] = reason
                    actions["blocked"] += 1
                    if not dry_run:
                        log_action("contradiction_blocked", me, {"reason": reason})
                else:
                    me.pop("contradiction", None)
                    me["status"] = "trusted"
                    me["graduated_on"] = today_str()
                    graduations_this_run += 1
                    actions["graduated"] += 1
                    if not dry_run:
                        log_action("graduated", me)
        else:
            entry = {
                "pattern": pat,
                "status": "provisional",
                "confidence": c.get("confidence"),
                "occurrences": c.get("occurrences", 1),
                "reinforce_count": 1,
                "first_promoted": today_str(),
                "last_reinforced": now_iso(),
                "sources": c.get("sources", []),
                "rule_line": c.get("rule_line", ""),
            }
            state[key_for(pat)] = entry
            actions["new"] += 1
            if not dry_run:
                log_action("new_provisional", entry)

    if not dry_run:
        save_state(state)
        render_behavior(state)
        if new_handoff:
            CONSUMED_FILE.write_text(json.dumps({"sig": handoff_sig, "ts": now_iso()}),
                                     encoding="utf-8")

    consumed_note = "" if new_handoff else " [handoff already consumed — intake skipped]"
    n_prov = sum(1 for e in state.values() if e.get("status") == "provisional")
    n_trust = sum(1 for e in state.values() if e.get("status") == "trusted")
    tag = " [DRY-RUN]" if dry_run else ""
    print(f"L4 promote{tag}{consumed_note}: {len(candidates)} promotable in · "
          f"new={actions['new']} reinforced={actions['reinforced']} "
          f"graduated={actions['graduated']} blocked={actions['blocked']} "
          f"expired={actions['expired']} demoted={actions['demoted']}")
    print(f"  state now: {n_prov} provisional · {n_trust} trusted "
          f"(graduate at ×{GRADUATE_REINFORCE}, max {MAX_GRADUATIONS_PER_RUN}/run)")


def show_status():
    state = load_state()
    prov = [e for e in state.values() if e.get("status") == "provisional"]
    trust = [e for e in state.values() if e.get("status") == "trusted"]
    print(f"=== L4 promotion state: {len(prov)} provisional, {len(trust)} trusted ===")
    for e in sorted(trust, key=lambda x: x.get("graduated_on", ""), reverse=True):
        print(f"  [TRUSTED ×{e.get('reinforce_count')}] {e['pattern'][:90]}")
    for e in sorted(prov, key=lambda x: x.get("reinforce_count", 0), reverse=True):
        need = GRADUATE_REINFORCE - e.get("reinforce_count", 0)
        print(f"  [prov ×{e.get('reinforce_count')}, {need} to graduate] {e['pattern'][:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show decisions, write nothing")
    ap.add_argument("--status", action="store_true", help="print current state and exit")
    args = ap.parse_args()
    if args.status:
        show_status()
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
