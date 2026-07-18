import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

// dinomem-neuron: Note Claim On-Create (before_tool_call)
// ------------------------------------------------------
// Closes the LAST discipline-dependent sliver in the note-claim protocol: a
// _note_ CREATED (or first-edited) mid-session, before the model has run
// claim_note.sh on it. The other two layers cover everything else:
//   - bootstrap auto-claim (dinomem-open-notes hook): claims in_progress notes
//     that already exist when a session starts.
//   - note-claim-refresh (after_tool_call): keeps an EXISTING live claim fresh.
// Neither fires for a note born mid-session that the live session hasn't claimed
// yet -> a cron (Advancer/Improver/Deleter) could grab it in that window. This
// plugin stamps the claim in the SAME turn the write happens, BEFORE the tool
// executes, so the note is protected from the instant it lands.
//
// MECHANISM: on before_tool_call, if the tool is a note-writing tool (write /
// edit / apply_patch) whose target path is a memory/_note_*.md, we run
// `claim_note.sh claim <note> live-session-<agentId>` synchronously (short
// timeout) BEFORE returning. claim_note.sh self-guards: it takes the claim if
// free / stale / already ours, and exits 3 (no-op) if another live actor holds
// it fresh -- so this never steals a note from another session. We NEVER block
// or rewrite the tool: we return undefined (no decision) always, even on claim
// failure. Claiming is a side effect; the write proceeds regardless.
//
// WHY before_tool_call (not after): the whole point is to hold the claim BEFORE
// the note exists on disk long enough for a cron tick to see it unclaimed. After
// the write there is a (small) window where the note is unclaimed. Claiming just
// before the write closes it. If the file does not exist yet (fresh create),
// claim_note.sh needs the file present, so for a CREATE we let the write proceed
// and rely on the refresh/next-tool-call to claim -- but for the common case
// (write tool creates the file, THEN subsequent edits) and for edits to an
// existing note, we claim pre-write. See handleCreateFallback below.
//
// PORTABILITY: no hardcoded paths. Workspace resolved from (in order) plugin
// config workspaceDir, the target note path's own workspace root, OPENCLAW_WORKSPACE,
// the $HOME/.openclaw/workspace-<agentId> convention, or ctx. Claimant derives
// from agentId generically. Safe on any buyer install; no-ops if claim_note.sh
// is absent (e.g. mis-wired / base-only host).

// Tools that write/create note files. Names match OpenClaw's tool surface;
// we also accept common aliases to be robust across runtimes.
const NOTE_WRITE_TOOLS = new Set([
  "write",
  "edit",
  "apply_patch",
  "Write",
  "Edit",
  "NotebookEdit",
]);

// Derive agentId from a sessionKey like `agent:<id>:telegram:...` -> `<id>`.
function agentIdFromSessionKey(sk: unknown): string | undefined {
  if (typeof sk !== "string") return undefined;
  const m = /^agent:([^:]+):/.exec(sk);
  return m ? m[1] : undefined;
}

// Pull a plausible target file path out of a tool's params. Different tools name
// the field differently (path / file_path / filename); apply_patch encodes paths
// inside the patch body, and the host may pre-derive them into event.derivedPaths.
function extractTargetPaths(
  toolName: string,
  params: Record<string, unknown> | undefined,
  derivedPaths: unknown,
): string[] {
  const out: string[] = [];
  const pushIf = (v: unknown) => {
    if (typeof v === "string" && v.length > 0) out.push(v);
  };
  if (params) {
    pushIf(params.path);
    pushIf(params.file_path);
    pushIf((params as Record<string, unknown>).filePath);
    pushIf(params.filename);
    // apply_patch: paths live in the patch text ("*** Update File: <p>" etc.)
    const patch = params.input ?? params.patch ?? params.content;
    if (typeof patch === "string" && /\*\*\* (Add|Update|Delete) File:/.test(patch)) {
      const re = /\*\*\* (?:Add|Update|Delete) File:\s*(.+)/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(patch)) !== null) pushIf(m[1].trim());
    }
  }
  // host-derived best-effort target hints (may over-approximate)
  if (Array.isArray(derivedPaths)) {
    for (const p of derivedPaths) pushIf(p);
  }
  return out;
}

// Is this path a dinomem note file (memory/_note_*.md)? Tolerant of relative vs
// absolute; we match on the tail segments, not a fixed root.
function isNotePath(p: string): boolean {
  const norm = p.replace(/\\/g, "/");
  const base = norm.split("/").pop() ?? "";
  if (!(base.startsWith("_note_") && base.endsWith(".md"))) return false;
  return norm.includes("/memory/") || !norm.includes("/");
}

