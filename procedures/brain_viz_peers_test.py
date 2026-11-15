#!/usr/bin/env python3
"""brain_viz_peers_test.py -- proves pick_subgraph() pulls every peer file
(memory/peers/<platform>_<id>.md) into the rendered subset even when hub-first
growth runs out of budget first, takes ONE chunk per person, respects the hop
cap, and never treats a dated user_fact file as a person.
Synthetic graph, no workspace, no I/O. Run: python3 brain_viz_peers_test.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main():
    try:
        import networkx as nx
    except ImportError:
        print("SKIP brain_viz_peers_test (networkx not installed -- optional extra)")
        return 0
    import brain_viz as bv

    G = nx.Graph()
    byidx = {}
    # hub core: 31-node clique -> every core node out-degrees any chain node, so
    # hub-first growth fills the 31 budget with the core and never walks a chain
    # (a star core fails this: its leaves have degree 1 < chain degree 2).
    for i in range(31):
        byidx[i] = {"file": f"2026-01-01_decision_n{i}.md"}
        for j in range(i + 1, 31):
            G.add_edge(i, j, w=1.0)
    nid = 100

    def chain(start, length, last_file, mid="2026-01-01_log_x.md"):
        nonlocal nid
        prev = start
        for k in range(length):
            nid += 1
            G.add_edge(prev, nid, w=1.0)
            byidx[nid] = {"file": last_file if k == length - 1 else mid}
            prev = nid
        return prev

    near = chain(5, 3, "telegram_111222333.md")          # person A, close
    near2 = chain(near, 1, "telegram_111222333.md")      # 2nd chunk of person A (must NOT be added)
    far = chain(6, 6, "discord_987654321.md")            # person B, 7-node path (within cap)
    too_far = chain(7, 12, "whatsapp_555000111.md")      # person C, beyond PEER_HOPS
    fact = chain(8, 2, "2026-10-29_user_fact_something.md")  # dated user_fact, NOT a person

    keep = set(bv.pick_subgraph(nx, G, 31, byidx))
    assert near in keep, "close peer not pulled in"
    assert near2 not in keep, "more than one chunk per person"
    assert far in keep, "peer within PEER_HOPS not pulled in"
    assert too_far not in keep, "peer beyond PEER_HOPS pulled in"
    assert fact not in keep, "dated user_fact file treated as a person"
    # path to a pulled peer is pulled too, so the brain stays one connected piece
    assert nx.is_connected(G.subgraph(keep)), "pulled peer left disconnected"
    # backward compat: no byidx -> exactly the old hub-first behaviour
    assert len(bv.pick_subgraph(nx, G, 31)) == 31
    for f, want in [("telegram_1.md", True), ("peers/signal_abc.md", True),
                    ("2026-10-29_user_fact_x.md", False), ("_pin_telegram-rules.md", False)]:
        assert bool(bv.PEER_FILE.match(f)) is want, f
    print("PASS brain_viz_peers_test (peer pull-in, one-per-person, hop cap, user_fact excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
