#!/usr/bin/env python3
"""
promotion_safety.py — Pre-stage safety validator for hook/cron behavior promotion.

Hooks/crons are runtime-mutating: a bad one can loop the agent, silently never fire,
or stall the pipeline. Skills are passive .md and skip this. Before skill_extract.py
STAGES a hook/cron proposal, it runs validate_hook()/validate_cron() here. A proposal
that fails a BLOCKER is never staged (status=rejected + reasons). WARNINGs are staged
but flagged so the human sees them at confirm time.

Grounded in OpenClaw's actual behavior (docs/automation/hooks.md + cron-jobs.md):
  - Internal hooks are REACT-ONLY (cannot block/cancel) — so the danger is not veto,
    it is SELF-RETRIGGER LOOPS, SILENT-DEAD hooks, and PIPELINE STALLS.
  - event.messages is delivered ONLY on command:new|reset + compaction; pushing on any
    other surface is silently ignored (a silent no-op).
  - Cron already has native guardrails (retry backoff, maxConcurrentRuns, per-run
    timeouts, staggering, dead-endpoint skip) and dinomem-base cron_tool refuses
    unconditional recurring --message without --confirmed. So cron gets LIGHT checks.
"""
import re

# The 16 events OpenClaw core actually emits (docs/automation/hooks.md). Anything else
# = silent-dead hook (loader logs a warning, hook never runs).
VALID_HOOK_EVENTS = {
    "command:new", "command:reset", "command:stop", "command",
    "session:compact:before", "session:compact:after", "session:patch",
    "agent:bootstrap",
    "gateway:startup", "gateway:shutdown", "gateway:pre-restart",
    "message:received", "message:transcribed", "message:preprocessed", "message:sent",
}
# Family listeners are valid (subscribe to every action in the family).
VALID_HOOK_FAMILIES = {"command", "session", "agent", "gateway", "message"}

# Surfaces where a hook that SENDS/PUSHES a message can re-trigger itself.
# message:received  -> a reply is an inbound elsewhere / re-entrant risk
# message:sent      -> fires on EVERY outbound; sending here = guaranteed loop
# command / command:* -> a sent message can be command-like -> re-entrant
LOOP_PRONE_EVENTS = {"message:received", "message:sent", "message:preprocessed",
                     "message:transcribed", "command", "command:new", "command:reset", "command:stop"}

# Action text that indicates the hook SENDS or PUSHES a message (loop fuel on loop-prone events).
SEND_ACTION_RE = re.compile(
    r"\b(send|reply|respond|message|push|post|notify|ping|announce|dm|answer)\b", re.I)

# event.messages push is delivered ONLY here; pushing elsewhere is a silent no-op.
REPLYABLE_EVENTS = {"command:new", "command:reset", "session:compact:before", "session:compact:after"}

# Heavy/blocking work on the command hot path (hooks run DURING command processing).
BLOCKING_ACTION_RE = re.compile(
    r"\b(sleep|while\s*\(|for\s*\(|await\s+fetch|http|download|scrape|train|embed|llm|model call|full scan)\b", re.I)


def validate_hook(slug: str, event: str, action_text: str) -> dict:
    """Return {ok, blockers:[], warnings:[]}. ok=False => DO NOT stage."""
    blockers, warnings = [], []
    ev = (event or "").strip()

    # 1) SILENT-DEAD: event must be a real emitted event or a family listener.
    if ev not in VALID_HOOK_EVENTS and ev not in VALID_HOOK_FAMILIES:
        blockers.append(
            f"unknown event '{ev}': OpenClaw core emits none of this -> hook would be silently dead. "
            f"Use one of the 16 valid events or a family listener {sorted(VALID_HOOK_FAMILIES)}.")

    # 2) SELF-TRIGGER LOOP: sending/pushing a message on a loop-prone event.
    sends = bool(SEND_ACTION_RE.search(action_text or ""))
    if sends and ev in LOOP_PRONE_EVENTS:
        blockers.append(
            f"loop risk: action sends/pushes a message on '{ev}', which can re-trigger this hook "
            f"(message:sent fires on EVERY outbound). Internal hooks are react-only and OpenClaw does "
            f"NOT break self-loops. Add a hard idempotency/self-message guard, or move the send to a "
            f"cron/tool, before this can be a hook.")

    # 3) SILENT NO-OP: pushing event.messages on a non-replyable surface does nothing.
    pushes = bool(re.search(r"\b(event\.)?messages(\.push)?\b|\bpush\b", action_text or "", re.I))
    if pushes and ev not in REPLYABLE_EVENTS:
        warnings.append(
            f"pushed messages on '{ev}' are IGNORED by OpenClaw (delivered only on {sorted(REPLYABLE_EVENTS)}). "
            f"The push will silently do nothing — use a tool/cron to actually deliver.")

    # 4) PIPELINE STALL: heavy synchronous work blocks command processing.
    if BLOCKING_ACTION_RE.search(action_text or ""):
        warnings.append(
            "handler looks heavy/blocking; hooks run DURING command processing. Keep it fast and "
            "fire-and-forget (void processInBackground(...)) or it stalls every command.")

    return {"ok": len(blockers) == 0, "blockers": blockers, "warnings": warnings}


# ── Cron: LIGHT checks (OpenClaw + base cron_tool already do the heavy lifting) ──────────
UNCONDITIONAL_RECURRING_RE = re.compile(r"\b(every|daily|hourly|weekly|each|recurring)\b", re.I)

def validate_cron(slug: str, cron_kind: str, payload_text: str, looks_like_llm_turn: bool = True) -> dict:
    """Light validation. The staged proposal already routes through base cron_tool with
    --confirmed, which enforces tiers + refuses unconditional recurring LLM turns. We only
    surface the one thing the human must consciously accept: recurring token cost."""
    warnings = []
    recurring = (cron_kind or "") in {"every", "cron"}
    if recurring and looks_like_llm_turn:
        warnings.append(
            "recurring job that looks like a model-backed turn burns tokens on EVERY fire. Prefer a "
            "deterministic --command or a --gate (T1: LLM only on a real hit). base cron_tool will "
            "refuse an unconditional recurring --message unless you pass --confirmed.")
    # never a blocker — cron's native guardrails + confirm gate are the real safety net.
    return {"ok": True, "blockers": [], "warnings": warnings}


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) >= 4 and sys.argv[1] == "hook":
        print(json.dumps(validate_hook(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:])), indent=2))
    elif len(sys.argv) >= 4 and sys.argv[1] == "cron":
        print(json.dumps(validate_cron(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:])), indent=2))
    else:
        print("usage: promotion_safety.py hook <slug> <event> <action...> | cron <slug> <kind> <payload...>")