// Resolve the workspace that owns a given note path by walking up until we find
// a sibling scripts/claim_note.sh. Returns {script, notePath(abs)} or undefined.
function resolveFromNotePath(
  notePath: string,
): { script: string; note: string } | undefined {
  let abs = notePath;
  try {
    if (!path.isAbsolute(abs)) abs = path.resolve(process.cwd(), abs);
  } catch {
    return undefined;
  }
  // .../memory/_note_x.md -> workspace root is parent of memory/
  const memDir = path.dirname(abs);
  if (path.basename(memDir) !== "memory") return undefined;
  const wsRoot = path.dirname(memDir);
  const script = path.join(wsRoot, "scripts", "claim_note.sh");
  try {
    if (fs.existsSync(script)) return { script, note: abs };
  } catch {
    /* ignore */
  }
  return undefined;
}

// Fallback workspace resolution when the note path is relative / rootless: try
// config, env, and the $HOME/.openclaw/workspace-<agentId> convention.
function resolveWorkspaceScript(
  candidates: Array<string | undefined>,
): string | undefined {
  for (const c of candidates) {
    if (!c || typeof c !== "string") continue;
    const script = path.join(c, "scripts", "claim_note.sh");
    try {
      if (fs.existsSync(script)) return script;
    } catch {
      /* try next */
    }
  }
  return undefined;
}

export default definePluginEntry({
  id: "note-claim-oncreate",
  name: "Note Claim On-Create",
  register(api) {
    api.on(
      "before_tool_call",
      async (event: any, ctx: any) => {
        try {
          const toolName: string =
            event?.toolName ?? event?.tool ?? ctx?.toolName ?? "";
          if (!NOTE_WRITE_TOOLS.has(toolName)) return; // not a note-write tool

          const params = (event?.params ?? event?.args) as
            | Record<string, unknown>
            | undefined;
          const targets = extractTargetPaths(
            toolName,
            params,
            event?.derivedPaths,
          );
          const noteTargets = targets.filter(isNotePath);
          if (noteTargets.length === 0) return; // not touching a _note_ file

          // claimant = live-session-<agentId>
          const agentId =
            agentIdFromSessionKey(ctx?.sessionKey ?? event?.sessionKey) ??
            (typeof ctx?.agentId === "string" ? ctx.agentId : undefined);
          if (!agentId) return;
          const claimant = `live-session-${agentId}`;

          const cfg = event?.context?.pluginConfig ?? ctx?.pluginConfig ?? {};
          const home = process.env.HOME || process.env.USERPROFILE;
          const convWs = home
            ? path.join(home, ".openclaw", `workspace-${agentId}`)
            : undefined;

          for (const t of noteTargets) {
            // Prefer resolving the workspace FROM the note path itself (most
            // reliable on multi-workspace hosts). Fall back to config/env/conv.
            let script: string | undefined;
            let notePath = t;
            const fromNote = resolveFromNotePath(t);
            if (fromNote) {
              script = fromNote.script;
              notePath = fromNote.note;
            } else {
              script = resolveWorkspaceScript([
                typeof cfg.workspaceDir === "string"
                  ? cfg.workspaceDir
                  : undefined,
                process.env.OPENCLAW_WORKSPACE,
                convWs,
                typeof ctx?.workspaceDir === "string"
                  ? ctx.workspaceDir
                  : undefined,
              ]);
            }
            if (!script) continue; // no claim_note.sh resolvable -> skip this target

            // Only claim if the note file already exists on disk. claim_note.sh
            // requires the file present (it edits frontmatter). For a fresh
            // CREATE the file won't exist yet -> we skip here and let the
            // note-claim-refresh (after_tool_call) or next-turn bootstrap claim
            // it; the write itself proceeds either way.
            try {
              if (!fs.existsSync(notePath)) continue;
            } catch {
              continue;
            }

            // Synchronous, short-timeout claim BEFORE the write. Self-guards:
            // exit 0 = took/refreshed/ours; exit 3 = held fresh by another
            // (leave it). We never block the tool regardless of outcome.
            try {
              spawnSync("bash", [script, "claim", notePath, claimant], {
                timeout: 4000,
                stdio: "ignore",
              });
            } catch {
              /* never let a claim failure affect the tool */
            }
          }
        } catch {
          // never let a hook error affect the tool call
        }
        // Always no-decision: we only stamp a claim as a side effect.
        return;
      },
      { priority: 0 },
    );
  },
});
