#!/usr/bin/env python3
"""
extract_user_test.py — T3 gate test fixture (ships WITH the deriver).

Proves the quality-first surprisal gate + derive-apply pipeline works, including
the silent-failure mode: ONE buried high-signal line among noise must be caught.

Runs OFFLINE — call_llm is monkeypatched with a canned derive response, so there
is zero LLM cost and the test is deterministic. Tests, in order:
  T3.1  Stage-0 fires on a buried high-signal line among noise (recall-max).
  T3.2  Stage-0 does NOT fire on provably-empty / pure-machinery content.
  T3.3  parse_derive_json tolerates fenced / bare / trailing-text JSON.
  T3.4  apply_derive writes facts+beat, supersede-in-place logs provenance.
  T3.5  full process_archive w/ mocked LLM extracts the buried signal into a rep.

Exit 0 = all pass, 1 = any fail. No network, no real archives required.
"""

import sys
import os
import json
import tempfile
import importlib.util
from pathlib import Path

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        print(f"PASS  {name}")
        _PASS += 1
    else:
        print(f"FAIL  {name}")
        _FAIL += 1


# ─── Load extract_user with all paths redirected to an isolated scratch dir ───
def load_module(scratch):
    proc = Path(__file__).resolve().parent
    (scratch / "memory" / "peers").mkdir(parents=True, exist_ok=True)
    (scratch / "logs").mkdir(parents=True, exist_ok=True)
    (scratch / "templates").mkdir(parents=True, exist_ok=True)
    tmpl = proc.parent / "templates" / "peer_rep.md.tmpl"
    if tmpl.exists():
        import shutil
        shutil.copy(tmpl, scratch / "templates" / "peer_rep.md.tmpl")
    spec = importlib.util.spec_from_file_location("extract_user_t", proc / "extract_user.py")
    eu = importlib.util.module_from_spec(spec)
    sys.modules["extract_user_t"] = eu
    spec.loader.exec_module(eu)
    eu.MEMORY_DIR = scratch / "memory"
    eu.PEERS_DIR = scratch / "memory" / "peers"
    eu.LOG_FILE = scratch / "logs" / "extract_user.log"
    eu.PROCESSED_LOG = scratch / "logs" / ".proc.json"
    eu.STATUS_FILE = scratch / "logs" / ".status.json"
    eu.TEMPLATE_FILE = scratch / "templates" / "peer_rep.md.tmpl"
    return eu


# ─── Canned transcript: ONE buried high-signal line among NOISE ────────────
# The silent-failure mode a quality-first gate must catch: a single important
# identity fact drowned in routine market chatter.
NOISE_TRANSCRIPT = """[USER]: whats btc doing today
[ASSISTANT]: BTC is around 64k, funding neutral.
[USER]: ok and eth
[ASSISTANT]: ETH ~3400, OI building slightly.
[USER]: cool. btw just so you know i only trade money i can afford to lose, keep that in mind for any sizing you suggest
[ASSISTANT]: Understood, I'll keep position sizing conservative.
[USER]: whats sol at
[ASSISTANT]: SOL ~150.
[USER]: thanks
"""

# Pure-machinery / empty content that must NOT fire Stage-0.
EMPTY_TRANSCRIPT = """[ASSISTANT]: Here is the data you requested.
[ASSISTANT]: Done.
"""

# Canned derive response the mocked LLM returns for the buried-signal transcript.
CANNED_DERIVE = json.dumps({
    "facts": [{"text": "Only trades money they can afford to lose", "confidence": 0.95}],
    "beat": [{"text": "Low risk tolerance; wants conservative position sizing", "confidence": 0.8}],
    "ledger": [],
    "supersedes": [],
})


