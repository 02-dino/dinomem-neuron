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

SCOPE DECISION (CAPPED — do NOT index the world)
  Coverage = big-six + agent-config: python/bash/json/markdown/JS/TS SHIPPED.
  40-language parity is Graphify's PRODUCT need, NOT ours. Go/Rust/Java/C#/C++ +
  niche langs (fortran/ocaml/pascal/zig/etc) are ADD-ON-DEMAND: add a
  parse_<lang> + 2 lines (LANG_BY_EXT + PARSERS) ONLY when a real buyer file
  exists. The scaffold makes that a ~2-line job — so there is no reason to
  pre-index languages nobody in the install actually ships. Do NOT index the
  world; a bloated grammar set is slower, heavier, and buys nothing without
  dogfood files. (Rationale + rulings: _note_project_code-graph-multilang.md.)

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
    ".sh": "bash", ".bash": "bash",
    ".json": "json",
    ".md": "markdown", ".markdown": "markdown",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    # scaffolded — add-on-demand (add a parse_<lang> + 2 lines per scope doc):
    # ".go": "go", ".rs": "rust",
}

SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv",
             ".dinomem-snap.git", ".mypy_cache", ".pytest_cache", "dist",
             "build", ".egg-info"}

# ── path-scoped ingestion exemption ──────────────────────────────────────────
# Dirs already OWNED by another retrieval surface (memory_search, docs_search,
# data_query) must NOT be re-ingested into the code graph — that would double-
# index the same content under two routers and clash on recall. We AUTO-DERIVE
# the exempt-prefix set from EXISTING catalogs (no hand-maintained list):
#   (a) memory/           -> owned by memory_search (hard default; always)
#   (b) kb/               -> our own index store (never source code)
#   (c) docs_ingest DOCS_DIR (rel) -> owned by docs_search (RAG)
#   (d) data_register catalog source paths -> owned by data_query (SQL)
# Buyers add their own RAG dirs via env DINOMEM_CODE_GRAPH_EXEMPT (colon-list).
# Fail-open everywhere: a missing catalog / unreadable file contributes nothing,
# never crashes the walk. Prefixes are rel-to-root, normalized with trailing "/".
_EXEMPT_HARD = ("memory/", "kb/")  # always owned by memory_search / index store

def _norm_prefix(p: str) -> str:
    p = p.replace(os.sep, "/").strip().strip("/")
    return (p + "/") if p else ""

def derive_exempt_prefixes(root: str) -> set[str]:
    """Union of hard defaults + docs_ingest DOCS_DIR + data_query catalog source
    dirs + env override, each as a rel-to-root prefix ('memory/', 'docs/', ...).
    Every source is best-effort: any failure yields no prefix, never raises."""
    root_p = Path(root)
    prefixes = set(_EXEMPT_HARD)

    # (c) docs_search RAG dir (docs_ingest DOCS_DIR). Only exempt when it lives
    # UNDER root (a shared workspace docs/ dir); an absolute-elsewhere DOCS_DIR
    # can't collide with this walk anyway.
    try:
        docs_dir = os.environ.get("DINOMEM_DOCS_DIR", "").strip() or str(root_p / "docs")
        dp = Path(docs_dir).expanduser().resolve()
        rel = os.path.relpath(dp, root_p)
        if not rel.startswith("..") and rel != ".":
            prefixes.add(_norm_prefix(rel))
    except Exception:
        pass

    # (d) data_query catalog: exempt the DIRECTORY of each registered source file.
    try:
        cat = root_p / "kb" / "data_store" / "catalog.json"
        if cat.exists():
            data = json.loads(cat.read_text(encoding="utf-8", errors="replace"))
            entries = data.values() if isinstance(data, dict) else (data or [])
            for e in entries:
                if not isinstance(e, dict):
                    continue
                for key in ("source", "source_file", "file", "path", "sources"):
                    val = e.get(key)
                    if not val:
                        continue
                    for src in (val if isinstance(val, list) else [val]):
                        try:
                            sp = Path(str(src)).expanduser().resolve()
                            rel = os.path.relpath(sp.parent, root_p)
                            if not rel.startswith("..") and rel != ".":
                                prefixes.add(_norm_prefix(rel))
                        except Exception:
                            continue
    except Exception:
        pass

    # env override (buyer-supplied colon-list of rel or abs dirs)
    env = os.environ.get("DINOMEM_CODE_GRAPH_EXEMPT", "").strip()
    if env:
        for raw in env.split(os.pathsep):
            raw = raw.strip()
            if not raw:
                continue
            try:
                if os.path.isabs(raw):
                    rel = os.path.relpath(Path(raw).resolve(), root_p)
                    if rel.startswith("..") or rel == ".":
                        continue
                    prefixes.add(_norm_prefix(rel))
                else:
                    prefixes.add(_norm_prefix(raw))
            except Exception:
                continue

    prefixes.discard("")
    return prefixes

def _is_exempt(rel: str, prefixes: set[str]) -> bool:
    r = rel.replace(os.sep, "/").lstrip("./")
    return any(r.startswith(p) for p in prefixes)

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
    exempt = derive_exempt_prefixes(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.endswith(".egg-info")]
        # prune exempt dirs at the directory level (cheap; avoids descending
        # into memory/ kb/ docs/ data_query source dirs entirely)
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames
                       if not _is_exempt(os.path.join(rel_dir, d) if rel_dir != "." else d, exempt)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            lang = exts.get(ext) if isinstance(exts, dict) else None
            if lang is None:
                continue
            ap = os.path.join(dirpath, fn)
            rel = os.path.relpath(ap, root)
            if _ignored(rel, fn, patterns):
                continue
            if _is_exempt(rel, exempt):
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


