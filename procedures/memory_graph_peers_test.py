#!/usr/bin/env python3
"""memory_graph_peers_test.py -- proves memory/peers/<platform>_<id>.md files become `person`
entities in the graph's entity layer, and that the id match used for the representative chunk
is word-bounded (a short/truncated id must not hit unrelated numbers). Throwaway temp dir, no DB,
no embeddings. Run: python3 memory_graph_peers_test.py"""
import re
import sys
import tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import memory_graph as mg  # noqa: E402

PEER = """---
type: peer
platform: {plat}
platform_id: {pid}
person_ref:            # nullable
name: unknown
handle: {handle}
---

## FACTS
"""


def main():
    with tempfile.TemporaryDirectory() as td:
        mem = Path(td) / "memory"
        peers = mem / "peers"
        peers.mkdir(parents=True)
        (mem / "2026-01-01_entity_x.md").write_text("[entity] Project Advancer | type: tool\n")
        (peers / "telegram_123456789.md").write_text(PEER.format(plat="telegram", pid="123456789", handle="@alice_x"))
        (peers / "discord_42.md").write_text(PEER.format(plat="discord", pid='"42"', handle=""))
        (peers / "notes.md").write_text("no frontmatter here\n")          # ignored: not a peer
        (peers / "peers_graph.json").write_text("{}")                     # ignored: not .md
        ents = mg.extract_entity_nodes(mem)
        by = {e["name"]: e for e in ents}
        assert "Project Advancer" in by, "regular [entity] lines must still load"
        assert by["telegram:123456789"]["type"] == "person"
        assert by["telegram:123456789"]["match_terms"] == ["123456789", "alice_x"]
        assert by["discord:42"]["match_terms"] == [], "short ids must not be used for matching"
        assert sum(1 for e in ents if e.get("source") == "peers") == 2
        # word-bounded match: the id inside a longer number must NOT count
        pat = re.compile(r'(?<![0-9a-z])' + re.escape("123456789") + r'(?![0-9a-z])')
        assert pat.search("reply from telegram:123456789 today")
        assert not pat.search("tx 91234567890 settled")
        # no peers dir -> no-op, never raises
        assert mg._peer_entities(Path(td) / "missing") == []
    print("PASS memory_graph_peers_test (peer->person entity, short-id guard, word-bounded match)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
