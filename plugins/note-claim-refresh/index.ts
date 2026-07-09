import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

// dinomem-neuron: Note Claim Refresh
// ----------------------------------
// Auto-refresh the live-session note claim as a CONSEQUENCE of editing a note,
// so the shared claim protocol (Advancer/Improver/Deleter crons back off a fresh
// live-session claim) becomes enforceable without the agent remembering to run
// `claim_note.sh refresh` every turn.
//
// Fires on after_tool_call for edit/write/apply_patch targeting a
// memory/_note_*.md. Cheap string guards bail in microseconds on the 99% of tool
// calls that are not note edits. Fire-and-forget: claim_note.sh refresh self-
// guards (exit 3 if the note is not ours -> no-op), so no ownership pre-check is
// needed here. Observe-only; never blocks or mutates the tool result.
//
// WORKSPACE RESOLUTION (buyer installs have arbitrary workspace paths — never
// hardcode). Priority:
//   1. plugin config workspaceDir (set by install.sh to the real --workspace)
//   2. $OPENCLAW_WORKSPACE env
//   3. derived from the target note path: the parent of the `memory/` segment
//      when the tool passed an absolute path (most robust, install-independent)
//   4. ctx.workspaceDir / ctx.cwd if the runtime provides it
// The chosen workspace must contain scripts/claim_note.sh; if none resolves to a
// real script, we no-op silently (graceful on a base-only / mis-wired install).

const EDIT_TOOLS = new Set(["edit", "write", "apply_patch"]);
const NOTE_RE = /(^|\/)memory\/_note_[^/]*\.md$/;

// Derive the workspace root from an absolute note path: <ws>/memory/_note_X.md
// -> <ws>. Returns undefined if the path has no `memory/` segment.
function wsFromNotePath(rawPath: string): string | undefined {
  if (!path.isAbsolute(rawPath)) return undefined;
  const parts = rawPath.split(path.sep);
  const idx = parts.lastIndexOf("memory");
  if (idx <= 0) return undefined;
  return parts.slice(0, idx).join(path.sep) || path.sep;
}

// First candidate whose scripts/claim_note.sh exists on disk.
function resolveWorkspace(
  candidates: Array<string | undefined>,
): { ws: string; script: string } | undefined {
  for (const c of candidates) {
    if (!c || typeof c !== "string") continue;
    const script = path.join(c, "scripts", "claim_note.sh");
    try {
      if (fs.existsSync(script)) return { ws: c, script };
    } catch {
      /* ignore fs errors, try next */
    }
  }
  return undefined;
}

export default definePluginEntry({
  id: "note-claim-refresh",
  name: "Note Claim Refresh",
  register(api) {
    api.on(
      "after_tool_call",
      async (event: any, ctx: any) => {
        try {
          // 1) cheap string guard — exit here for almost every tool call
          const tool = event?.toolName;
          if (!tool || !EDIT_TOOLS.has(tool)) return;

          // 2) extract target path (edit/write/apply_patch use params.path)
          const params = event?.params ?? {};
          const rawPath: string | undefined =
            typeof params.path === "string" ? params.path : undefined;
          if (!rawPath) return;

          // 3) one regex: only memory/_note_*.md
          if (!NOTE_RE.test(rawPath)) return;

          // 4) claimant = live-session-<agentId>
          const agentId = ctx?.agentId;
          if (!agentId) return;
          const claimant = `live-session-${agentId}`;

          // 5) resolve workspace dynamically (no hardcoded path)
          const cfg = event?.context?.pluginConfig ?? ctx?.pluginConfig ?? {};
          const resolved = resolveWorkspace([
            typeof cfg.workspaceDir === "string" ? cfg.workspaceDir : undefined,
            process.env.OPENCLAW_WORKSPACE,
            wsFromNotePath(rawPath),
            typeof ctx?.workspaceDir === "string" ? ctx.workspaceDir : undefined,
            typeof ctx?.cwd === "string" ? ctx.cwd : undefined,
          ]);
          if (!resolved) return; // no claim_note.sh anywhere -> nothing to do

          const { ws, script } = resolved;
          const notePath = path.isAbsolute(rawPath)
            ? rawPath
            : path.join(ws, rawPath);

          // 6) fire-and-forget detached refresh; do not await, do not block
          const child = spawn(
            "bash",
            [script, "refresh", notePath, claimant],
            { detached: true, stdio: "ignore" },
          );
          child.on("error", () => {});
          child.unref();
        } catch {
          // never let a hook error affect the tool result
        }
      },
      { priority: 0 },
    );
  },
});
