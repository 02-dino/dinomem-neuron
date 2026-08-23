#!/usr/bin/env bash
# code_graph_bash_test.sh — deterministic TEST for the bash extractor in
# procedures/code_graph.py (test-don't-assume). Generic/portable: no analyst-only
# paths; resolves code_graph.py relative to this repo, falls back to the live
# workspace copy so it runs from either the base repo OR the analyst workspace.
#
# Runnable standalone (any cwd):  bash test/code_graph_bash_test.sh
# Exits 0 + prints "RESULT: PASS" iff every assertion holds; non-zero on any fail.
#
# Fixture (tiny, self-contained in a tmpdir):
#   fileA.sh  ->  sources fileB.sh, then calls foo (a fn defined in fileB)
#   fileB.sh  ->  defines foo()
#   fileA also calls `grep` (an UNKNOWN external) -> must produce NO bogus edge.
# Asserts, against the built graph.json:
#   (1) source/import edge  fileA.sh --imports--> fileB.sh   (resolved, b>=0)
#   (2) calls edge          <fileA> --calls--> foo           (file:line present)
#   (3) both def nodes exist: fileA.sh file node + foo function node w/ line
#   (4) NO edge whose object is the unknown external `grep`  (noise dropped)
# A deliberately-broken extractor (e.g. not reading command_name) fails (1)/(2).

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# locate code_graph.py: repo (../procedures) first, then env/install workspace,
# then any ~/.openclaw workspace-* dir (install-agnostic; no analyst-only path).
CG=""
for cand in \
  "$TEST_DIR/../procedures/code_graph.py" \
  "${DINOMEM_WORKSPACE:-}/procedures/code_graph.py" \
  "$HOME"/.openclaw/workspace-*/procedures/code_graph.py; do
  [ -f "$cand" ] && { CG="$(cd "$(dirname "$cand")/.." && pwd)/procedures/code_graph.py"; break; }
done

FAILED=0; PASSED=0
_pass() { printf 'PASS  %s\n' "$1"; PASSED=$((PASSED+1)); }
_fail() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED+1)); }

if [ -z "$CG" ]; then
  printf 'SKIP  code_graph.py not found (base repo or live workspace)\n'
  printf 'RESULT: SKIP\n'; exit 0
fi
CG_ROOT="$(cd "$(dirname "$CG")/.." && pwd)"

# tree-sitter bash grammar is an optional dep; SKIP (don't FAIL) if absent.
if ! python3 -c 'from tree_sitter_language_pack import get_parser; get_parser("bash")' 2>/dev/null; then
  printf 'SKIP  tree-sitter bash grammar unavailable (optional dep)\n'
  printf 'RESULT: SKIP\n'; exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/src"
cat > "$WORK/src/fileB.sh" <<'EOF'
#!/usr/bin/env bash
foo() {
  echo "in foo"
}
EOF
cat > "$WORK/src/fileA.sh" <<'EOF'
#!/usr/bin/env bash
DIR="$(dirname "$0")"
source "$DIR/fileB.sh"
main() {
  foo
  grep -q x /dev/null
}
main
EOF

# run the extractor directly (isolated; no dependence on the live graph build).
OUT="$WORK/out.json"
CG_ROOT="$CG_ROOT" SRC="$WORK/src" OUT="$OUT" python3 - "$CG" <<'PYEOF'
import sys, os, json, importlib.util
cg_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("code_graph", cg_path)
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.dirname(os.path.dirname(cg_path)))
spec.loader.exec_module(m)
src = os.environ["SRC"]
# build a graph over just the fixture dir using the module's own pipeline.
exts = {".sh": "bash", ".bash": "bash"}
files = m.walk_code_files(src, exts)
parsed = []
from tree_sitter_language_pack import get_parser
parser = get_parser("bash")
for ap, rel, lang in files:
    with open(ap, "rb") as fh:
        data = fh.read()
    defs, refs = m.parse_bash(data, rel, parser)
    parsed.append((rel, defs, refs))
payload = m.assemble_graph(parsed, src, built_at_sha="TEST")
with open(os.environ["OUT"], "w") as fh:
    json.dump(payload, fh)
PYEOF

if [ ! -s "$OUT" ]; then
  _fail "extractor produced no graph output"
  printf 'RESULT: FAIL\n'; exit 1
fi

# assertions via python (jq may be absent; python always present here).
ASSERT_OUT="$WORK/assert.txt"
OUT="$OUT" python3 - <<'PYEOF' > "$ASSERT_OUT" 2>&1
import json, os
g = json.load(open(os.environ["OUT"]))
nodes = g["nodes"]; edges = g["relation_edges"]
def node(i): return nodes[i]

# (3) def nodes
foo_defs = [n for n in nodes if n["name"] == "foo" and n["kind"] == "function"]
fileA = [n for n in nodes if n["file"].endswith("fileA.sh") and n["kind"] == "file"]
fileB = [n for n in nodes if n["file"].endswith("fileB.sh") and n["kind"] == "file"]
print("DEF_FOO", 1 if (foo_defs and foo_defs[0].get("line")) else 0)
print("DEF_FILEA", 1 if fileA else 0)

# (1) import edge fileA -> fileB (resolved)
imp = [e for e in edges if e["label"] == "imports"
       and e["b"] >= 0 and node(e["a"])["file"].endswith("fileA.sh")
       and node(e["b"])["file"].endswith("fileB.sh")]
print("IMPORT_EDGE", 1 if imp else 0)

# (2) calls edge -> foo, with caller file:line
foo_idx = {n["idx"] for n in foo_defs}
calls = [e for e in edges if e["label"] == "calls" and e["b"] in foo_idx]
ok_call = 0
for e in calls:
    fr = node(e["a"])
    if fr.get("line") and fr.get("file"):
        ok_call = 1; break
print("CALLS_EDGE", ok_call)

# (4) NO edge referencing the unknown external `grep`
grep_edges = [e for e in edges if e.get("object") == "grep"]
print("NO_GREP", 1 if not grep_edges else 0)
PYEOF

get() { grep -E "^$1 " "$ASSERT_OUT" | awk '{print $2}'; }

[ "$(get DEF_FILEA)"   = "1" ] && _pass "fileA.sh file node exists" || _fail "fileA.sh file node missing"
[ "$(get DEF_FOO)"     = "1" ] && _pass "foo function node exists w/ line" || _fail "foo def node missing/no line"
[ "$(get IMPORT_EDGE)" = "1" ] && _pass "fileA --imports--> fileB (resolved)" || _fail "source/import edge missing"
[ "$(get CALLS_EDGE)"  = "1" ] && _pass "caller --calls--> foo (file:line)" || _fail "calls->foo edge missing"
[ "$(get NO_GREP)"     = "1" ] && _pass "unknown external grep -> no bogus edge" || _fail "bogus edge emitted for grep"

printf '\n%s\n' "----------------------------------------"
printf 'passed=%s failed=%s\n' "$PASSED" "$FAILED"
if [ "$FAILED" -eq 0 ]; then printf 'RESULT: PASS\n'; exit 0; else printf 'RESULT: FAIL\n'; exit 1; fi