# ── AST extraction (bash) ──────────────────────────────────────────
# Bash has no import system; the structural signal is (1) function_definition
# nodes and (2) `source`/`.` of another script (an import/source edge) and (3)
# a command whose name is a graph-known local function (a calls edge). Unresolved
# command names are DROPPED (every bare word in bash is a 'command' — echo, ls,
# grep, etc. — so an unresolved-name edge would be almost entirely noise; unlike
# python where an unresolved call still names a real symbol). Zero LLM; pure AST.
#
# Clean-room implementation. Approach cross-checked against graphify's
# extractors/bash.py + symbol_resolution.resolve_bash_source_edges (Apache-2.0)
# as REFERENCE only; no code lifted (their extractor is wired to their store).
_BASH_BUILTINS = {
    # never emit calls edges for these even if a same-named local fn exists — they
    # are overwhelmingly the shell builtin, not a user function.
    "echo", "printf", "cd", "test", "[", "[[", "read", "exit", "return",
    "local", "export", "set", "unset", "shift", "eval", "source", ".",
    "true", "false", ":", "trap", "wait", "kill", "exec", "declare",
    "readonly", "getopts", "shopt", "builtin", "command", "type", "hash",
    # shell keywords / control words that appear as command_name in the grammar
    "continue", "break", "then", "else", "elif", "fi", "do", "done",
    "case", "esac", "while", "until", "for", "if", "function", "in",
}

def _bash_cmd_name(cmd, src: bytes):
    """First command_name child's literal text (bash `command` has no 'name'
    field; child_by_field_name('name') returns the wrong node). None if absent."""
    for c in cmd.children:
        if c.type == "command_name":
            return _txt(c, src).strip()
    return None

def _bash_source_arg(cmd, src: bytes):
    """The path argument of a `source`/`.` command -> basename stem for matching.
    Reads the first string/word sibling after command_name; strips quotes and
    any leading $VAR/dir components so `"$DIR/lib/gate_lib.sh"` -> 'gate_lib.sh'.
    Returns the basename (with extension) or None."""
    arg = None
    seen_name = False
    for c in cmd.children:
        if c.type == "command_name":
            seen_name = True
            continue
        if seen_name and c.type in ("string", "word", "raw_string", "concatenation"):
            arg = _txt(c, src)
            break
    if not arg:
        return None
    arg = arg.strip().strip('"').strip("'")
    # keep only the final path component (basename); ignores $DIR/ prefixes
    base = arg.replace("\\", "/").rstrip("/").split("/")[-1]
    return base or None

def parse_bash(src: bytes, rel: str, parser):
    """Return (defs, refs) for one bash file.
      defs: function_definition nodes -> {kind function, name, file, line, qualname}
      refs: calls (command name == a local fn, resolved later) + imports
            (source/. of another script; to_name = sourced basename).
    Bash is flat (no nested-class scope); qualname == name. Resolution to idx is
    deferred to assemble_graph (same contract as python)."""
    tree = parser.parse(src)
    root = tree.root_node
    defs, refs = [], []

    # pass A: collect this file's own function names (enclosing-scope tagging is
    # flat in bash; a call's from_qual is best-effort the nearest enclosing fn).
    def collect_defs(node):
        for child in node.children:
            if child.type == "function_definition":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src).strip() if nm else None
                if name:
                    defs.append({"kind": "function", "name": name, "file": rel,
                                 "line": _line(child), "qualname": name})
                collect_defs(child)
            else:
                collect_defs(child)
    collect_defs(root)

    # pass B: refs. A command inside a function def is attributed to that fn;
    # otherwise to '<module>'. imports (source/.) always attribute to '<module>'
    # (they run at load time). calls to same-file/other-file local fns resolve
    # in assembly by NAME; builtins + unknown externals are skipped here.
    def walk_refs(node, enclosing):
        for child in node.children:
            if child.type == "function_definition":
                nm = child.child_by_field_name("name")
                fn_name = _txt(nm, src).strip() if nm else enclosing
                walk_refs(child, fn_name or enclosing)
            elif child.type == "command":
                cname = _bash_cmd_name(child, src)
                if cname in ("source", "."):
                    base = _bash_source_arg(child, src)
                    if base:
                        refs.append({"label": "imports", "from_qual": "<module>",
                                     "to_name": base, "line": _line(child),
                                     "provenance": "inferred", "_bash_source": True})
                elif cname and cname not in _BASH_BUILTINS \
                        and not cname.startswith("$") and "/" not in cname \
                        and not cname.startswith(("\"", "'")) \
                        and cname.replace("_", "").replace("-", "").isalnum():
                    # candidate local-fn call; kept ONLY if it resolves to a
                    # graph-known function during assembly (unresolved dropped).
                    refs.append({"label": "calls", "from_qual": enclosing,
                                 "to_name": cname, "line": _line(child),
                                 "provenance": "inferred", "_bash_call": True})
                walk_refs(child, enclosing)
            else:
                walk_refs(child, enclosing)
    walk_refs(root, "<module>")
    return defs, refs

# ── AST extraction (json / config) ───────────────────────────────────
# Config files are structural too: a nested key path (channels.telegram.actions)
# is a navigable location, and a string VALUE that is a path to a file already in
# the graph is a structural reference (a config wiring code). We model:
#   (a) each object key -> a `config_key` node id'd by its DOTTED PATH (so
#       `channels.telegram.actions` is one node, queryable by code_query), with
#       a `contains` edge parent-key -> child-key (nesting) and file -> top-key.
#   (b) a string value that resolves to a graph-known file -> `references` edge
#       from the enclosing key node to that file node.
# Array indices are NOT keyed (noise); we descend into array elements to catch
# path-valued strings but don't emit a node per index. data_query-registered
# dataset json is already excluded by the step-1 walk exemption, so this only
# fires on real config. Zero LLM; pure AST. Clean-room (graphify json_config ref).
#
# Key-node explosion guard: giant generated json (lockfiles, vendored blobs) can
# have thousands of keys. We cap nested key-nodes per file + depth so a config
# stays navigable without flooding the graph.
_JSON_MAX_KEYS_PER_FILE = 400
_JSON_MAX_DEPTH = 8

def _json_str_value(node, src: bytes):
    """Literal text of a JSON string node without the surrounding quotes."""
    for c in node.children:
        if c.type == "string_content":
            return _txt(c, src)
    # empty string "" has no string_content child
    return "" if node.type == "string" else None

def _looks_like_path(s: str) -> bool:
    """Cheap filter: a value worth trying to resolve to a file node. Must contain
    a path separator or a known source extension, and no whitespace/url scheme."""
    if not s or len(s) > 300 or " " in s or "://" in s or s.startswith("$"):
        return False
    return ("/" in s) or s.rsplit(".", 1)[-1].lower() in (
        "py", "sh", "bash", "js", "ts", "json", "md", "go", "rs")

