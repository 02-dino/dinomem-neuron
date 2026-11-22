#!/usr/bin/env bash
# code_graph_json_test.sh — deterministic TEST for the json/config extractor in
# procedures/code_graph.py (test-don't-assume). Generic/portable: resolves
# code_graph.py from this repo OR the live workspace; SKIPs if the json grammar
# is absent (optional dep).
#
# Runnable standalone (any cwd):  bash test/code_graph_json_test.sh
# Exits 0 + prints "RESULT: PASS" iff every assertion holds.
#
# Fixture (self-contained tmpdir):
#   src/app.json      -> nested keys a.b.c + a "script":"scripts/tool.sh" value
#   src/scripts/tool.sh -> a real file the path-value must resolve to
#   data/registered.json -> lives under a DIR added to DINOMEM_CODE_GRAPH_EXEMPT
#                           (simulating a data_query-registered dataset) -> must
#                           yield ZERO code-graph nodes.
# Asserts, against the built graph.json:
#   (1) nested config_key nodes exist with DOTTED paths (a, a.b, a.b.c)
#   (2) a `contains` nesting edge  a.b --contains--> a.b.c
#   (3) a `references` edge  <a key> --references--> tool.sh  (path-value resolved)
#   (4) the exempted data/registered.json produces 0 nodes (exemption holds)

set -uo pipefail
TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CG=""
for cand in \
  "$TEST_DIR/../procedures/code_graph.py" \
  "${DINOMEM_WORKSPACE:-}/procedures/code_graph.py" \
  "$HOME"/.openclaw/workspace-*/procedures/code_graph.py; do
  [ -f "$cand" ] && { CG="$(cd "$(dirname "$cand")/.." && pwd)/procedures/code_graph.py"; break; }
done
[ -z "$CG" ] && { printf 'SKIP  code_graph.py not found\nRESULT: SKIP\n'; exit 0; }

if ! python3 -c 'from tree_sitter_language_pack import get_parser; get_parser("json")' 2>/dev/null; then
  printf 'SKIP  tree-sitter json grammar unavailable (optional dep)\nRESULT: SKIP\n'; exit 0
fi

FAILED=0; PASSED=0
_pass() { printf 'PASS  %s\n' "$1"; PASSED=$((PASSED+1)); }
_fail() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED+1)); }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src/scripts" "$WORK/data"
cat > "$WORK/src/scripts/tool.sh" <<'EOF'
#!/usr/bin/env bash
run() { echo x; }
EOF
cat > "$WORK/src/app.json" <<'EOF'
{
  "a": {
    "b": {
      "c": 1,
      "script": "scripts/tool.sh"
    }
  },
  "top": true
}
EOF
cat > "$WORK/data/registered.json" <<'EOF'
{ "dataset": { "rows": 3, "path": "src/scripts/tool.sh" } }
EOF

OUT="$WORK/out.json"
# exempt the data/ dir via env (user-style data_query dir); the src tree indexes.
SRC="$WORK/src" DATA="$WORK/data" OUT="$OUT" DINOMEM_CODE_GRAPH_EXEMPT="$WORK/data" \
python3 - "$CG" "$WORK" <<'PYEOF'
import sys, os, json, importlib.util
cg_path = sys.argv[1]; root = sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.dirname(cg_path)))
spec = importlib.util.spec_from_file_location("code_graph", cg_path)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
exts = {".json": "json", ".sh": "bash", ".bash": "bash"}
from tree_sitter_language_pack import get_parser
# walk the WHOLE root so the exemption (data/) is exercised by the real walk.
files = m.walk_code_files(root, exts)
parsed = []
for ap, rel, lang in files:
    with open(ap, "rb") as fh: data = fh.read()
    parser = get_parser(lang)
    if lang == "json":
        defs, refs = m.parse_json(data, rel, parser)
    elif lang == "bash":
        defs, refs = m.parse_bash(data, rel, parser)
    else:
        defs, refs = [], []
    parsed.append((rel, defs, refs))
payload = m.assemble_graph(parsed, root, built_at_sha="TEST")
json.dump(payload, open(os.environ["OUT"], "w"))
PYEOF

[ -s "$OUT" ] || { _fail "no graph output"; printf 'RESULT: FAIL\n'; exit 1; }

ASSERT="$WORK/assert.txt"
OUT="$OUT" python3 - <<'PYEOF' > "$ASSERT" 2>&1
import json, os
g = json.load(open(os.environ["OUT"]))
nodes = g["nodes"]; edges = g["relation_edges"]
name = {n["idx"]: n for n in nodes}
ck = [n for n in nodes if n["kind"] == "config_key"]
names = {n["name"] for n in ck}
print("NESTED", 1 if {"a", "a.b", "a.b.c"} <= names else 0)

# contains a.b -> a.b.c
ab = next((n for n in ck if n["name"] == "a.b"), None)
abc = next((n for n in ck if n["name"] == "a.b.c"), None)
ok_contains = 0
if ab and abc:
    ok_contains = 1 if any(e["label"] == "contains" and e["a"] == ab["idx"]
                           and e["b"] == abc["idx"] for e in edges) else 0
print("CONTAINS", ok_contains)

# references -> tool.sh (path value resolved to the file node)
tool = next((n for n in nodes if n["file"].endswith("scripts/tool.sh")
             and n["kind"] == "file"), None)
ok_ref = 0
if tool:
    ok_ref = 1 if any(e["label"] == "references" and e["b"] == tool["idx"]
                      for e in edges) else 0
print("REFERENCES", ok_ref)

# exemption: NO node whose file is under data/
exempt = [n for n in nodes if n["file"].replace("\\", "/").startswith("data/")
          or "/data/registered.json" in n["file"].replace("\\", "/")]
print("EXEMPT_ZERO", 1 if not exempt else 0)
PYEOF

get() { grep -E "^$1 " "$ASSERT" | awk '{print $2}'; }
[ "$(get NESTED)"     = "1" ] && _pass "nested dotted config_key nodes (a,a.b,a.b.c)" || _fail "nested key nodes missing"
[ "$(get CONTAINS)"   = "1" ] && _pass "a.b --contains--> a.b.c nesting edge" || _fail "nesting edge missing"
[ "$(get REFERENCES)" = "1" ] && _pass "key --references--> tool.sh (path-value resolved)" || _fail "references edge missing"
[ "$(get EXEMPT_ZERO)" = "1" ] && _pass "exempted data/ json -> 0 nodes" || _fail "exemption breached (data/ produced nodes)"

printf '\n----------------------------------------\n'
printf 'passed=%s failed=%s\n' "$PASSED" "$FAILED"
if [ "$FAILED" -eq 0 ]; then printf 'RESULT: PASS\n'; exit 0; else printf 'RESULT: FAIL\n'; exit 1; fi
