import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

// dinomem-neuron: Note Claim Refresh
// ----------------------------------
// Keeps a live-session note claim fresh for as long as the session is ACTIVELY
// WORKING IN THE SAME WORKSPACE as the claimed note.
//
// WHY THE BROADENED TRIGGER (original caf5242 rationale, preserved):
// A live session can legitimately hold a claim on note X while spending an
// hour doing exec/git/read/other-file work (e.g. building a repo the note
// tracks). No note edit -> no refresh -> claim aged past 120 min -> a cron
// reclaimed a note a human was still actively working (observed 2026-07-10).
//
// WHY WE SCOPE IT BACK DOWN (fix, 2026-09-25):
// The caf5242 broadening used "any tool call by this agentId" as the liveness
// signal. That's too wide: it refreshes ALL notes claimed by live-session-<id>
// even when the tool call belongs to a completely unrelated chat session on the
// same agent (e.g. a market-analysis chat while a project note sits claimed
// from a previous session). Effect: claimed_at never expires -> Advancer always
// skips the note -> zero project advancement while the agent is active at all.
//
// FIX: two-tier liveness signal:
//   Tier 1 (STRONG): the tool call touches a path inside the same workspace
//     as the claimed note (read/write/exec with a path arg under ws/). This is
//     the real "I'm working on this project" signal. Refresh unconditionally
//     (up to REFRESH_EVERY_MS throttle).
//   Tier 2 (WEAK / grace): no workspace-path signal in this tool call, but
//     the claim is still very fresh (< GRACE_NO_PATH_MS since claimed_at).
//     Refresh once more to cover the gap between claim-write and the first
//     real path-touching tool call. After the grace window, no path signal =
//     no refresh = claim expires naturally, Advancer can pick it up.
//
// Throttle key is now sessionKey (not agentId) so two concurrent sessions on
// the same agent don't share a throttle bucket and accidentally suppress each
// other's refreshes.
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

// Refresh at most this often per session. Well under the 120-min live-session
// window, so a claim can never lapse while the session keeps working.
const REFRESH_EVERY_MS = 5 * 60 * 1000; // 5 minutes

// After this many ms since claimed_at, a tool call with NO workspace-path
// signal will no longer refresh the claim. Keeps the grace window tight so
// unrelated chats don't hold claims open indefinitely.
const GRACE_NO_PATH_MS = 30 * 60 * 1000; // 30 minutes

// Last-refresh timestamp per sessionKey (module-scoped; process-lifetime memo).
// Keyed by sessionKey (not agentId) so concurrent sessions don't share buckets.
const lastRefreshBySession = new Map<string, number>();

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

// Return absolute paths of _note_*.md files currently claimed by `claimant`,
// along with their claimed_at timestamp (parsed from frontmatter).
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

interface ClaimedNote {
  notePath: string;
  claimedAt: Date | undefined; // parsed from frontmatter; undefined if missing/unparseable
}

function notesClaimedBy(memDir: string, claimant: string): ClaimedNote[] {
  let entries: string[];
  try {
    entries = fs.readdirSync(memDir);
  } catch {
    return [];
  }
  const needle = `claimed_by: ${claimant}`;
  const out: ClaimedNote[] = [];
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
        if (!head.includes(needle)) continue;

        // Parse claimed_at from frontmatter for the grace-window check.
        let claimedAt: Date | undefined;
        const caMatch = /^claimed_at:\s*(.+)$/m.exec(head);
        if (caMatch) {
          const d = new Date(caMatch[1].trim());
          if (!isNaN(d.getTime())) claimedAt = d;
        }
        out.push({ notePath: full, claimedAt });
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      /* unreadable note -> skip */
    }
  }
  return out;
}