def parse_json(src: bytes, rel: str, parser):
    """Return (defs, refs) for one json/config file.
      defs: config_key nodes (dotted-path name) + the enclosing file node's
            children; each carries kind 'config_key', name=dotted path, line.
      refs: `contains` (parent-key -> child-key nesting; resolved by unique
            dotted-path name) + `references` (key -> a graph-known file whose
            path == a string value; resolved in assembly by file basename/rel).
    We id key nodes by DOTTED PATH so nesting is queryable; qualname == path."""
    tree = parser.parse(src)
    root = tree.root_node
    defs, refs = [], []
    key_count = [0]

    def descend(node, prefix, depth):
        # node is an object/array/value; prefix is the dotted key path to it.
        if node.type == "object":
            for pair in node.children:
                if pair.type != "pair":
                    continue
                key_node = pair.child_by_field_name("key")
                val_node = pair.child_by_field_name("value")
                if key_node is None:
                    continue
                key = _json_str_value(key_node, src)
                if key is None:
                    continue
                path = f"{prefix}.{key}" if prefix else key
                if depth <= _JSON_MAX_DEPTH and key_count[0] < _JSON_MAX_KEYS_PER_FILE:
                    defs.append({"kind": "config_key", "name": path, "file": rel,
                                 "line": _line(key_node), "qualname": path})
                    key_count[0] += 1
                    # nesting edge: parent key path -> this key path
                    if prefix:
                        refs.append({"label": "contains", "from_qual": prefix,
                                     "to_name": path, "line": _line(key_node),
                                     "provenance": "extracted", "_json_key": True})
                    else:
                        # top-level key: file -> key
                        refs.append({"label": "contains", "from_qual": "<file>",
                                     "to_name": path, "line": _line(key_node),
                                     "provenance": "extracted", "_json_topkey": True})
                if val_node is not None:
                    _handle_value(val_node, path, depth + 1)

    def _handle_value(val, path, depth):
        if val.type == "object":
            descend(val, path, depth)
        elif val.type == "array":
            for el in val.children:
                if el.type in ("object", "array"):
                    _handle_value(el, path, depth)
                elif el.type == "string":
                    _maybe_path_ref(el, path)
        elif val.type == "string":
            _maybe_path_ref(val, path)

    def _maybe_path_ref(strnode, path):
        s = _json_str_value(strnode, src)
        if s and _looks_like_path(s):
            base = s.replace("\\", "/").rstrip("/").split("/")[-1]
            if base:
                refs.append({"label": "references", "from_qual": path,
                             "to_name": base, "line": _line(strnode),
                             "provenance": "inferred", "_json_ref": True})

    # root is a `document` node wrapping the top-level value; descend into its
    # object child (a top-level array/scalar has no keys -> nothing to extract).
    for child in root.children:
        if child.type == "object":
            descend(child, "", 1)
            break
    return defs, refs

# ── AST extraction (markdown) ─ CODE-REPO PATHS ONLY ─────────────────────
# Markdown in a CODE repo (skills/, github/, project docs) structurally wires
# docs to code: a SKILL.md that links [x](scripts/foo.sh) or names a script in a
# fenced code block is DOCUMENTING that code. We model:
#   (a) each heading -> a `section` node (name = heading text, file:line).
#   (b) a markdown link [text](path) OR a fenced-code line that is a bare path,
#       where the path resolves to a graph-known file -> `documents` edge from
#       the file's doc node to that code file node (unresolved DROPPED, like the
#       bash/json externals — an out-of-repo link is noise).
# CRITICAL: memory/ and docs/ are ALREADY exempt (step 1), so this only fires on
# CODE-REPO markdown, never our _note_/_pin_ files (owned by memory_search) or
# docs_search's RAG corpus. Structural only — no semantic overlap with those.
# The block tree splits link URLs into char nodes, so we regex the raw text for
# link targets + scan code-fence content (cheaper + grammar-version-robust than
# threading the markdown_inline sub-parser). Zero LLM. Clean-room (graphify ref).
import re as _re
_MD_LINK = _re.compile(r"\]\(\s*<?([^)>\s]+)>?\s*(?:\"[^\"]*\")?\)")
_MD_MAX_SECTIONS = 300

def _md_heading_text(node, src: bytes):
    """Text of an atx_heading / setext_heading: the inline child's literal."""
    for c in node.children:
        if c.type == "inline":
            return _txt(c, src).strip()
    # fallback: strip leading #'s from the whole node
    return _txt(node, src).lstrip("#").strip()

def parse_markdown(src: bytes, rel: str, parser):
    """Return (defs, refs) for one CODE-REPO markdown file.
      defs: section nodes (heading text, file:line).
      refs: `documents` edges (doc -> a graph-known file named by a link target
            or a code-fence path line; resolved in assembly by basename).
    from_qual is '<file>' (the doc file node is the documenter); resolution maps
    it to this file's node. Unresolved targets are DROPPED."""
    tree = parser.parse(src)
    root = tree.root_node
    defs, refs = [], []
    sec_count = [0]
    seen_targets = set()

    def add_ref(target, line):
        base = target.replace("\\", "/").rstrip("/").split("/")[-1]
        base = base.split("#")[0].split("?")[0]  # strip anchor/query
        if not base or base in seen_targets:
            return
        # only path-like code targets (has an ext we index or a dir separator)
        low = base.rsplit(".", 1)[-1].lower()
        if low not in ("py", "sh", "bash", "js", "ts", "tsx", "jsx", "go",
                       "rs", "json", "md") and "/" not in target:
            return
        seen_targets.add(base)
        refs.append({"label": "documents", "from_qual": "<file>",
                     "to_name": base, "line": line, "provenance": "inferred",
                     "_md_ref": True})

    def walk(node):
        for child in node.children:
            if child.type in ("atx_heading", "setext_heading"):
                if sec_count[0] < _MD_MAX_SECTIONS:
                    txt = _md_heading_text(child, src)
                    if txt:
                        name = txt[:120]
                        defs.append({"kind": "section", "name": name, "file": rel,
                                     "line": _line(child), "qualname": name})
                        sec_count[0] += 1
            elif child.type == "fenced_code_block":
                for gc in child.children:
                    if gc.type == "code_fence_content":
                        for ln in _txt(gc, src).splitlines():
                            s = ln.strip().strip("`").strip()
                            # a bare path line inside a code fence
                            if s and " " not in s and ("/" in s or "." in s):
                                add_ref(s, _line(gc))
            elif child.type in ("paragraph", "inline"):
                for m in _MD_LINK.finditer(_txt(child, src)):
                    add_ref(m.group(1), _line(child))
            walk(child)
    walk(root)
    return defs, refs

