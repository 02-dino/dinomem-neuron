#!/usr/bin/env python3
"""
contradiction_check.py — contradiction guard for neuron-firing insights.

Reads the promoted insight history (insight_history.jsonl) and checks whether a
candidate insight contradicts any existing trusted insight. If it does, the new
insight is blocked (stays provisional) and the contradiction is logged.

Self-healing: if the old insight later decays (confidence drops or is invalidated),
the blocked insight can be re-evaluated and promoted.

Usage:
  python3 contradiction_check.py --check <candidate.json>
  python3 contradiction_check.py --candidate '{"pattern": "...", ...}'
  python3 contradiction_check.py --report
  python3 contradiction_check.py --re-evaluate
"""

import argparse
import json
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# Reuse the LLM caller
sys.path.insert(0, str(Path(__file__).parent))
from extract_memory import call_llm  # noqa: E402

# Workspace resolution (priority): DINOMEM_WORKSPACE env var > install-time sed
# substitution of DINOMEM_WORKSPACE_PLACEHOLDER > self-locate from this file's
# location. Self-locate is a safety net if the install-time sed was
# skipped/failed (manual copy, partial install, moved workspace dir).
import os as _os
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
_WS = Path(_os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
_L3 = _WS / "kb/memory_neuron/l3_synthesis"
INSIGHT_HISTORY = _L3 / "insight_history.jsonl"
CONTRADICTION_LOG = _L3 / "contradiction_log.jsonl"
CONTRADICTION_QUEUE = _L3 / "contradiction_queue.jsonl"

TOPIC_SIM_THRESHOLD = 0.20  # lowered: short statements vs long explanations need looser match
MIN_KEYWORDS_OVERLAP = 2   # need at least 2 meaningful shared keywords to consider related


def load_history():
    """Load all promoted insights from history."""
    insights = []
    if not INSIGHT_HISTORY.exists():
        return insights
    for line in INSIGHT_HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            # promoted = trusted/graduated insight
            if obj.get("promoted", True):  # backward-compat: default promoted
                insights.append(obj)
        except json.JSONDecodeError:
            continue
    return insights


_STOPWORDS = {
    'that', 'with', 'from', 'they', 'them', 'have', 'been', 'were', 'this', 'than',
    'also', 'only', 'more', 'most', 'very', 'what', 'when', 'where', 'which', 'while',
    'will', 'would', 'could', 'should', 'about', 'after', 'before', 'during', 'above',
    'below', 'under', 'over', 'into', 'onto', 'upon', 'out', 'off', 'down', 'just',
    'even', 'still', 'already', 'never', 'always', 'often', 'sometimes', 'usually',
    'really', 'actually', 'probably', 'definitely', 'certainly', 'possibly', 'maybe',
    'perhaps', 'likely', 'surely', 'clearly', 'obviously', 'apparently', 'seemingly',
    'potentially', 'essentially', 'basically', 'generally', 'specifically',
    'particularly', 'especially', 'notably', 'significantly', 'substantially',
    'considerably', 'relatively', 'comparatively', 'absolutely', 'completely',
    'entirely', 'totally', 'fully', 'partly', 'partially', 'hardly', 'barely',
    'scarcely', 'nearly', 'almost', 'quite', 'rather', 'pretty', 'fairly',
    'somewhat', 'somehow', 'somewhere', 'someone', 'somebody', 'something',
    'anything', 'anybody', 'anyone', 'anywhere', 'everybody', 'everyone',
    'everything', 'everywhere', 'nothing', 'nobody', 'nowhere', 'none', 'other',
    'another', 'each', 'every', 'all', 'both', 'either', 'neither', 'such',
    'these', 'those', 'than', 'then', 'now', 'here', 'there', 'thus', 'hence',
    'since', 'until', 'unless', 'although', 'though', 'because', 'while',
    'once', 'whenever', 'wherever', 'however', 'moreover', 'furthermore',
    'nevertheless', 'nonetheless', 'otherwise', 'instead', 'meanwhile', 'besides',
    'accordingly', 'consequently', 'therefore', 'so', 'yet', 'indeed', 'same',
    'different', 'like', 'unlike', 'as', 'too', 'very', 'just', 'even', 'also',
    'enough', 'does', 'doesn', 'doesnt', 'done', 'don', 'dont', 'did', 'didn',
    'didnt', 'doing', 'done', 'have', 'has', 'had', 'hadn', 'hadnt', 'having',
    'will', 'won', 'wont', 'would', 'wouldn', 'wouldnt', 'shall', 'should',
    'shouldn', 'shouldnt', 'may', 'might', 'must', 'mustn', 'mustnt', 'can',
    'can', 'cannot', 'cant', 'could', 'couldn', 'couldnt', 'need', 'needn',
    'neednt', 'dare', 'daren', 'darent', 'ought', 'used', 'use', 'using',
    'get', 'gets', 'got', 'gotten', 'getting', 'go', 'goes', 'going', 'went',
    'gone', 'make', 'makes', 'making', 'made', 'take', 'takes', 'taking',
    'took', 'taken', 'come', 'comes', 'coming', 'came', 'know', 'knows',
    'knowing', 'knew', 'known', 'think', 'thinks', 'thinking', 'thought',
    'say', 'says', 'saying', 'said', 'see', 'sees', 'seeing', 'saw', 'seen',
    'look', 'looks', 'looking', 'looked', 'want', 'wants', 'wanting', 'wanted',
    'give', 'gives', 'giving', 'gave', 'given', 'use', 'uses', 'using', 'used',
    'find', 'finds', 'finding', 'found', 'tell', 'tells', 'telling', 'told',
    'ask', 'asks', 'asking', 'asked', 'work', 'works', 'working', 'worked',
    'seem', 'seems', 'seeming', 'seemed', 'feel', 'feels', 'feeling', 'felt',
    'try', 'tries', 'trying', 'tried', 'leave', 'leaves', 'leaving', 'left',
    'call', 'calls', 'calling', 'called', 'good', 'new', 'first', 'last',
    'long', 'great', 'little', 'own', 'other', 'old', 'right', 'big', 'high',
    'small', 'large', 'next', 'early', 'young', 'important', 'few', 'public',
    'bad', 'same', 'able', 'back', 'down', 'off', 'over', 'under', 'again',
    'further', 'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how',
    'all', 'any', 'both', 'each', 'few', 'more', 'most', 'other', 'some',
    'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 'just', 'now', 'also', 'back', 'after', 'use', 'two', 'how',
    'our', 'work', 'first', 'well', 'way', 'even', 'new', 'want', 'because',
    'any', 'these', 'give', 'day', 'most', 'us', 'is', 'are', 'was', 'were',
    'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'shall', 'should', 'may', 'might', 'must', 'can', 'could', 'need',
    'dare', 'ought', 'used', 'get', 'got', 'make', 'made', 'take', 'took',
    'come', 'came', 'know', 'knew', 'think', 'thought', 'say', 'said', 'see',
    'saw', 'look', 'want', 'give', 'gave', 'find', 'found', 'tell', 'told',
    'ask', 'asked', 'work', 'worked', 'seem', 'seemed', 'feel', 'felt', 'try',
    'tried', 'leave', 'left', 'call', 'called', 'good', 'new', 'first',
    'last', 'long', 'great', 'little', 'own', 'old', 'right', 'big', 'high',
    'small', 'large', 'next', 'early', 'young', 'important', 'few', 'public',
    'bad', 'able', 'back', 'down', 'off', 'over', 'under', 'again', 'further',
    'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all',
    'any', 'both', 'each', 'more', 'most', 'some', 'such', 'no', 'nor', 'not',
    'only', 'so', 'than', 'too', 'very', 'just', 'now', 'also', 'two', 'how',
    'our', 'well', 'way', 'even', 'because', 'any', 'give', 'day', 'most', 'us'
}


def keyword_overlap(a, b):
    """Check for meaningful keyword overlap between two insights.
    Returns number of shared meaningful keywords (4+ chars, not stopwords)."""
    def extract(text):
        words = set(re.findall(r'[a-z]{4,}', text.lower()))
        return words - _STOPWORDS

    a_words = extract(a.get("pattern", "") + " " + a.get("why", ""))
    b_words = extract(b.get("pattern", "") + " " + b.get("why", ""))
    if not a_words or not b_words:
        return 0
    return len(a_words & b_words)


def topic_similarity(a, b):
    """Quick text similarity check to decide if a deep comparison is worth it."""
    a_text = (a.get("pattern", "") + " " + a.get("why", "")).lower()
    b_text = (b.get("pattern", "") + " " + b.get("why", "")).lower()
    return SequenceMatcher(None, a_text, b_text).ratio()


CONTRA_PROMPT = """You are a contradiction checker for an AI agent's memory system.

EXISTING INSIGHT (already trusted):
Title: {existing_pattern}
Details: {existing_why}

CANDIDATE INSIGHT (wants to be promoted):
Title: {candidate_pattern}
Details: {candidate_why}

A contradiction exists when:
(a) The candidate says the OPPOSITE of the existing insight on a core claim
(b) The candidate invalidates a key mechanism the existing insight relies on
(c) They cannot both be true in the same market context

Mere disagreement is NOT contradiction. Differing risk levels, different timeframes,
or different conclusions from the same data are NOT contradictions.

Output ONLY valid JSON:
{{
  "contradiction": true or false,
  "severity": "blocking" or "warning" or "none",
  "reason": "one sentence",
  "shared_domain": true or false
}}"""


def llm_contradiction_check(new_insight, existing_insight):
    """Ask LLM if new insight contradicts existing one."""
    prompt = CONTRA_PROMPT.format(
        existing_pattern=existing_insight.get("pattern", ""),
        existing_why=existing_insight.get("why", ""),
        candidate_pattern=new_insight.get("pattern", ""),
        candidate_why=new_insight.get("why", ""),
    )

    ok, raw = call_llm(prompt, max_tokens=400, reasoning=True)
    if not ok or not raw:
        return {
            "contradiction": False,
            "severity": "none",
            "reason": "LLM call failed — treated as non-contradiction",
            "shared_domain": False,
        }

    try:
        s = raw.find("{")
        e = raw.rfind("}")
        if s != -1 and e != -1:
            obj = json.loads(raw[s : e + 1])
            return obj
    except json.JSONDecodeError:
        pass

    return {
        "contradiction": False,
        "severity": "none",
        "reason": "parse error — treated as non-contradiction",
        "shared_domain": False,
    }


def rules_contradict(new_rule, existing_rules):
    """Rule-level adapter for the LIVE L4 graduation gate (memory_promote.py).

    Wraps the SAME LLM judgment (`llm_contradiction_check` + CONTRA_PROMPT) used by
    the insight-level checker, so there is ONE prompt / one logic — no drift. Takes
    plain rule_line strings. Returns (contradicts: bool, reason: str). Fail-open:
    any error → (False, ...) so a flaky model never stalls the pipeline.
    """
    existing_rules = [r for r in (existing_rules or []) if r and r.strip()]
    if not existing_rules:
        return (False, "")
    try:
        new = {"pattern": new_rule, "why": ""}
        for r in existing_rules:
            res = llm_contradiction_check(new, {"pattern": r, "why": ""})
            if res.get("contradiction") and res.get("severity") == "blocking":
                return (True, f"conflicts with «{r}»: {res.get('reason', '')}")
        return (False, "")
    except Exception as ex:
        return (False, f"check skipped ({type(ex).__name__}) — allowed")


def check_contradiction(candidate_insight, verbose=False):
    """Check if candidate contradicts any existing promoted insight.

    Returns dict:
      status: "approved" | "warning" | "blocked"
      reason: str
      contradictions: list of detail objects
    """
    history = load_history()
    if not history:
        return {
            "status": "approved",
            "reason": "no history — nothing to contradict",
            "contradictions": [],
        }

    contradictions = []
    for existing in history:
        # Dual pre-filter: text similarity OR keyword overlap
        sim = topic_similarity(candidate_insight, existing)
        k_overlap = keyword_overlap(candidate_insight, existing)
        
        # Pass if EITHER: text similarity above threshold OR meaningful keyword overlap
        if sim < TOPIC_SIM_THRESHOLD and k_overlap < MIN_KEYWORDS_OVERLAP:
            continue

        if verbose:
            print(f"  deep-check against '{existing.get('pattern', '')[:50]}...' (sim={sim:.2f}, keywords={k_overlap})")

        result = llm_contradiction_check(candidate_insight, existing)
        if result.get("contradiction"):
            contradictions.append({
                "existing_pattern": existing.get("pattern", ""),
                "existing_date": existing.get("first_seen", "unknown"),
                "existing_confidence": existing.get("confidence", 0),
                "severity": result.get("severity", "warning"),
                "reason": result.get("reason", ""),
                "topic_similarity": round(sim, 2),
                "keyword_overlap": k_overlap,
            })

    blocking = [c for c in contradictions if c["severity"] == "blocking"]
    warnings = [c for c in contradictions if c["severity"] == "warning"]

    if blocking:
        return {
            "status": "blocked",
            "reason": f"{len(blocking)} blocking contradiction(s) found",
            "contradictions": contradictions,
        }
    elif warnings:
        return {
            "status": "warning",
            "reason": f"{len(warnings)} warning(s) found",
            "contradictions": contradictions,
        }
    else:
        return {
            "status": "approved",
            "reason": "no contradictions found",
            "contradictions": [],
        }


def log_contradiction(candidate, result):
    """Log a contradiction check to the contradiction log."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "candidate_pattern": candidate.get("pattern", "")[:100],
        "result_status": result["status"],
        "result_reason": result["reason"],
        "contradictions_count": len(result["contradictions"]),
    }
    with CONTRADICTION_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def queue_blocked(candidate, result):
    """Add a blocked insight to the contradiction queue for later re-evaluation."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "candidate": candidate,
        "blocking_contradictions": result["contradictions"],
        "status": "blocked",
    }
    with CONTRADICTION_QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_queue():
    """Load all blocked insights waiting for re-evaluation."""
    if not CONTRADICTION_QUEUE.exists():
        return []
    entries = []
    for line in CONTRADICTION_QUEUE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def report():
    """Print a summary of the contradiction system state."""
    history = load_history()
    blocked = load_queue()
    log_entries = []
    if CONTRADICTION_LOG.exists():
        for line in CONTRADICTION_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    log_entries.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

    print("=== Contradiction Guard Report ===")
    print(f"Promoted insights:   {len(history)}")
    print(f"Blocked insights:    {len(blocked)}")
    print(f"Total checks logged: {len(log_entries)}")
    print()

    if blocked:
        print("Blocked insights waiting:")
        for b in blocked:
            print(f"  - {b['candidate']['pattern'][:60]}... ({b['timestamp'][:10]})")
        print()

    if log_entries:
        from collections import Counter
        statuses = Counter(e["result_status"] for e in log_entries)
        print("Check outcomes:")
        for status, count in statuses.most_common():
            print(f"  {status}: {count}")
        print()

    print(f"Files:")
    print(f"  {INSIGHT_HISTORY}")
    print(f"  {CONTRADICTION_QUEUE}")
    print(f"  {CONTRADICTION_LOG}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", type=str, help="JSON file containing candidate insight")
    ap.add_argument("--candidate", type=str, help="JSON string of candidate insight")
    ap.add_argument("--report", action="store_true", help="show contradiction report")
    ap.add_argument("--re-evaluate", action="store_true", help="re-evaluate blocked insights (placeholder)")
    ap.add_argument("--verbose", action="store_true", help="show deep-check details")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    if args.re_evaluate:
        # Placeholder: could re-run contradiction check for all queued entries
        # after history has changed (e.g., an old insight decayed or was removed)
        print("Re-evaluate: load queued entries and re-check them")
        blocked = load_queue()
        if not blocked:
            print("No blocked insights to re-evaluate.")
            return 0
        print(f"Re-evaluating {len(blocked)} blocked insight(s)...")
        for b in blocked:
            result = check_contradiction(b["candidate"], verbose=args.verbose)
            if result["status"] == "approved":
                print(f"  ✅ UNBLOCKED — {b['candidate']['pattern'][:60]}...")
            else:
                print(f"  ⛔ still blocked — {b['candidate']['pattern'][:60]}...")
        return 0

    if args.check:
        candidate = json.loads(Path(args.check).read_text(encoding="utf-8"))
    elif args.candidate:
        candidate = json.loads(args.candidate)
    else:
        ap.print_help()
        return 1

    result = check_contradiction(candidate, verbose=args.verbose)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    log_contradiction(candidate, result)

    if result["status"] == "blocked":
        queue_blocked(candidate, result)
        print("\n⛔ BLOCKED — added to contradiction queue")
        return 1
    elif result["status"] == "warning":
        print("\n⚠️  WARNING — contradictions present but not blocking")
        return 0
    else:
        print("\n✅ APPROVED — no contradictions")
        return 0


if __name__ == "__main__":
    # ---- dinomem-neuron license guard ----
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from license_check import enforce_license as _enforce_license
        _enforce_license(_os.path.basename(__file__))
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(main())
