import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

// dinomem-neuron: Note Claim Refresh
// ----------------------------------
// Keeps a live-session note claim fresh for as long as the session is ACTIVELY
// DOING ANYTHING — not only while it happens to be editing the claimed note.
//
// WHY THE BROADENED TRIGGER: the shared claim protocol lets the Advancer /
// Improver / Deleter crons back off a fresh `live-session-*` claim (120-min
// window). The original hook only refreshed on edit/write to a memory/_note_*.md.
// But a live session can legitimately hold a claim on note X while spending an
// hour doing exec/git/read/other-file work (e.g. building a repo the note tracks).
// No note edit -> no refresh -> the claim aged past 120 min -> a cron reclaimed a
// note a human was still actively working (observed 2026-07-10). The real liveness
// signal is "the session is doing SOMETHING", so we now refresh on ANY tool call.
//
// Fires on after_tool_call for every tool. A per-agent time throttle keeps it to
// at most one refresh per REFRESH_EVERY_MS, so scanning stays negligible. Each
// refresh scans memory/_note_*.md for notes claimed by THIS session and bumps
// their claimed_at via claim_note.sh (which self-guards: exit 3 / no-op if the
// note is not ours). Fire-and-forget, detached; never blocks or mutates the tool
// result. If no live claim exists, the scan finds nothing and does nothing.
//
// IDENTITY: the runtime hands plugins a sessionKey like `agent:<id>:<channel>:...`
// (see sibling hooks). We derive agentId from it (fallback ctx.agentId) and build
// claimant = live-session-<agentId>, matching what an interactive session stamps.
//
// WORKSPACE RESOLUTION (buyer installs have arbitrary workspace paths — never
// hardcode). Priority:
//   1. plugin config workspaceDir (set by install.sh to the real --workspace)
//   2. $OPENCLAW_WORKSPACE env
//   3. $HOME/.openclaw/workspace-<agentId>  (OpenClaw multi-agent convention)
//   4. ctx.workspaceDir / ctx.cwd if the runtime provides it
// The chosen workspace must contain scripts/claim_note.sh AND a memory/ dir; if
// none resolves, we no-op silently (graceful on a base-only / mis-wired install).

// Derive agentId from a sessionKey like `agent:<id>:telegram:...` -> `<id>`.
function agentIdFromSessionKey(sk: unknown): string | undefined {
  if (typeof sk !== "string") return undefined;
  const m = /^agent:([^:]+):/.exec(sk);
  return m ? m[1] : undefined;
}

// Refresh at most this often per agent. Well under the 120-min live-session
// window, so a claim can never lapse while the session keeps working, yet we do
// not spawn a scan on every single tool call.
const REFRESH_EVERY_MS = 5 * 60 * 1000; // 5 minutes

// Last-refresh timestamp per agentId (module-scoped; process-lifetime memo).
const lastRefreshByAgent = new Map<string, number>();

// First candidate whose scripts/claim_note.sh and memory/ both exist on disk.
function resolveWorkspace(
  candidates: Array<string | undefined>,
): { ws: string; script: string; memDir: string } | undefined {
  for (const c of candidates) {
    if (!c || typeof c !== "string") continue;
    const script = path.join(c, "scripts", "claim_note.sh");
    const memDir = path.join(c, "memory");
    try {
      if (fs.existsSync(script) && fs.existsSync(memDir)) {
        return { ws: c, script, memDir };
      }
    } catch {
      /* ignore fs errors, try next */
    }
  }
  return undefined;
}