# ── AST extraction (javascript / typescript / jsx / tsx) ─────────────────
# One extractor serves all four: the tree-sitter node types overlap (function_
# declaration / class_declaration / import_statement / call_expression are
# identical across the js/ts/tsx grammars; the parser OBJECT differs per grammar
# via _ts_get_parser, but the walk is the same). TS-only nodes (interface_
# declaration / type_alias_declaration) are captured as def nodes but need no
# special edge logic in v1.
#
# Structural signal (verified against the live grammar walk):
#   DEFS:
#     function_declaration (name field)                 -> function
#     variable_declarator whose value is arrow_function
#       or function_expression (name = declared ident)  -> function
#     method_definition (property_identifier) in a class -> method (Class.method)
#     class_declaration (name)                           -> class
#     interface_declaration / type_alias_declaration     -> type (TS only)
#   inherits:
#     class_heritage -> extends -> identifier            -> inherits edge
#   imports:
#     import_statement trailing string->string_fragment  -> imports (module path)
#     call_expression whose fn ident == require + string  -> imports (CommonJS)
#     -> resolve module path to a graph-known file by BASENAME (strip ./ ../,
#        try .js/.jsx/.ts/.tsx + index.*). Bare pkg names / node_modules DROP
#        (external, like bash externals / json external path-values).
#   calls:
#     call_expression fn = plain identifier               -> calls (callee)
#     call_expression fn = member_expression (obj.method)  -> calls (last segment)
#
# Enclosing-qualname tracking = same name-stack walk as parse_python. Zero LLM.
# Clean-room; graphify's extractors/ JS/TS + resolution.py (Apache-2.0) consulted
# as REFERENCE ONLY (their 30+ _js_* helpers are wired to their store; v1 does
# BASENAME resolution, not tsconfig-alias/workspace-glob — that's a store-specific
# product need, not required for the cross-file import/call/inherits edges).
_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

def _js_module_basename(spec: str):
    """A JS/TS import/require module specifier -> a basename candidate for
    matching a graph-known file node, or None if it's an EXTERNAL package.
    Relative ('./util', '../lib/bar') -> keep final path component, strip ext,
    return the stem (assembly resolves by file-node basename). Bare specifiers
    ('react', '@scope/pkg', 'node:fs') -> None (external -> DROP)."""
    if not spec:
        return None
    spec = spec.strip().strip('"').strip("'")
    if not spec:
        return None
    # external: bare package name / scoped pkg / node: builtin (not path-like)
    if not (spec.startswith("./") or spec.startswith("../") or spec.startswith("/")):
        return None
    base = spec.replace("\\", "/").rstrip("/").split("/")[-1]
    if not base:
        return None  # e.g. import from './dir/' -> index.* (unresolved by basename v1)
    # strip a known extension so './util' and './util.js' both -> 'util'
    for ext in _JS_EXTS:
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    return base or None

def _strip_jsonc(text: str) -> str:
    """tsconfig.json is JSONC (comments + trailing commas). Tolerant strip:
    remove // line comments, /* */ block comments, and trailing commas, so a
    plain json.loads succeeds. Best-effort — not a full JSON5 parser."""
    out = []
    i, n = 0, len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    s = "".join(out)
    # drop trailing commas: ,] and ,}
    s = _re.sub(r",(\s*[\]}])", r"\1", s)
    return s

def _load_ts_alias_map(root: str):
    """Build-time (once per root) tsconfig `paths`/`baseUrl` + package.json
    `workspaces` -> an ALIAS_MAP: {alias_prefix: [resolved_dir_rel_to_root,...]}.
    Powers #1 aliased-import resolution (`@/lib/x` -> the real file). FAIL-OPEN:
    a missing/malformed config contributes nothing, never raises. Env override
    DINOMEM_TS_PATHS (colon list of `alias=dir`) adds/overrides entries.
    Alias keys are stored WITHOUT the trailing `/*` (e.g. `@` for `@/*`, `@lib`
    for `@lib/*`); an EXACT (no-star) alias is stored as-is. Values are dirs
    rel-to-root with any trailing `/*` stripped."""
    amap = {}

    def _add(alias, target):
        alias = alias.strip()
        target = target.strip().replace("\\", "/")
        star_a = alias.endswith("/*")
        key = alias[:-2] if star_a else alias
        val = target[:-2] if target.endswith("/*") else target
        val = val.lstrip("./").rstrip("/")
        if key:
            amap.setdefault(key, [])
            if val not in amap[key]:
                amap[key].append(val)

    for cfg in ("tsconfig.json", "tsconfig.base.json"):
        p = os.path.join(root, cfg)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.loads(_strip_jsonc(fh.read()))
            co = data.get("compilerOptions", {}) or {}
            base_url = (co.get("baseUrl") or "").strip().replace("\\", "/")
            base_url = base_url.lstrip("./").rstrip("/")
            paths = co.get("paths", {}) or {}
            for alias, targets in paths.items():
                if not isinstance(targets, list):
                    continue
                for tgt in targets:
                    if not isinstance(tgt, str):
                        continue
                    # prepend baseUrl if the target is relative to it
                    joined = tgt
                    if base_url and not tgt.startswith((".", "/")):
                        joined = base_url.rstrip("/") + "/" + tgt.lstrip("/")
                    _add(alias, joined)
        except Exception:
            continue  # fail-open

    # package.json workspaces globs -> pkg roots (basename alias -> dir), so a
    # monorepo import by pkg name has a chance to resolve. Best-effort: a glob
    # like `packages/*` maps a later bare-ish import to that dir tree; we only
    # record the glob PREFIX dirs (the '*' is expanded at match time by prefix).
    pj = os.path.join(root, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read())
            ws = data.get("workspaces")
            globs = []
            if isinstance(ws, list):
                globs = ws
            elif isinstance(ws, dict) and isinstance(ws.get("packages"), list):
                globs = ws["packages"]
            for g in globs:
                if isinstance(g, str) and g:
                    d = g.replace("\\", "/").rstrip("/")
                    d = d[:-2] if d.endswith("/*") else d
                    amap.setdefault("__workspace_roots__", [])
                    if d not in amap["__workspace_roots__"]:
                        amap["__workspace_roots__"].append(d)
        except Exception:
            pass  # fail-open

    env = os.environ.get("DINOMEM_TS_PATHS", "")
    if env:
        for pair in env.split(os.pathsep):
            if "=" in pair:
                a, d = pair.split("=", 1)
                _add(a, d)

    return amap

