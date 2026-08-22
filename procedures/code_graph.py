#!/usr/bin/env python3
"""
code_graph.py — ZERO-LLM code-structure indexer for dinomem-neuron.

WHY (the thesis)
  Code STRUCTURE (imports/calls/inherits/contains) is deterministically
  extractable by a parser (tree-sitter AST). Spending LLM tokens to "understand"
  it is waste. Build the structural index ONCE at $0, then the agent queries a
  scoped subgraph (few hundred tokens) instead of grep+read whole files into
  context (~20x tokens for lower coverage). Ideas stolen from Graphify:
  AST-not-LLM, EXTRACTED/INFERRED edge provenance, path+line nodes.

DESIGN CONTRACT (settled — see note _note_project_code-graph-layer.md)
  - SEPARATE DB kb/code_graph/ (parallel to kb/vector_db_docs, kb/data_store).
    Never pollutes the memory graph.
  - REUSE graph_store.py store technology (SqliteBackend), NOT its DB file:
    graph_store.open(json_path=..., db_path=...) points the SAME backend at a
    2nd file. Payload shape = {nodes:[...], relation_edges:[...], edges:[...],
    + meta}. nodes carry .idx == list position (STABLE ID CONTRACT).
  - REUSE git_history.py AS-IS (diff_since / commit_count / rev-parse) — no new
    git code. Incremental rebuild + built_at_sha provenance + churn weight.
  - ZERO LLM in the build path. NEVER add an LLM pass here — that is the point.
  - FAIL-OPEN: missing tree-sitter -> code_graph offline, rest of neuron fine.
    Missing git -> full rebuild (not incremental). Missing store -> benign.
  - ROOT-RESOLUTION LADDER (no hardcoded path): --path arg > DINOMEM_CODE_ROOTS
    env > agent workspace > ~/.openclaw.
  - INGEST FILTER = extension allowlist (tree-sitter grammar set), .gitignore
    respected, skip .git/__pycache__/node_modules/venv/.dinomem-snap.git.

NODE/EDGE SCHEMA (code_graph_v1)
  node: {idx, id "<relpath>::<kind>::<name>::L<line>", kind file|function|class|
         method, name, file relpath, line, title, snippet, degree, churn}
  relation_edge: {a src idx, b tgt idx (or -1 unresolved), label calls|imports|
         inherits|contains, subject src name, object tgt name, confidence 1.0,
         provenance extracted|inferred, source "code_ast"}
  edges (similarity): [] empty — code is structural only.

USAGE
  python3 procedures/code_graph.py --path <root> [--lang py] [--json]
  (git incremental is step 3; this file ships the FULL-rebuild path first.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── resolve sibling modules (procedures/) so we reuse, never fork ────────────
_PROC_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PROC_DIR.parent
sys.path.insert(0, str(_PROC_DIR))

try:
    import graph_store as _gstore
except Exception as e:  # pragma: no cover - store is in-repo, should import
    print(f"[code_graph] FATAL: cannot import graph_store ({e})", file=sys.stderr)
    sys.exit(1)

try:
    import git_history as _gh
except Exception:
    _gh = None  # fail-open: no git enrichment (churn/incremental) available


# ── tree-sitter dep gate (FAIL-SOFT) ─────────────────────────────────────────
# Binding preference (frozen): tree_sitter_language_pack (single pip, bundled
# grammars) > (tree_sitter + tree_sitter_languages) > hard-offline. Missing dep
# is NOT a crash — code_graph goes offline, the rest of neuron is unaffected.
_TS_ERR = None
_ts_get_parser = None
try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser  # type: ignore
except Exception:
    try:
        from tree_sitter_languages import get_parser as _ts_get_parser  # type: ignore
    except Exception as e:
        _TS_ERR = (
            "tree-sitter not installed. Install ONE of:\n"
            "  pip install tree-sitter-language-pack   (preferred)\n"
            "  pip install tree-sitter tree-sitter-languages\n"
            f"(import error: {e})"
        )


def ts_available() -> bool:
    return _ts_get_parser is not None


# ── language map — extension allowlist -> tree-sitter grammar name ───────────
# START minimal (python only, proven), scaffold the rest for later steps.
LANG_BY_EXT = {
    ".py": "python",
    # scaffolded — enable as each grammar's query is written:
    # ".js": "javascript", ".jsx": "javascript",
    # ".ts": "typescript", ".tsx": "tsx",
    # ".sh": "bash", ".go": "go", ".rs": "rust",
}

SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv",
             ".dinomem-snap.git", ".mypy_cache", ".pytest_cache", "dist",
             "build", ".egg-info"}

CODE_SUBDIR = os.path.join("kb", "code_graph")
CODE_JSON = "code_graph.json"
CODE_DB = "graph.db"
PARSE_CACHE = "parse_cache.json"  # per-file AST result cache for incremental rebuild


# ── root-resolution ladder (no hardcoded path) ───────────────────────────────
def resolve_root(cli_path: str | None) -> str:
    """--path arg > DINOMEM_CODE_ROOTS env (first entry) > agent workspace
    (install-time sed placeholder / DINOMEM_WORKSPACE) > ~/.openclaw. Mirrors the
    repo-wide resolution pattern (graph_store __main__)."""
    if cli_path:
        return str(Path(cli_path).expanduser().resolve())
    env = os.environ.get("DINOMEM_CODE_ROOTS", "").strip()
    if env:
        first = env.split(os.pathsep)[0].split(",")[0].strip()
        if first:
            return str(Path(first).expanduser().resolve())
    ws = "DINOMEM_WORKSPACE_PLACEHOLDER"
    if ws.startswith("DINOMEM_"):  # sed did not run (dev/raw checkout)
        ws = os.environ.get("DINOMEM_WORKSPACE", "")
    if ws and Path(ws).exists():
        return str(Path(ws).resolve())
    return str(Path(os.path.expanduser("~/.openclaw")).resolve())


# ── .gitignore-aware file walk (extension allowlist) ─────────────────────────
def _load_gitignore(root: Path) -> list[str]:
    """Cheap .gitignore reader: returns simple name/glob patterns. Not a full
    gitignore engine (no negation/nesting) — SKIP_DIRS covers the noisy dirs;
    this catches project-declared ignores. Fail-open: no file -> []."""
    gi = root / ".gitignore"
    if not gi.exists():
        return []
    pats = []
    try:
        for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("!"):
                pats.append(s.rstrip("/"))
    except Exception:
        pass
    return pats


def _ignored(rel: str, name: str, patterns: list[str]) -> bool:
    import fnmatch
    for p in patterns:
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or ("/" + p) in ("/" + rel):
            return True
    return False


def walk_code_files(root: str, exts: set[str]) -> list[tuple[str, str, str]]:
    """Return [(abs_path, rel_path, lang)] for allowlisted source files under
    root. Skips SKIP_DIRS + .gitignore patterns. Extension allowlist, NOT
    hardcoded dirs."""
    root_p = Path(root)
    patterns = _load_gitignore(root_p)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.endswith(".egg-info")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            lang = exts.get(ext) if isinstance(exts, dict) else None
            if lang is None:
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            if _ignored(rel, fn, patterns):
                continue
            out.append((ap, rel, lang))
    return out


# ── AST extraction (python) ────────────────────────────────────────
# Each parsed file yields DEFS (function/class/method nodes) and RAW REFS
# (import/call/inherit by NAME). Name->idx resolution happens after ALL files
# are parsed (a call can target a def in another file). Zero LLM: pure AST.

def _txt(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1  # tree-sitter rows are 0-based


def parse_python(src: bytes, rel: str, parser):
    """Return (defs, refs) for one python file.
      defs: [{kind, name, file, line, qualname}]  (qualname = Class.method / func)
      refs: [{label calls|imports|inherits, from_qual, to_name, line, provenance}]
    from_qual is the enclosing def's qualname (or '<module>'). Resolution to idx
    is deferred. provenance starts 'inferred'; upgraded to 'extracted' when the
    target name resolves to an in-repo def during graph assembly."""
    tree = parser.parse(src)
    root = tree.root_node
    defs, refs = [], []

    def enclosing_qual(stack):
        return ".".join(stack) if stack else "<module>"

    def walk(node, stack):
        for child in node.children:
            t = child.type
            if t == "function_definition":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src) if nm else "<anon>"
                kind = "method" if stack else "function"
                qual = ".".join(stack + [name])
                defs.append({"kind": kind, "name": name, "file": rel,
                             "line": _line(child), "qualname": qual})
                walk(child, stack + [name])
            elif t == "class_definition":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src) if nm else "<anon>"
                qual = ".".join(stack + [name])
                defs.append({"kind": "class", "name": name, "file": rel,
                             "line": _line(child), "qualname": qual})
                # inherits: superclasses in the argument_list
                sup = child.child_by_field_name("superclasses")
                if sup:
                    for arg in sup.children:
                        if arg.type in ("identifier", "attribute"):
                            base = _txt(arg, src).split(".")[-1]
                            refs.append({"label": "inherits", "from_qual": qual,
                                         "to_name": base, "line": _line(child),
                                         "provenance": "inferred"})
                walk(child, stack + [name])
            elif t in ("import_statement", "import_from_statement"):
                for nm in child.children:
                    if nm.type == "dotted_name":
                        base = _txt(nm, src).split(".")[0]
                        refs.append({"label": "imports",
                                     "from_qual": enclosing_qual(stack),
                                     "to_name": base, "line": _line(child),
                                     "provenance": "inferred"})
            elif t == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    callee = _txt(fn, src).split(".")[-1]
                    if callee.isidentifier():
                        refs.append({"label": "calls",
                                     "from_qual": enclosing_qual(stack),
                                     "to_name": callee, "line": _line(child),
                                     "provenance": "inferred"})
                walk(child, stack)
            else:
                walk(child, stack)

    walk(root, [])
    return defs, refs


PARSERS = {"python": parse_python}


# ── graph assembly ─────────────────────────────────────────────
def _node_id(kind, name, rel, line):
    return f"{rel}::{kind}::{name}::L{line}"


def assemble_graph(parsed, root, built_at_sha):
    """parsed = [(rel, defs, refs), ...] -> graph_store payload dict.
    Node idx == list position (STABLE ID CONTRACT). A file node is created per
    file; defs get `contains` edges from their file. Refs resolve to a target
    node by NAME (last-def-wins index); resolved => provenance 'extracted' + real
    b idx, unresolved => provenance 'inferred' + b=-1."""
    nodes = []
    idx_of = {}          # node_id -> idx
    name_to_idx = {}     # bare name -> idx (for ref resolution; last wins)
    file_idx = {}        # rel -> file node idx

    def add_node(kind, name, rel, line):
        nid = _node_id(kind, name, rel, line)
        if nid in idx_of:
            return idx_of[nid]
        idx = len(nodes)
        nodes.append({
            "idx": idx, "id": nid, "kind": kind, "name": name,
            "file": rel, "line": line, "title": name,
            "snippet": f"{kind} {name} ({rel}:{line})", "degree": 0,
        })
        idx_of[nid] = idx
        name_to_idx[name] = idx
        return idx

    # pass 1: all def + file nodes (so cross-file refs can resolve)
    for rel, defs, _refs in parsed:
        file_idx[rel] = add_node("file", os.path.basename(rel), rel, 1)
        for d in defs:
            add_node(d["kind"], d["name"], d["file"], d["line"])

    rel_edges = []

    def add_edge(a, b, label, subj, obj, prov):
        rel_edges.append({
            "a": a, "b": b, "label": label, "subject": subj, "object": obj,
            "confidence": 1.0, "provenance": prov, "source": "code_ast",
        })
        if 0 <= a < len(nodes):
            nodes[a]["degree"] += 1
        if 0 <= b < len(nodes):
            nodes[b]["degree"] += 1

    # pass 2: contains (file -> defs) + resolve refs
    for rel, defs, refs in parsed:
        fi = file_idx[rel]
        for d in defs:
            di = idx_of[_node_id(d["kind"], d["name"], d["file"], d["line"])]
            add_edge(fi, di, "contains", os.path.basename(rel), d["name"], "extracted")
        for r in refs:
            src_name = r["from_qual"].split(".")[-1]
            a = name_to_idx.get(src_name, fi)  # enclosing def, else the file
            tgt = name_to_idx.get(r["to_name"])
            if tgt is not None:
                add_edge(a, tgt, r["label"], src_name, r["to_name"], "extracted")
            else:
                add_edge(a, -1, r["label"], src_name, r["to_name"], "inferred")

    # churn (git commit_count) as node weight — fail-open to 0
    if _gh is not None:
        for n in nodes:
            try:
                n["churn"] = _gh.commit_count(root, n["file"])
            except Exception:
                n["churn"] = 0
    else:
        for n in nodes:
            n["churn"] = 0

    lang_counts = {}
    for rel, defs, _ in parsed:
        lang_counts["files"] = lang_counts.get("files", 0) + 1

    payload = {
        "nodes": nodes,
        "relation_edges": rel_edges,
        "edges": [],  # code is structural only, no similarity edges
        "schema": "code_graph_v1",
        "built_at_sha": built_at_sha,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "root": root,
        "node_count": len(nodes),
        "edge_count": len(rel_edges),
        "lang_counts": lang_counts,
    }
    return payload


def _head_sha(root):
    if _gh is None:
        return None
    try:
        out = _gh._run(root, ["rev-parse", "HEAD"])
        return out.strip() if out else None
    except Exception:
        return None


def _parse_one(ap, rel, lang, errors):
    """Parse a single file -> (rel, defs, refs) or None on error (recorded)."""
    parser_fn = PARSERS.get(lang)
    if parser_fn is None:
        return None
    try:
        parser = _ts_get_parser(lang)
        with open(ap, "rb") as fh:
            src = fh.read()
        defs, refs = parser_fn(src, rel, parser)
        return (rel, defs, refs)
    except Exception as e:
        errors.append({"file": rel, "error": str(e)})
        return None


def _cache_path(root):
    return os.path.join(root, CODE_SUBDIR, PARSE_CACHE)


def _load_cache(root):
    """Prior per-file parse result {rel: {defs, refs, lang}}. {} if absent/bad
    (fail-open -> caller falls back to full rebuild)."""
    try:
        with open(_cache_path(root), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(root, cache):
    p = _cache_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, p)  # atomic


def _persist(parsed, cache, root, code_json_path, code_db_path, errors):
    """assemble -> store.replace_all + save parse cache. Shared tail of
    full+incremental (DRY). parsed = [(rel,defs,refs)]; cache mirrors it."""
    payload = assemble_graph(parsed, root, _head_sha(root))
    payload["parse_errors"] = errors
    store = _gstore.open(json_path=code_json_path, db_path=code_db_path)
    store.replace_all(payload)
    store.close()
    _save_cache(root, cache)
    return payload


def build_full(root, exts, code_json_path, code_db_path):
    """Full rebuild: walk -> parse EVERY file -> assemble -> store + cache."""
    files = walk_code_files(root, exts)
    parsed, cache, errors = [], {}, []
    for ap, rel, lang in files:
        r = _parse_one(ap, rel, lang, errors)
        if r is not None:
            parsed.append(r)
            cache[rel] = {"defs": r[1], "refs": r[2], "lang": lang}
    return _persist(parsed, cache, root, code_json_path, code_db_path, errors)


def build_incremental(root, exts, code_json_path, code_db_path):
    """Re-parse ONLY files changed since the DB's built_at_sha; reuse cached
    parse for the rest; drop deleted files. FAIL-OPEN to build_full when the
    preconditions aren't met (no git / no stored sha / empty cache / diff error).
    Whole-graph is re-assembled from the merged cache (idx stability across
    rebuilds is NOT required — the store is replaced wholesale each build)."""
    if _gh is None or not _gh.available(root):
        return build_full(root, exts, code_json_path, code_db_path), "full(no-git)"
    cache = _load_cache(root)
    if not cache:
        return build_full(root, exts, code_json_path, code_db_path), "full(no-cache)"
    try:
        store = _gstore.open(json_path=code_json_path, db_path=code_db_path)
        last_sha = store.meta().get("built_at_sha")
        store.close()
    except Exception:
        last_sha = None
    if not last_sha:
        return build_full(root, exts, code_json_path, code_db_path), "full(no-sha)"
    changes = _gh.diff_since(root, last_sha)
    if not changes:
        # nothing changed OR diff failed -> re-persist cache as-is (cheap, no reparse)
        parsed = [(rel, v["defs"], v["refs"]) for rel, v in cache.items()]
        return _persist(parsed, cache, root, code_json_path, code_db_path, []), "noop(no-changes)"
    # map allowlisted extensions for reparse decisions
    changed_reparsed = 0
    dropped = 0
    errors = []
    for ch in changes:
        rel = ch["path"]
        ext = os.path.splitext(rel)[1].lower()
        lang = exts.get(ext)
        if ch["status"] == "D":
            if cache.pop(rel, None) is not None:
                dropped += 1
            continue
        if lang is None:
            continue  # A/M of a non-allowlisted file -> ignore
        ap = os.path.join(root, rel)
        if not os.path.exists(ap):
            cache.pop(rel, None)  # renamed/vanished despite A/M
            continue
        r = _parse_one(ap, rel, lang, errors)
        if r is not None:
            cache[rel] = {"defs": r[1], "refs": r[2], "lang": lang}
            changed_reparsed += 1
    parsed = [(rel, v["defs"], v["refs"]) for rel, v in cache.items()]
    payload = _persist(parsed, cache, root, code_json_path, code_db_path, errors)
    return payload, f"incremental(reparsed={changed_reparsed} dropped={dropped})"


def _code_paths(root):
    base = os.path.join(root, CODE_SUBDIR)
    return os.path.join(base, CODE_JSON), os.path.join(base, CODE_DB)


def main():
    ap = argparse.ArgumentParser(description="ZERO-LLM code-structure indexer (dinomem-neuron)")
    ap.add_argument("--path", default=None, help="Root to index (ladder: --path > DINOMEM_CODE_ROOTS > workspace > ~/.openclaw)")
    ap.add_argument("--lang", default=None, help="Restrict to one lang key (e.g. python). Default: all allowlisted.")
    ap.add_argument("--full", action="store_true", help="Force full rebuild (default: incremental when a prior DB+sha+cache exist)")
    ap.add_argument("--json", action="store_true", help="Emit JSON summary")
    args = ap.parse_args()

    if not ts_available():
        msg = {"ok": False, "offline": True, "reason": _TS_ERR}
        print(json.dumps(msg) if args.json else f"[code_graph] OFFLINE: {_TS_ERR}", file=sys.stderr)
        sys.exit(3)

    root = resolve_root(args.path)
    exts = dict(LANG_BY_EXT)
    if args.lang:
        exts = {e: l for e, l in exts.items() if l == args.lang}
        if not exts:
            print(f"[code_graph] no allowlisted extension for lang '{args.lang}'", file=sys.stderr)
            sys.exit(2)

    code_json, code_db = _code_paths(root)
    if args.full:
        payload, mode = build_full(root, exts, code_json, code_db), "full(forced)"
    else:
        payload, mode = build_incremental(root, exts, code_json, code_db)

    summary = {
        "ok": True, "root": root, "db": code_db, "mode": mode,
        "nodes": payload["node_count"], "edges": payload["edge_count"],
        "built_at_sha": payload.get("built_at_sha"),
        "parse_errors": len(payload.get("parse_errors", [])),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[code_graph] {summary['mode']}: {summary['nodes']} nodes / {summary['edges']} edges "
              f"from {root}\n  db={code_db}  sha={summary['built_at_sha']}  "
              f"parse_errors={summary['parse_errors']}")


if __name__ == "__main__":
    main()
