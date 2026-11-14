#!/usr/bin/env python3
"""brain_viz_test.py -- proves brain_viz renders a synthetic graph and that
--redact leaves NO node text in the file. Throwaway temp dir. Run: python3 brain_viz_test.py"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET = "zebrasecretword"


def main():
    try:
        import networkx  # noqa: F401
    except ImportError:
        print("SKIP brain_viz_test: networkx not installed")
        return 0
    nodes = [{"idx": i, "file": f"2026-01-01_decision_n{i}.md", "title": f"t{i}",
              "snippet": f"{SECRET} node {i} </script> <b>x</b>"} for i in range(40)]
    edges = [{"a": i, "b": (i + 1) % 40, "sim": 0.9} for i in range(40)] + \
            [{"a": i, "b": (i * 7) % 40, "sim": 0.8} for i in range(40)]
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "g.json"
        src.write_text(json.dumps({"nodes": nodes, "edges": edges, "node_count": 40, "edge_count": 80}))
        for redact in (False, True):
            out = Path(td) / f"b{int(redact)}.html"
            cmd = [sys.executable, str(HERE / "brain_viz.py"), "--src", str(src), "--out", str(out)]
            r = subprocess.run(cmd + (["--redact"] if redact else []), capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            html = out.read_text()
            assert html.count("</script>") == 1, "node text closed the script tag"
            assert (SECRET in html) is (not redact), f"redact={redact} text presence wrong"
    print("PASS brain_viz_test (render + redact + script-escape)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