// Extract all string-valued path-like arguments from a tool call event.
// We look at common argument shapes: positional strings, named path/file/
// command fields, and nested objects one level deep. This is best-effort;
// false negatives just fall back to the grace-window tier.
function extractPathArgs(event: any): string[] {
  const args = event?.args ?? event?.input ?? event?.arguments ?? {};
  const paths: string[] = [];
  const visit = (v: unknown) => {
    if (typeof v === "string" && v.length > 1 && (v.startsWith("/") || v.includes("/"))) {
      paths.push(v);
    }
  };
  if (typeof args === "string") {
    visit(args);
  } else if (Array.isArray(args)) {
    for (const a of args) visit(a);
  } else if (args && typeof args === "object") {
    for (const [k, v] of Object.entries(args)) {
      if (typeof v === "string") {
        visit(v);
      } else if (
        typeof v === "object" &&
        v !== null &&
        !Array.isArray(v) &&
        ["path", "file", "command", "input", "cwd", "workdir"].includes(k)
      ) {
        // one level deep for known path-bearing keys
        for (const vv of Object.values(v as object)) visit(vv);
      }
    }
  }
  return paths;
}

// Returns true if any of the extracted path args falls inside `wsRoot`.
function toolCallTouchesWorkspace(pathArgs: string[], wsRoot: string): boolean {
  const norm = wsRoot.endsWith("/") ? wsRoot : wsRoot + "/";
  return pathArgs.some((p) => p.startsWith(norm) || p === wsRoot);
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
          const sessionKey =
            ctx?.sessionKey ?? event?.sessionKey ?? ctx?.session ?? event?.session;
          const agentId =
            agentIdFromSessionKey(sessionKey) ??
            (typeof ctx?.agentId === "string" ? ctx.agentId : undefined);
          if (!agentId) return;
          const claimant = `live-session-${agentId}`;

          // 2) time throttle — at most one refresh per REFRESH_EVERY_MS per session
          //    (keyed by sessionKey so concurrent sessions don't share buckets)
          const throttleKey = typeof sessionKey === "string" ? sessionKey : agentId;
          const now = Date.now();
          const last = lastRefreshBySession.get(throttleKey) ?? 0;
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

          const { ws, script, memDir } = resolved;

          // 4) find notes THIS session currently holds; nothing held -> nothing to do
          const held = notesClaimedBy(memDir, claimant);
          if (held.length === 0) {
            lastRefreshBySession.set(throttleKey, now);
            return;
          }

          // 5) path-signal check: does this tool call touch the workspace?
          //    Tier 1 (STRONG): path arg inside ws/ -> refresh this note.
          //    Tier 2 (WEAK/grace): no path signal but claim is very fresh
          //      (< GRACE_NO_PATH_MS since claimed_at) -> refresh once more.
          //    Otherwise: no refresh -> claim expires naturally.
          const pathArgs = extractPathArgs(event);
          const hasWorkspacePath = toolCallTouchesWorkspace(pathArgs, ws);

          const toRefresh: string[] = [];
          for (const { notePath, claimedAt } of held) {
            if (hasWorkspacePath) {
              // Tier 1: strong signal — always refresh
              toRefresh.push(notePath);
            } else if (claimedAt !== undefined) {
              // Tier 2: grace window — refresh only if claim is still very fresh
              const claimAgeMs = now - claimedAt.getTime();
              if (claimAgeMs < GRACE_NO_PATH_MS) {
                toRefresh.push(notePath);
              }
              // else: claim is old and no workspace activity -> let it expire
            } else {
              // claimed_at unparseable -> conservative: apply grace (don't block)
              toRefresh.push(notePath);
            }
          }

          if (toRefresh.length === 0) {
            // nothing to refresh this tick; still stamp throttle
            lastRefreshBySession.set(throttleKey, now);
            return;
          }

          // 6) mark throttle BEFORE spawning so concurrent tool calls don't pile up
          lastRefreshBySession.set(throttleKey, now);

          // 7) fire-and-forget detached refresh for each qualifying note; refresh
          //    self-guards (exit 3 / no-op if not ours), so this is always safe.
          for (const notePath of toRefresh) {
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