// Return absolute paths of _note_*.md files currently claimed by `claimant`.
// Cheap-ish: reads only the frontmatter of each note file, NOT the whole body.
//
// BUG FIX (2026-07-10): the original version capped the read at a fixed 2048
// bytes. That's fine for simple fenced (---/---) notes where claimed_by sits
// near the top, but claim_note.sh's "project-note" format (bare frontmatter
// ending at the FIRST BLANK LINE) appends claimed_by/claimed_at at the END of
// that block -- which can be well past 2048 bytes for notes with a long
// `steps:` list or a multi-paragraph `resume_state: |` block (observed at
// byte ~5000 and ~12000 on real notes). The fixed-window read silently missed
// the claim entirely -> notesClaimedBy() always returned [] for those notes ->
// refresh became a permanent no-op with zero error, looking like the hook
// wasn't firing at all when it was firing fine (verified via debug log).
// Fix: read up to FRONTMATTER_SCAN_MAX bytes and stop early at the actual
// frontmatter terminator (closing `---` for fenced, first blank line for
// bare), so we scan exactly the frontmatter, not an arbitrary byte count.
const FRONTMATTER_SCAN_MAX = 32 * 1024; // generous cap; still cheap vs a full read
function notesClaimedBy(memDir: string, claimant: string): string[] {
  let entries: string[];
  try {
    entries = fs.readdirSync(memDir);
  } catch {
    return [];
  }
  const needle = `claimed_by: ${claimant}`;
  const out: string[] = [];
  for (const name of entries) {
    if (!name.startsWith("_note_") || !name.endsWith(".md")) continue;
    const full = path.join(memDir, name);
    try {
      const fd = fs.openSync(full, "r");
      try {
        const buf = Buffer.allocUnsafe(FRONTMATTER_SCAN_MAX);
        const n = fs.readSync(fd, buf, 0, FRONTMATTER_SCAN_MAX, 0);
        const chunk = buf.toString("utf8", 0, n);
        const lines = chunk.split("\n");
        let frontmatterEnd = lines.length; // default: whole chunk if terminator not found
        if (lines[0] === "---") {
          // Fenced format: ends at the next line that is exactly '---'.
          const closeIdx = lines.findIndex((l, i) => i > 0 && l === "---");
          if (closeIdx !== -1) frontmatterEnd = closeIdx + 1;
        } else if (/^#/.test(lines[0] ?? "")) {
          // Bare format: ends at the first blank line.
          const blankIdx = lines.findIndex((l, i) => i > 0 && l.trim() === "");
          if (blankIdx !== -1) frontmatterEnd = blankIdx + 1;
        }
        const head = lines.slice(0, frontmatterEnd).join("\n");
        if (head.includes(needle)) out.push(full);
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      /* unreadable note -> skip */
    }
  }
  return out;
}

export default definePluginEntry({
  id: "note-claim-refresh",
  name: "Note Claim Refresh",
  register(api) {
    api.on(
      "after_tool_call",
      async (event: any, ctx: any) => {
        try {
          // 1) claimant = live-session-<agentId> (agentId from sessionKey or ctx)
          const agentId =
            agentIdFromSessionKey(ctx?.sessionKey ?? event?.sessionKey) ??
            (typeof ctx?.agentId === "string" ? ctx.agentId : undefined);
          if (!agentId) return;
          const claimant = `live-session-${agentId}`;

          // 2) time throttle — at most one refresh per REFRESH_EVERY_MS per agent
          const now = Date.now();
          const last = lastRefreshByAgent.get(agentId) ?? 0;
          if (now - last < REFRESH_EVERY_MS) return;

          // 3) resolve workspace dynamically (no hardcoded path)
          const cfg = event?.context?.pluginConfig ?? ctx?.pluginConfig ?? {};
          const home = process.env.HOME || process.env.USERPROFILE;
          const convWs = home
            ? path.join(home, ".openclaw", `workspace-${agentId}`)
            : undefined;
          const resolved = resolveWorkspace([
            typeof cfg.workspaceDir === "string" ? cfg.workspaceDir : undefined,
            process.env.OPENCLAW_WORKSPACE,
            convWs,
            typeof ctx?.workspaceDir === "string" ? ctx.workspaceDir : undefined,
            typeof ctx?.cwd === "string" ? ctx.cwd : undefined,
          ]);
          if (!resolved) return; // no claim_note.sh + memory/ anywhere -> nothing to do

          const { script, memDir } = resolved;

          // 4) find notes THIS session currently holds; nothing held -> nothing to do
          const held = notesClaimedBy(memDir, claimant);
          if (held.length === 0) {
            // still stamp the throttle so we don't re-scan on every tool call
            lastRefreshByAgent.set(agentId, now);
            return;
          }

          // 5) mark throttle BEFORE spawning so concurrent tool calls don't pile up
          lastRefreshByAgent.set(agentId, now);

          // 6) fire-and-forget detached refresh for each held note; refresh self-
          //    guards (exit 3 / no-op if not ours), so this is always safe.
          for (const notePath of held) {
            const child = spawn(
              "bash",
              [script, "refresh", notePath, claimant],
              { detached: true, stdio: "ignore" },
            );
            child.on("error", () => {});
            child.unref();
          }
        } catch {
          // never let a hook error affect the tool result
        }
      },
      { priority: 0 },
    );
  },
});
