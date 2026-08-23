#!/usr/bin/env bash
# code_graph_markdown_test.sh — deterministic TEST for the markdown extractor in
# procedures/code_graph.py (test-don't-assume). CODE-REPO-ONLY markdown: doc->code
# structural edges + section nodes. Generic/portable; SKIPs if grammar absent.
#
# Runnable standalone (any cwd):  bash test/code_graph_markdown_test.sh
# Exits 0 + prints "RESULT: PASS" iff every assertion holds.
#
# Fixture (self-contained tmpdir):
#   src/SKILL.md   -> ## headings + a [link](scripts/tool.sh) + a fenced code
#                     block naming scripts/other.py  (both real files below)
#   src/scripts/tool.sh, src/scripts/other.py -> real targets the doc edges hit
#   exempt/_note_demo.md -> under DINOMEM_CODE_GRAPH_EXEMPT (memory-style dir);
#                           must yield ZERO nodes (the code-repo-only invariant).
# Asserts, against the built graph.json:
#   (1) section nodes exist for the headings
#   (2) a `documents` edge  SKILL.md --documents--> tool.sh  (link target resolved)
#   (3) a `documents` edge  SKILL.md --documents--> other.py (code-fence path)
#   (4) the exempted _note_demo.md produces 0 nodes (exemption + code-repo-only)

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

if ! python3 -c 'from tree_sitter_language_pack import get_parser; get_parser("markdown")' 2>/dev/null; then
  printf 'SKIP  tree-sitter markdown grammar unavailable (optional dep)\nRESULT: SKIP\n'; exit 0
fi

FAILED=0; PASSED=0
_pass() { printf 'PASS  %s\n' "$1"; PASSED=$((PASSED+1)); }
_fail() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED+1)); }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/src/scripts" "$WORK/exempt"
cat > "$WORK/src/scripts/tool.sh" <<'EOF'
#!/usr/bin/env bash
run() { echo x; }
EOF
cat > "$WORK/src/scripts/other.py" <<'EOF'
def helper(): return 1
EOF
cat > "$WORK/src/SKILL.md" <<'EOF'
# Demo Skill

Route intent to [the tool](scripts/tool.sh) when triggered.

## Usage

Run this script:

```
scripts/other.py
```
EOF
cat > "$WORK/exempt/_note_demo.md" <<'EOF'
# A memory note
Links to [x](scripts/tool.sh) — but this dir is exempt, so NO nodes.
EOF

OUT="$WORK/out.json"
SRC="$WORK/src" OUT="$OUT" DINOMEM_CODE_GRAPH_EXEMPT="$WORK/exempt" \
python3 - "$CG" "$WORK" <<'PYEOF'
import sys, os, json, importlib.util
cg_path = sys.argv[1]; root = sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.dirname(cg_path)))
spec = importlib.util.spec_from_file_location("code_graph", cg_path)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
exts = {".md": "markdown", ".sh": "bash", ".py": "python"}
from tree_sitter_language_pack import get_parser
files = m.walk_code_files(root, exts)
parsed = []
for ap, rel, lang in files:
    with open(ap, "rb") as fh: data = fh.read()
    parser = get_parser("markdown" if lang == "markdown" else lang)
    if lang == "markdown":
        defs, refs = m.parse_markdown(data, rel, parser)
    elif lang == "bash":
        defs, refs = m.parse_bash(data, rel, parser)
    elif lang == "python":
        defs, refs = m.parse_python(data, rel, parser)
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
sec = [n for n in nodes if n["kind"] == "section"]
print("SECTIONS", 1 if sec else 0)

skill = next((n for n in nodes if n["file"].endswith("SKILL.md") and n["kind"] == "file"), None)
tool = next((n for n in nodes if n["file"].endswith("scripts/tool.sh") and n["kind"] == "file"), None)
other = next((n for n in nodes if n["file"].endswith("scripts/other.py") and n["kind"] == "file"), None)

def has_doc(a, b):
    if not a or not b: return 0
    return 1 if any(e["label"] == "documents" and e["a"] == a["idx"] and e["b"] == b["idx"] for e in edges) else 0
print("DOC_LINK", has_doc(skill, tool))
print("DOC_FENCE", has_doc(skill, other))

exempt = [n for n in nodes if "_note_demo.md" in n["file"] or n["file"].replace("\\","/").split("/")[0] == "exempt"]
print("EXEMPT_ZERO", 1 if not exempt else 0)
PYEOF

get() { grep -E "^$1 " "$ASSERT" | awk '{print $2}'; }
[ "$(get SECTIONS)"   = "1" ] && _pass "heading section nodes exist" || _fail "section nodes missing"
[ "$(get DOC_LINK)"   = "1" ] && _pass "SKILL.md --documents--> tool.sh (link target)" || _fail "doc->code link edge missing"
[ "$(get DOC_FENCE)"  = "1" ] && _pass "SKILL.md --documents--> other.py (code-fence path)" || _fail "doc->code fence edge missing"
[ "$(get EXEMPT_ZERO)" = "1" ] && _pass "exempted _note_ md -> 0 nodes (code-repo-only)" || _fail "exemption breached (exempt md produced nodes)"

printf '\n----------------------------------------\n'
printf 'passed=%s failed=%s\n' "$PASSED" "$FAILED"
if [ "$FAILED" -eq 0 ]; then printf 'RESULT: PASS\n'; exit 0; else printf 'RESULT: FAIL\n'; exit 1; fi
