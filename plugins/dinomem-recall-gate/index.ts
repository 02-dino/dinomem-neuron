/**
 * dinomem-recall-gate — MID-SESSION recall enforcement (the piece bootstrap can't do).
 *
 * PROBLEM (dino, 2026-07-25): the dinomem-open-notes bootstrap hook injects the
 * M2/M3 recall gate ONCE, at agent:bootstrap. Mid-session (turn 5, turn 20...)
 * nothing re-injects, so recall rests on the model's own discipline — the exact
 * "plea to the model" we were mechanizing away.
 *
 * DESIGN (v2, LANGUAGE-AGNOSTIC): earlier drafts parsed the USER MESSAGE for
 * "named entity / continuation" keywords. That was proven English-only — a
 * multilingual test scored 1/5 on Indonesian, Spanish, and Chinese back-
 * references (all accidental hits on literal tool names). You cannot wordlist
 * across every language an installer might type in. So we STOPPED parsing the
 * message entirely.
 *
 * The gate now fires on the DANGER, not the message: if the model reaches for
 * the FILESYSTEM/SHELL (exec/read/grep/glob) *cold* — i.e. WITHOUT having run any
 * recall tool yet this turn — it gets ONE block telling it to recall first. Zero
 * language understanding; can't be defeated by typing in another language. A
 * false positive (a genuinely-fresh fs reach) costs exactly one cheap recall
 * call — the safe direction. A false negative is silent amnesia, the thing this
 * exists to kill.
 *
 * PER-TIER TOOLING: recallTools/fsTools come from config, so base dinomem
 * (memory_search/memory_get only) and neuron (+ session_search/graph_search/
 * docs_search/data_query/semantic_search) each ship a config matching their
 * actual tool set. The CODE is tier-agnostic; only the shipped config differs.
 *
 * WHY before_tool_call: internal message:received hooks cannot inject into model
 * context; only bootstrap + tool-call interception can steer a live turn. This is
 * the only mechanized mid-session lever.
 *
 * FAIL-OPEN: any error never blocks a tool. A recall-nudge bug must never brick
 * the agent. Owner-only concern — dinotrust-enforce owns the security floor; this
 * is purely a recall nudge.
 */

import { definePluginEntry } from "@openclaw/plugin-sdk";

// ── Config (overridable via plugins.entries["dinomem-recall-gate"].config) ──
type Cfg = {
  enforce: boolean;        // false => dry-run: never actually block
  agentFilter: string;     // only act on sessionKeys containing this; "" = all
  recallTools: string[];   // tools that COUNT as "recall done this turn" (TIER-SPECIFIC)
  fsTools: string[];       // fs/exec tools whose COLD use triggers the nudge
  cooldownTurns: number;   // min turns between fires (avoid nagging on repeated ignores)
};

function cfg(raw: any): Cfg {
  const c = raw ?? {};
  return {
    enforce: c.enforce !== false,
    agentFilter: typeof c.agentFilter === "string" ? c.agentFilter : "analyst",
    // Default = FULL neuron set. Base dinomem MUST override to [memory_search,
    // memory_get] in its shipped config, else the blockReason names tools a base
    // user lacks. The plugin never assumes a tier; the config declares it.
    recallTools: Array.isArray(c.recallTools) ? c.recallTools
      : ["memory_search", "memory_get", "graph_search", "session_search", "semantic_search", "docs_search", "data_query"],
    fsTools: Array.isArray(c.fsTools) ? c.fsTools : ["exec", "read", "grep", "glob"],
    cooldownTurns: Number.isFinite(c.cooldownTurns) ? c.cooldownTurns : 3,
  };
}

// ── Per-turn state (in-memory). A "turn" = one user message + its tool calls. ──
// Keyed by sessionKey. We do NOT inspect message CONTENT for meaning (that was the
// English-only trap) — we only fingerprint it to detect turn boundaries.
type TurnState = {
  turnId: string;       // fingerprint of the current user message (boundary detection only)
  turnIndex: number;    // monotonic counter of turns in this session
  recallDone: boolean;  // a recallTool ran during this turn
  firedTurn: number;    // turnIndex of last fire (-Infinity if never) -> cooldown
};
const _state = new Map<string, TurnState>();

function userMsgOf(event: any, ctx: any): string {
  return String(
    ctx?.userMessage ?? ctx?.messageText ?? event?.userMessage ??
    ctx?.lastUserMessage ?? event?.message?.text ?? ""
  );
}

// Cheap stable fingerprint of the message, ONLY to detect "is this a new turn?".
// No semantic parsing — language-irrelevant by construction.
function fp(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
  return String(h);
}

// ── register ─────────────────────────────────────────────────────────────────
export default definePluginEntry({
  name: "dinomem-recall-gate",
  description: "Mid-session recall enforcement (language-agnostic): blocks the first COLD " +
    "fs/exec reach on a turn until a recall tool has run. No message parsing.",
  register(api: any) {
    const c = cfg(api?.pluginConfig?.config);
    const recallList = c.recallTools.join("/");

    api.on("before_tool_call", async (event: any, ctx: any) => {
      try {
        const sessionKey: string = String(ctx?.sessionKey ?? event?.sessionKey ?? "");
        if (c.agentFilter && !sessionKey.includes(c.agentFilter)) return undefined;

        const toolName: string = String(event?.toolName ?? event?.tool ?? "");
        if (!toolName) return undefined;

        // Turn-boundary detection ONLY (no semantic parse of the message).
        const turnId = fp(userMsgOf(event, ctx) || sessionKey);

        let st = _state.get(sessionKey);
        if (!st || st.turnId !== turnId) {
          const turnIndex = (st?.turnIndex ?? 0) + 1;
          st = {
            turnId,
            turnIndex,
            recallDone: false,
            firedTurn: st?.firedTurn ?? -Infinity,
          };
          _state.set(sessionKey, st);
        }

        // A recall tool ran this turn -> latch cleared, gate silent until next turn.
        if (c.recallTools.includes(toolName)) {
          st.recallDone = true;
          return undefined;
        }

        // Only fs/exec-shaped tools are gated.
        if (!c.fsTools.includes(toolName)) return undefined;

        // Recall already happened this turn -> no gate. (THE core rule: cold reach
        // = no recall yet this turn. Language-independent by construction.)
        if (st.recallDone) return undefined;

        // Cooldown: don't nag every turn if the model keeps skipping recall.
        if (st.turnIndex - st.firedTurn < c.cooldownTurns) return undefined;

        // Fire ONCE per turn. Do NOT hard-loop-block: if the model retries the same
        // fs tool this turn without recalling, let it through rather than stall the
        // turn. The nudge fired; persistent-ignore is now a model-quality problem,
        // not a missing-injection one.
        const alreadyFiredThisTurn = st.firedTurn === st.turnIndex;
        st.firedTurn = st.turnIndex;
        if (alreadyFiredThisTurn) return undefined;

        if (!c.enforce) return undefined; // dry-run: would have blocked, didn't

        return {
          block: true,
          blockReason:
            `dinomem-recall-gate: you're reaching for '${toolName}' before running any recall ` +
            `tool this turn. If this touches prior work/context, recall FIRST (${recallList}), ` +
            `then retry. If it's genuinely fresh work with nothing to recall, just re-issue — ` +
            `the gate won't fire again this turn.`,
        };
      } catch (err) {
        // Fail open, always. A recall nudge must never brick the tool loop.
        return undefined;
      }
    });
  },
});