def _resolve_ts_alias(spec: str, alias_map: dict):
    """An aliased import spec (`@/lib/x`, `@lib/util`) -> a candidate (stem,
    rel_dir_prefix) for matching a file node, or None if no alias matches.
    Returns (stem, [dir_prefixes]) where stem is the final path component and
    dir_prefixes are rel-to-root dirs the file should live under (for precise
    full-path match; assembly falls back to stem match)."""
    if not spec or not alias_map:
        return None
    # try the longest matching alias prefix first (so `@lib` beats `@`)
    for key in sorted((k for k in alias_map if not k.startswith("__")),
                      key=len, reverse=True):
        if spec == key or spec.startswith(key + "/"):
            rest = spec[len(key):].lstrip("/")
            stem = rest.replace("\\", "/").rstrip("/").split("/")[-1] if rest else ""
            for ext in _JS_EXTS:
                if stem.endswith(ext):
                    stem = stem[: -len(ext)]
                    break
            dirs = []
            for base_dir in alias_map[key]:
                sub = "/".join(rest.split("/")[:-1]) if "/" in rest else ""
                dirs.append((base_dir + "/" + sub).strip("/") if sub else base_dir)
            return (stem or None, dirs)
    return None

def parse_javascript(src: bytes, rel: str, parser):
    """Return (defs, refs) for one JS/TS/JSX/TSX file. See module-header comment
    for the node-type map. from_qual = enclosing def qualname (or '<module>');
    resolution to idx is deferred to assemble_graph. Module-path imports carry a
    STEM in to_name (assembly matches it against file-node basenames, last-wins);
    external (bare-pkg) specifiers are dropped in this extractor, never emitted."""
    tree = parser.parse(src)
    root = tree.root_node
    defs, refs = [], []
    # local-name -> {stem, spec, imported, ns} for every import binding in this
    # file. Powers #3 member-call scoping (a `l.helper()` where `l` is a bound
    # import namespace resolves to THAT module's file, not any same-named def).
    js_local_binding = {}

    def enclosing_qual(stack):
        return ".".join(stack) if stack else "<module>"

    def add_def(kind, name, node, stack):
        qual = ".".join(stack + [name])
        defs.append({"kind": kind, "name": name, "file": rel,
                     "line": _line(node), "qualname": qual})
        return qual

    def callee_info(call_node):
        """call_expression -> (callee_name, obj_root, is_member).
          plain-identifier call `helper()`     -> ('helper', None, False)
          member call `l.helper()`             -> ('helper', 'l',  True)
          member call `this.foo()` / `a.b.c()` -> ('foo'/'c', None, True)
          computed/complex callee              -> (None, None, False)
        is_member distinguishes a member call whose obj is NOT a plain binding
        identifier (this./arr./chained) from a true plain-identifier call, so #3
        can DROP the former as noise instead of mis-emitting it as a plain call."""
        fn = call_node.child_by_field_name("function")
        if fn is None:
            return (None, None, False)
        if fn.type == "identifier":
            return (_txt(fn, src), None, False)
        if fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            obj = fn.child_by_field_name("object")
            name = None
            if prop is not None and prop.type in ("property_identifier", "identifier"):
                name = _txt(prop, src)
            obj_root = _txt(obj, src) if (obj is not None and obj.type == "identifier") else None
            return (name, obj_root, True)
        return (None, None, False)

    def module_str(imp_node):
        """import_statement / import_clause-bearing node -> the module specifier
        string (from the trailing `string`->`string_fragment`)."""
        src_node = imp_node.child_by_field_name("source")
        target = src_node if src_node is not None else None
        if target is None:
            for c in imp_node.children:
                if c.type == "string":
                    target = c
                    break
        if target is None:
            return None
        for c in target.children:
            if c.type == "string_fragment":
                return _txt(c, src)
        return _txt(target, src)

    def import_bindings(imp_node):
        """Parse an import_statement's import_clause -> a list of local bindings:
          import def from 'm'          -> [{imported:'default', local:'def'}]
          import * as ns from 'm'      -> [{imported:'*', local:'ns', ns:True}]
          import {a, b as c} from 'm'  -> [{imported:'a',local:'a'},
                                          {imported:'b',local:'c'}]
          import def, {a} from 'm'     -> default + named combined
        Side-effect imports (`import 'm'`) -> []. Powers #3 member-scoping
        (a namespace/default binding maps a local name to the module's file) and
        #1 barrel hops (named bindings tell us WHICH symbols to trace)."""
        out = []
        clause = None
        for c in imp_node.children:
            if c.type == "import_clause":
                clause = c
                break
        if clause is None:
            return out
        for c in clause.children:
            if c.type == "identifier":
                # default import: import def from 'm'
                out.append({"imported": "default", "local": _txt(c, src),
                            "ns": False})
            elif c.type == "namespace_import":
                # import * as ns from 'm'
                nm = None
                for gc in c.children:
                    if gc.type == "identifier":
                        nm = _txt(gc, src)
                if nm:
                    out.append({"imported": "*", "local": nm, "ns": True})
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type != "import_specifier":
                        continue
                    nm = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias")
                    imported = _txt(nm, src) if nm else None
                    if imported is None:
                        continue
                    local = _txt(alias, src) if alias else imported
                    out.append({"imported": imported, "local": local,
                                "ns": False})
        return out

    def walk(node, stack):
        for child in node.children:
            t = child.type
            if t == "function_declaration" or t == "generator_function_declaration":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src) if nm else "<anon>"
                kind = "method" if stack else "function"
                q = add_def(kind, name, child, stack)
                walk(child, stack + [name])
            elif t == "class_declaration":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src) if nm else "<anon>"
                q = add_def("class", name, child, stack)
                # inherits: class_heritage -> extends <identifier|member_expr>
                for gc in child.children:
                    if gc.type == "class_heritage":
                        for h in gc.children:
                            if h.type in ("identifier", "member_expression"):
                                base = _txt(h, src).split(".")[-1]
                                refs.append({"label": "inherits", "from_qual": q,
                                             "to_name": base, "line": _line(child),
                                             "provenance": "inferred"})
                walk(child, stack + [name])
            elif t in ("interface_declaration", "type_alias_declaration"):
                nm = child.child_by_field_name("name")
                if nm is not None:
                    add_def("type", _txt(nm, src), child, stack)
                walk(child, stack)
            elif t == "method_definition":
                nm = child.child_by_field_name("name")
                name = _txt(nm, src) if nm else "<anon>"
                add_def("method", name, child, stack)
                walk(child, stack + [name])
            elif t in ("lexical_declaration", "variable_declaration"):
                # const/let/var: a declarator bound to an arrow/function_expr
                # is a named function def; still descend for nested calls.
                for dcl in child.children:
                    if dcl.type != "variable_declarator":
                        continue
                    nm = dcl.child_by_field_name("name")
                    val = dcl.child_by_field_name("value")
                    if (nm is not None and nm.type == "identifier" and val is not None
                            and val.type in ("arrow_function", "function_expression",
                                             "function")):
                        fname = _txt(nm, src)
                        add_def("method" if stack else "function", fname, dcl, stack)
                        walk(val, stack + [fname])
                    else:
                        walk(dcl, stack)
            elif t == "import_statement":
                mod = module_str(child)
                stem = _js_module_basename(mod) if mod else None
                binds = import_bindings(child)
                # Emit the import ref for ANY path-ish / alias spec (relative OR
                # an @-alias like `@/util`). _js_module_basename returns None for
                # aliases (they look bare), so we can't gate on `stem` here — that
                # would drop aliased imports before the assembly alias-resolver
                # ever runs (#1). Gate on: has a spec AND is not a plain external
                # bare package. An alias/relative spec is kept; a true bare pkg
                # (`react`, `node:fs`, `@scope/pkg`) is dropped.
                is_relative = bool(mod and (mod.startswith("./")
                                            or mod.startswith("../")
                                            or mod.startswith("/")))
                is_aliasish = bool(mod and mod.startswith("@") and "/" in mod)
                keep = bool(stem) or is_relative or is_aliasish
                if keep:
                    refs.append({"label": "imports",
                                 "from_qual": enclosing_qual(stack),
                                 "to_name": stem, "line": _line(child),
                                 "provenance": "inferred", "_js_import": True,
                                 "_js_spec": mod, "_js_bindings": binds})
                    # Record the local->module binding map for member-call
                    # scoping (#3), keyed by local name. Carry the raw spec so
                    # #3 can alias-resolve the bound module's file too.
                    for b in binds:
                        js_local_binding[b["local"]] = {
                            "stem": stem, "spec": mod,
                            "imported": b["imported"], "ns": b.get("ns", False)}
                # true external bare pkg (stem None, not relative/alias) -> DROP.
            elif t == "call_expression":
                fn = child.child_by_field_name("function")
                # CommonJS require('./x') -> imports edge
                if fn is not None and fn.type == "identifier" and _txt(fn, src) == "require":
                    args = child.child_by_field_name("arguments")
                    modstr = None
                    if args is not None:
                        for a in args.children:
                            if a.type == "string":
                                for sc in a.children:
                                    if sc.type == "string_fragment":
                                        modstr = _txt(sc, src)
                                        break
                                break
                    stem = _js_module_basename(modstr) if modstr else None
                    # CommonJS `const l = require('./m')` binds local `l` to the
                    # module namespace -> capture for #3 member-scoping. The
                    # enclosing variable_declarator (if any) names the local.
                    local = None
                    par = child.parent
                    if par is not None and par.type == "variable_declarator":
                        lnm = par.child_by_field_name("name")
                        if lnm is not None and lnm.type == "identifier":
                            local = _txt(lnm, src)
                    if stem:
                        refs.append({"label": "imports",
                                     "from_qual": enclosing_qual(stack),
                                     "to_name": stem, "line": _line(child),
                                     "provenance": "inferred", "_js_import": True,
                                     "_js_spec": modstr,
                                     "_js_bindings": ([{"imported": "*",
                                        "local": local, "ns": True}] if local else [])})
                        if local:
                            js_local_binding[local] = {
                                "stem": stem, "spec": modstr,
                                "imported": "*", "ns": True}
                    walk(child, stack)
                    continue
                callee, obj_root, is_member = callee_info(child)
                if callee and callee.isidentifier():
                    if not is_member:
                        # plain-identifier call `helper()` -> today's behavior.
                        refs.append({"label": "calls",
                                     "from_qual": enclosing_qual(stack),
                                     "to_name": callee, "line": _line(child),
                                     "provenance": "inferred"})
                    elif obj_root is not None and obj_root in js_local_binding:
                        # #3: `ns.helper()` where `ns`/`l` is a bound import
                        # namespace -> scope the call to THAT module's file, so
                        # assembly resolves `helper` to the module's def (not a
                        # global same-name collision). Carry the bound module.
                        b = js_local_binding[obj_root]
                        refs.append({"label": "calls",
                                     "from_qual": enclosing_qual(stack),
                                     "to_name": callee, "line": _line(child),
                                     "provenance": "inferred",
                                     "_js_member_of": {"stem": b.get("stem"),
                                                       "spec": b.get("spec")}})
                    # else: unscoped member call (`this.foo()`, `arr.map()`,
                    # some-local-obj.method()) -> DROP as noise (stdlib / intra-
                    # class already covered by method defs). No edge.
                walk(child, stack)
            elif t == "export_statement":
                # Barrel re-export: `export * from './y'` / `export {z} from './y'`
                # -> a `reexports` edge barrel-file -> y-file (resolved like an
                # import). A local `export {local}` (no `from`) is NOT a re-export
                # (it just marks an existing def exported) -> skip. Powers #1
                # barrel hops: assembly flattens importer->barrel->provider.
                mod = module_str(child)
                if mod:
                    is_relative = (mod.startswith("./") or mod.startswith("../")
                                   or mod.startswith("/"))
                    is_aliasish = mod.startswith("@") and "/" in mod
                    if is_relative or is_aliasish or _js_module_basename(mod):
                        stem = _js_module_basename(mod)
                        refs.append({"label": "reexports",
                                     "from_qual": "<file>",
                                     "to_name": stem, "line": _line(child),
                                     "provenance": "inferred", "_js_reexport": True,
                                     "_js_spec": mod})
                walk(child, stack)
            else:
                walk(child, stack)

    walk(root, [])
    # sentinel ref carrying this file's local-import-binding map for #3 member-
    # call scoping; assembly collects it per-file then skips it (no edge).
    if js_local_binding:
        refs.append({"_js_binding_map": js_local_binding})
    return defs, refs