def run():
    with tempfile.TemporaryDirectory(prefix="eu_t3_") as td:
        scratch = Path(td)
        eu = load_module(scratch)

        # T3.1 Stage-0 fires on buried signal among noise
        check("T3.1 stage0 fires on buried high-signal line",
              eu.stage0_has_signal(NOISE_TRANSCRIPT) is True)

        # T3.2 Stage-0 does NOT fire on empty/machinery content
        check("T3.2 stage0 skips provably-empty content",
              eu.stage0_has_signal(EMPTY_TRANSCRIPT) is False)

        # T3.3 parse_derive_json tolerates fenced / bare / trailing-text JSON
        fenced = "```json\n" + CANNED_DERIVE + "\n```"
        trailing = CANNED_DERIVE + "\n\nThat's my analysis."
        check("T3.3a parse bare JSON", bool(eu.parse_derive_json(CANNED_DERIVE).get("facts")))
        check("T3.3b parse fenced JSON", bool(eu.parse_derive_json(fenced).get("facts")))
        check("T3.3c parse JSON w/ trailing prose", bool(eu.parse_derive_json(trailing).get("facts")))
        check("T3.3d parse garbage -> {}", eu.parse_derive_json("not json at all") == {})

        # T3.4 apply_derive writes facts+beat; supersede logs provenance
        plat, pid = "telegram", "555000111"
        eu.ensure_stub(plat, pid, name="tester")
        derived = eu.parse_derive_json(CANNED_DERIVE)
        eu.apply_derive(plat, pid, derived)
        rep = eu.peer_path(plat, pid).read_text()
        check("T3.4a fact written to rep", "afford to lose" in rep)
        check("T3.4b beat written to rep", "risk tolerance" in rep)
        check("T3.4c confidence tag present", "conf: 0.95" in rep)

        # supersede-in-place logs provenance
        sup = {"facts": [], "beat": [], "ledger": [],
               "supersedes": [{"old": "Only trades money they can afford to lose",
                               "new": "Now trades a dedicated risk budget only"}]}
        eu.apply_derive(plat, pid, sup)
        rep2 = eu.peer_path(plat, pid).read_text()
        check("T3.4d supersede replaces old fact", "dedicated risk budget" in rep2)
        check("T3.4e provenance line logged", "[SUPERSEDED" in rep2)

        # T3.5 full process_archive w/ mocked LLM extracts the buried signal
        # monkeypatch call_llm to return the canned derive, force derive gate open
        eu.call_llm = lambda prompt, max_tokens=None, reasoning=False: (True, CANNED_DERIVE)
        eu.DERIVE_MIN_INTERACTIONS = 1
        eu.read_archive_meta_and_text = lambda fn: ("agent:_:telegram:direct:777888999", NOISE_TRANSCRIPT)
        eu.process_archive("canned.archived.reset.jsonl")
        rep3_path = eu.peer_path("telegram", "777888999")
        ok = rep3_path.exists() and "afford to lose" in rep3_path.read_text()
        check("T3.5 process_archive extracts buried signal end-to-end", ok)

        # ─── NEURON tests (relations tier, peers_graph, compactor) ──────────
        # Point the graph at the scratch peers dir so we don't touch real state.
        eu.PEERS_GRAPH_FILE = scratch / "memory" / "peers" / "peers_graph.json"

        # T6 RELATIONS tier: derive with relations -> rep RELATIONS + peers_graph
        rel_derive = {
            "facts": [], "beat": [], "ledger": [], "supersedes": [],
            "relations": [
                {"verb": "trades", "object": "ETH", "confidence": 0.9},
                {"verb": "prefers", "object": "raw orderbook data", "confidence": 0.7},
            ],
        }
        rp, ri = "telegram", "222333"
        eu.ensure_stub(rp, ri, name="relguy")
        eu.apply_derive(rp, ri, rel_derive)
        relrep = eu.peer_path(rp, ri).read_text()
        check("T6.1 relation line written to rep RELATIONS",
              "peer:telegram:222333 → trades → ETH" in relrep)
        check("T6.2 peers_graph.json created", eu.PEERS_GRAPH_FILE.exists())
        g = json.loads(eu.PEERS_GRAPH_FILE.read_text())
        subs = {e["subject"] for e in g["edges"]}
        check("T6.3 edge subject is namespaced peer:", "peer:telegram:222333" in subs)
        objs = {e["object"] for e in g["edges"]}
        check("T6.4 both relation objects landed", {"ETH", "raw orderbook data"} <= objs)

        # T7 peers_graph dedup + confidence-max on repeat derive
        eu.apply_derive(rp, ri, {"facts": [], "beat": [], "ledger": [], "supersedes": [],
                                 "relations": [{"verb": "trades", "object": "ETH", "confidence": 0.95}]})
        g2 = json.loads(eu.PEERS_GRAPH_FILE.read_text())
        eth_edges = [e for e in g2["edges"]
                     if e["subject"] == "peer:telegram:222333" and e["verb"] == "trades" and e["object"] == "ETH"]
        check("T7.1 dedup: single trades->ETH edge (not duplicated)", len(eth_edges) == 1)
        check("T7.2 confidence-max applied on repeat", eth_edges and eth_edges[0]["confidence"] == 0.95)
        check("T7.3 count incremented on repeat", eth_edges and eth_edges[0].get("count", 0) >= 2)

        # T7.4 PRIVACY: a second peer's edges never leak into the first's subject set
        eu.ensure_stub("signal", "999", name="other")
        eu.apply_derive("signal", "999", {"facts": [], "beat": [], "ledger": [], "supersedes": [],
                                          "relations": [{"verb": "trades", "object": "BTC", "confidence": 0.8}]})
        g3 = json.loads(eu.PEERS_GRAPH_FILE.read_text())
        p1_objs = {e["object"] for e in g3["edges"] if e["subject"] == "peer:telegram:222333"}
        check("T7.4 privacy: peer A does not see peer B's BTC edge", "BTC" not in p1_objs)

        # T9 compactor: decay untouched fact below floor -> PROVENANCE, file survives
        cp, ci = "telegram", "444555"
        eu.ensure_stub(cp, ci, name="decayguy")
        # apply an old-dated fact just above floor so one decay pass could matter,
        # but easier: seed a below-floor-after-decay line by hand into the rep
        cpath = eu.peer_path(cp, ci)
        ctext = cpath.read_text()
        ctext = ctext.replace("## FACTS\n", "## FACTS\n- old stale fact  (conf: 0.16, ts: 2020-01-01)\n", 1)
        cpath.write_text(ctext)
        eu.run_compaction()
        after = cpath.read_text()
        check("T9.1 rep file survives compaction (never deleted)", cpath.exists())
        check("T9.2 below-floor fact moved out of FACTS", "old stale fact  (conf" not in after)
        check("T9.3 decayed fact preserved in PROVENANCE audit", "[DECAYED" in after and "old stale fact" in after)
        check("T9.4 no duplicate PROVENANCE header", after.count("## PROVENANCE") == 1)
        check("T9.5 no repeated [DECAYED] prefix", "DECAYED" in after and "] [DECAYED" not in after)

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
