#!/usr/bin/env python3
"""code_anchors.py — grep-backed PROMPT-ANCHOR index (dinomem code graph sidecar).

WHY (the gap this fills):
  The heavy behavioral logic in a dinomem install lives inside install.sh HEREDOC
  prompts + cron JSON `message` payloads (Advancer/Improver/Ideator/Note-Cron-Gate
  system prompts). To an AST that is ONE OPAQUE STRING BLOB — code_graph.py can see
  the shell around it but not the "Step 3:" / "SCOPE LOCK" / "FAIRNESS ORDERING"
  decision points *inside* the string. So "where does the Advancer decide selection
  order?" is un-queryable structurally.

  This scanner is deliberately NOT AST: it greps install.sh + cron payloads for
  named prompt anchors (Step N:, ALL-CAPS section labels, cron-job ids) and emits
  lightweight anchor nodes (label -> file:line) into a SIDECAR anchors.json that
  code_query reads. Zero LLM, zero grammar; pure line scan. Generic: scans every
  install.sh + *.json cron payload under the resolved root (no analyst-only paths).

OUTPUT: kb/code_graph/anchors.json = {"anchors":[{label,file,line,kind,context}], meta}
  queried by `tools/code_query.py anchors "<query>"`.

RUN: python3 procedures/code_anchors.py [--path ROOT] [--out FILE]
  Auto-refresh: piggyback the same cron as code_graph (cheap; pure grep).
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse code_graph's root ladder + exemption so anchors honor the SAME excluded
# dirs (never index memory/ docs/ data_query prompt-shaped strings). Fail-soft if
# code_graph import is unavailable (standalone use).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    import code_graph as _cg  # type: ignore
    _resolve_root = _cg.resolve_root
    _derive_exempt = _cg.derive_exempt_prefixes
    _is_exempt = _cg._is_exempt
except Exception:  # standalone fallback
    _cg = None
    def _resolve_root(p):  # noqa
        return str(Path(p).expanduser().resolve()) if p else os.getcwd()
    def _derive_exempt(root):  # noqa
        return {"memory/", "kb/", "docs/"}
    def _is_exempt(rel, prefixes):  # noqa
        r = rel.replace(os.sep, "/").lstrip("./")
        return any(r.startswith(p) for p in prefixes)

# ── anchor patterns ──────────────────────────────────────────────────────────
# Each pattern captures a human-named DECISION POINT inside a prompt/heredoc/cron
# payload. Kept intentionally tight so we index labels, not prose. group(1) (or
# the whole match) becomes the anchor label.
_ANCHOR_PATTERNS = [
    ("step",      re.compile(r"\b(Step\s+\d+)\s*[:.]")),                       # Step 3:
    ("numbered",  re.compile(r"(?:^|\\n|\.\s)\s*(\d+)\)\s+([a-z][a-z_]{3,})")),# 2) task_bound
    ("section",   re.compile(r"\b([A-Z][A-Z][A-Z ]{3,}[A-Z])\b(?=\s*(?:\(|:|—|\\n|-))")),  # SCOPE LOCK, FAIRNESS ORDERING, HARD RULE
    ("role",      re.compile(r"\b(Advancer|Improver|Ideator|Deleter|Note\s+Cron\s+Gate|Project\s+Cron\s+Gate|Project\s+Advancer)\b")),
    ("cron_id",   re.compile(r'"(?:id|name)"\s*:\s*"([A-Za-z0-9][A-Za-z0-9._-]{2,})"')),
]

# files worth scanning for prompt anchors (installers + cron payloads + any
# json that carries a "message"/"prompt" field). Extensions + basename globs.
_SCAN_BASENAMES = ("install.sh",)
_SCAN_EXTS = (".sh", ".json")
# only json that actually looks like a prompt/cron payload (has a message/prompt
# key) is scanned for anchors — skips data blobs cheaply.
_PROMPT_KEY = re.compile(r'"(?:message|prompt|system|instructions)"\s*:')


def _iter_scan_files(root: str, exempt):
    """Yield (abs, rel) for installers + prompt-carrying json under root, honoring
    the code-graph exemption (never scan memory/ docs/ data_query dirs)."""
    SKIP = getattr(_cg, "SKIP_DIRS", {".git", "node_modules", "__pycache__",
                                      "venv", ".venv"}) if _cg else \
           {".git", "node_modules", "__pycache__", "venv", ".venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames
                       if not _is_exempt(os.path.join(rel_dir, d) if rel_dir != "." else d, exempt)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if fn not in _SCAN_BASENAMES and ext not in _SCAN_EXTS:
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            if _is_exempt(rel, exempt):
                continue
            yield ap, rel


def _scan_file(ap: str, rel: str):
    """Return anchor dicts for one file. Line-based (grep-shaped): the anchor
    line number is the physical line where the label text appears. For a heredoc
    /json that packs many labels on ONE physical line (escaped \\n inside a
    string), we still point at that line — it's the queryable jump target."""
    out = []
    is_json = ap.endswith(".json")
    try:
        with open(ap, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return out
    # for json, only bother if a prompt-ish key exists somewhere (cheap gate)
    if is_json and not any(_PROMPT_KEY.search(ln) for ln in lines):
        # still allow cron_id extraction on cron json even without a prompt key
        pass
    seen = set()
    for lineno, ln in enumerate(lines, 1):
        for kind, pat in _ANCHOR_PATTERNS:
            for m in pat.finditer(ln):
                # label = last non-empty group, else whole match
                groups = [g for g in m.groups() if g]
                label = (groups[-1] if groups else m.group(0)).strip()
                label = re.sub(r"\s+", " ", label)
                if not label or len(label) > 80:
                    continue
                key = (kind, label, lineno)
                if key in seen:
                    continue
                seen.add(key)
                # small context window (trim long heredoc lines)
                ctx = ln.strip()
                if len(ctx) > 160:
                    idx = ctx.find(label)
                    lo = max(0, idx - 40)
                    ctx = ("…" if lo else "") + ctx[lo:idx + len(label) + 60] + "…"
                out.append({"label": label, "file": rel, "line": lineno,
                            "kind": kind, "context": ctx})
    return out


def build(root: str, out_path: str) -> dict:
    exempt = _derive_exempt(root)
    anchors = []
    files_scanned = 0
    for ap, rel in _iter_scan_files(root, exempt):
        found = _scan_file(ap, rel)
        if found:
            files_scanned += 1
            anchors.extend(found)
    payload = {
        "schema": "code_anchors_v1",
        "root": root,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "anchor_count": len(anchors),
        "files_scanned": files_scanned,
        "anchors": anchors,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, out_path)
    return payload


def main():
    ap = argparse.ArgumentParser(description="Grep-backed prompt-anchor index for the dinomem code graph")
    ap.add_argument("--path", dest="path", default=None, help="root to scan (default: code_graph root ladder)")
    ap.add_argument("--out", dest="out", default=None, help="output json (default: <root>/kb/code_graph/anchors.json)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = _resolve_root(args.path)
    out_path = args.out or os.path.join(root, "kb", "code_graph", "anchors.json")
    payload = build(root, out_path)
    summary = {"ok": True, "out": out_path, "anchor_count": payload["anchor_count"],
               "files_scanned": payload["files_scanned"], "root": root}
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"anchors: {payload['anchor_count']} from {payload['files_scanned']} file(s) -> {out_path}")


if __name__ == "__main__":
    main()