PARSERS = {"python": parse_python, "bash": parse_bash, "json": parse_json,
           "markdown": parse_markdown,
           "javascript": parse_javascript, "typescript": parse_javascript,
           "tsx": parse_javascript}


# ── graph assembly ─────────────────────────────────────────────
def _node_id(kind, name, rel, line):
    return f"{rel}::{kind}::{name}::L{line}"

def _derive_project(rel, root):
    """Zero-LLM, deterministic project tag from a node's path (NOT LLM-inferred).
    `github/<name>/...` -> `<name>` (each vendored repo is its own project);
    everything else (root-level tooling: procedures/ tools/ scripts/ ...) ->
    the workspace basename. This is the 'which project does this code belong to'
    axis the flat graph buried in the file path. Free: rel is already on hand."""
    parts = rel.replace(os.sep, "/").split("/")
    if len(parts) >= 2 and parts[0] == "github":
        return parts[1]
    return os.path.basename(root.rstrip("/")) or "root"


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
            "project": _derive_project(rel, root),
        })
        idx_of[nid] = idx
        name_to_idx[name] = idx
        return idx

    # pass 1: all def + file nodes (so cross-file refs can resolve)
    file_stem_idx = {}   # basename-stem (no ext) -> file node idx (JS import target)
    file_rel_norm = {}   # normalized rel path (no ext) -> file node idx (alias/full-path match)
    for rel, defs, _refs in parsed:
        base = os.path.basename(rel)
        file_idx[rel] = add_node("file", base, rel, 1)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        # first-wins for a stem so a deterministic target; index/main are common,
        # keep them resolvable but don't clobber a more specific same-stem file.
        file_stem_idx.setdefault(stem, file_idx[rel])
        # normalized rel-no-ext for precise alias/full-path resolution
        rel_norm = rel.replace("\\", "/")
        for ext in _JS_EXTS:
            if rel_norm.endswith(ext):
                rel_norm = rel_norm[: -len(ext)]
                break
        file_rel_norm.setdefault(rel_norm, file_idx[rel])
        for d in defs:
            add_node(d["kind"], d["name"], d["file"], d["line"])

    # #1: build the tsconfig/workspace alias map ONCE per root (fail-open).
    ts_alias_map = _load_ts_alias_map(root)

    def _resolve_js_import(spec, stem, importer_rel):
        """Resolve a JS/TS import to a file-node idx. Order:
        (1) tsconfig alias (@/x -> full-path match, then stem);
        (2) relative spec -> full-path match rel-to-importer (handles ./dir/index
            and same-stem collisions precisely), then index.* under the dir;
        (3) basename-stem fallback (today's behavior).
        Returns idx or None."""
        # (1) alias
        if spec:
            ali = _resolve_ts_alias(spec, ts_alias_map)
            if ali is not None:
                astem, adirs = ali
                for d in adirs:
                    cand = (d + "/" + astem).strip("/") if astem else d
                    if cand in file_rel_norm:
                        return file_rel_norm[cand]
                    idxc = (cand + "/index").strip("/")
                    if idxc in file_rel_norm:
                        return file_rel_norm[idxc]
                if astem and astem in file_stem_idx:
                    return file_stem_idx[astem]
        # (2) relative spec -> resolve rel-to-importer dir
        if spec and (spec.startswith("./") or spec.startswith("../")):
            imp_dir = os.path.dirname(importer_rel.replace("\\", "/"))
            joined = os.path.normpath(os.path.join(imp_dir, spec)).replace("\\", "/")
            joined = joined.lstrip("./") if joined.startswith("./") else joined
            for ext in _JS_EXTS:
                if joined.endswith(ext):
                    joined = joined[: -len(ext)]
                    break
            if joined in file_rel_norm:
                return file_rel_norm[joined]
            idxj = joined + "/index"
            if idxj in file_rel_norm:
                return file_rel_norm[idxj]
        # (3) stem fallback
        if stem and stem in file_stem_idx:
            return file_stem_idx[stem]
        return None

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

    # cross-project dependency rollup: when a resolved edge crosses a project
    # boundary (a.project != b.project), record it as a project-level depends_on.
    # STRUCTURAL rollup of the imports/calls that already cross boundaries (e.g.
    # dinomem-neuron importing from dinomem) -> surfaces 'neuron depends_on base'
    # at the project axis. Zero-LLM; dedup on (from_proj, to_proj); weight = count.
    proj_dep = {}  # (from_proj, to_proj) -> {count, labels:set}

    def _note_cross_project(a_idx, b_idx, label):
        if not (0 <= a_idx < len(nodes) and 0 <= b_idx < len(nodes)):
            return
        fp, tp = nodes[a_idx].get("project"), nodes[b_idx].get("project")
        if not fp or not tp or fp == tp:
            return
        key = (fp, tp)
        d = proj_dep.setdefault(key, {"count": 0, "labels": set()})
        d["count"] += 1
        d["labels"].add(label)

    # pass 2: contains (file -> defs) + resolve refs
    # per-file JS import-binding maps (local-name -> {stem,spec,imported,ns}),
    # collected from the sentinel ref each JS/TS file emits. Powers #3 member-
    # call scoping. Also index file defs by (file-stem, def-name) so a member
    # call on a bound module resolves to THAT module's def, not a global same-name.
    js_binding_by_file = {}
    for rel, defs, refs in parsed:
        for r in refs:
            if "_js_binding_map" in r:
                js_binding_by_file[rel] = r["_js_binding_map"]
    defs_by_stem = {}   # stem -> {name -> def-node-idx}
    for rel, defs, _refs in parsed:
        base = os.path.basename(rel)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        d_map = defs_by_stem.setdefault(stem, {})
        for d in defs:
            nid = _node_id(d["kind"], d["name"], d["file"], d["line"])
            if nid in idx_of:
                d_map.setdefault(d["name"], idx_of[nid])

    # #1 barrel: pre-resolve every file's re-export targets (barrel_file_idx ->
    # [provider_file_idx]) so an import that lands on a barrel can be FLATTENED
    # to the real provider(s). rel_by_idx maps a file node idx back to its rel
    # (needed to alias-resolve the reexport spec rel-to-the-barrel).
    rel_by_idx = {v: k for k, v in file_idx.items()}
    reexport_targets = {}   # barrel file idx -> set(provider file idx)
    for rel, _defs, refs in parsed:
        bi = file_idx.get(rel)
        if bi is None:
            continue
        for r in refs:
            if not r.get("_js_reexport"):
                continue
            tgt = _resolve_js_import(r.get("_js_spec"), r.get("to_name"), rel)
            if tgt is not None and tgt != bi:
                reexport_targets.setdefault(bi, set()).add(tgt)

    def _flatten_barrel(target_idx, _depth=0, _seen=None):
        """If target_idx is a barrel (re-exports other files), return the set of
        real provider file idxs it forwards to (transitively, cycle-guarded,
        depth<=3). If it's not a barrel, return {target_idx} (it IS the provider).
        A barrel that also has its own defs stays a provider too."""
        if _seen is None:
            _seen = set()
        if target_idx in _seen or _depth > 3:
            return set()
        _seen.add(target_idx)
        outs = reexport_targets.get(target_idx)
        if not outs:
            return {target_idx}
        result = set()
        for prov in outs:
            result |= _flatten_barrel(prov, _depth + 1, _seen)
        # keep the barrel itself too if it declares its own symbols
        return result or {target_idx}

    for rel, defs, refs in parsed:
        fi = file_idx[rel]
        binds = js_binding_by_file.get(rel, {})
        for d in defs:
            di = idx_of[_node_id(d["kind"], d["name"], d["file"], d["line"])]
            add_edge(fi, di, "contains", os.path.basename(rel), d["name"], "extracted")
        for r in refs:
            if "_js_binding_map" in r:
                continue  # sentinel, not an edge
            # JSON refs carry a DOTTED key path in from_qual and must resolve the
            # WHOLE path (not the last segment) against name_to_idx; the '<file>'
            # sentinel is the enclosing file node (top-level key parent).
            is_json = r.get("_json_key") or r.get("_json_topkey") or r.get("_json_ref")
            if is_json:
                if r["from_qual"] == "<file>":
                    a = fi
                    subj = os.path.basename(rel)
                else:
                    a = name_to_idx.get(r["from_qual"], fi)
                    subj = r["from_qual"]
            else:
                subj = r["from_qual"].split(".")[-1]
                a = name_to_idx.get(subj, fi)  # enclosing def, else the file
            src_name = subj
            # JS/TS imports resolve to a FILE node by basename-stem (module path
            # './util' -> util.js file node), not to a def/name. Try the stem map
            # first; unresolved (external pkg already dropped in extractor, so an
            # unresolved here is an in-repo file we didn't index) -> DROP.
            if r.get("_js_reexport"):
                continue  # barrel edges are pre-resolved into reexport_targets
            if r.get("_js_import"):
                a = fi  # the importing file is the subject
                src_name = os.path.basename(rel)
                # #1: alias/full-path aware resolution (tsconfig paths, relative
                # rel-to-importer, then basename-stem fallback). r may carry the
                # raw spec (_js_spec) for alias matching; older refs carry stem only.
                tgt = _resolve_js_import(r.get("_js_spec"), r["to_name"], rel)
                if tgt is not None and tgt != fi:
                    add_edge(a, tgt, "imports", src_name, r["to_name"], "extracted")
                    _note_cross_project(a, tgt, "imports")
                    # #1 barrel hop: if the import landed on a barrel (a file that
                    # re-exports others), ALSO add a flattened import edge to the
                    # real provider(s) so 'who provides X' is one hop, not a
                    # dead-end at the barrel. Cycle-guarded, depth<=3.
                    if tgt in reexport_targets:
                        for prov in _flatten_barrel(tgt):
                            if prov != fi and prov != tgt:
                                add_edge(a, prov, "imports", src_name,
                                         os.path.basename(rel_by_idx.get(prov, "")),
                                         "extracted")
                                _note_cross_project(a, prov, "imports")
                continue
            # #3: a member call scoped to a bound import namespace (`u.helper()`
            # where `u` = import * from './util') resolves `helper` to THAT
            # module's def FIRST (via defs_by_stem[util][helper]), not the global
            # last-wins name_to_idx that would false-match a same-named fn in an
            # unrelated file. Only fall back to global if the bound module has no
            # such def (then mark inferred, not extracted).
            if r.get("_js_member_of"):
                mo = r["_js_member_of"]
                mstem = mo.get("stem")
                mtgt = None
                if mstem and mstem in defs_by_stem:
                    mtgt = defs_by_stem[mstem].get(r["to_name"])
                if mtgt is not None:
                    add_edge(a, mtgt, "calls", src_name, r["to_name"], "extracted")
                    _note_cross_project(a, mtgt, "calls")
                # bound module has no such def -> DROP (do not global-fallback: a
                # module-scoped call to a name the module doesn't define is more
                # likely a re-export/type than a real global collision hit).
                continue
            tgt = name_to_idx.get(r["to_name"])
            if tgt is not None:
                add_edge(a, tgt, r["label"], src_name, r["to_name"], r.get("provenance", "extracted") if is_json else "extracted")
                _note_cross_project(a, tgt, r["label"])
            elif r.get("_json_ref"):
                # a config path-value that points OUTSIDE the graph (external file
                # or a not-indexed path) -> DROP; an inferred edge to a phantom
                # file node would be noise, same rationale as bash externals.
                continue
            elif r.get("_bash_call") or r.get("_bash_source"):
                # bash: an unresolved command name is almost always an external
                # binary (grep/ls/…) or an out-of-graph script, NOT a real in-repo
                # symbol. Emitting an inferred edge for it would flood the graph
                # with noise, so DROP it (unlike python, where an unresolved call
                # still names a meaningful symbol worth an inferred edge).
                continue
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

    # materialize project-level depends_on edges (sorted, dedup'd, weighted)
    project_edges = [
        {"from": fp, "to": tp, "relation": "depends_on",
         "weight": d["count"], "via": sorted(d["labels"])}
        for (fp, tp), d in sorted(proj_dep.items(), key=lambda kv: -kv[1]["count"])
    ]
    projects = sorted({n.get("project") for n in nodes if n.get("project")})

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
        "projects": projects,
        "project_edges": project_edges,
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
