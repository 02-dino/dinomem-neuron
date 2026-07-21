#!/usr/bin/env python3
"""
promotion_dedup.py — Cross-repo dedup gate for hook/cron behavior promotion.

Neuron installs ON TOP OF dinomem-base. Base's hook_tool/cron_tool already block
same-NAME collisions inside their own repo. But neuron promotes with its OWN slug for
a behavior base may ALREADY have installed under a different name -> base's name check
passes -> a DUPLICATE hook/cron installs -> it fires twice (double reply, double cron).

This gate closes that seam. Before neuron stages a hook/cron proposal it asks:
  1. LIVE STATE  — is a hook/cron already installed in openclaw.json doing this?
                   (queried via `openclaw hooks list --json` / `openclaw cron list --json`,
                    the SAME source base's tools read — with a filesystem fallback.)
  2. ALREADY STAGED — did neuron already stage/confirm this exact proposal? (avoid re-stamp
                    pile-up every promotion cycle.)
Matching is by NAME (exact/substring) AND by FUZZY BEHAVIOR (title/desc >= 0.72), so a
same-behavior-different-slug duplicate is caught, not just a same-name one.

Returns a verdict; a DUPLICATE verdict means the caller must NOT stage (skip, don't clutter).
"""
import json
import os
import subprocess
from difflib import SequenceMatcher

# behavior-similarity threshold: two descriptions this close = "same behavior"
BEHAVIOR_SIM = 0.72

import re as _re

# -- LANGUAGE-INDEPENDENT action-kind detection -------------------------------------------
# The action-kind tiebreaker must NOT depend on the user's spoken language. Neuron users
# write behavior descriptions in ANY language (Indonesian, Spanish, Chinese, ...), so prose
# keywords are unreliable. PRIMARY signal = CODE/API tokens in the machine payload neuron
# passes to base tools (--action TS snippet for hooks; --command/--message/--system-event
# for crons). Code is language-neutral: sendMessage(, .push(, fs.appendFile(, unlink( read
# the same in every human language. Prose keywords are a LAST-DITCH English fallback only.
_CODE_KINDS = {
    "send":     r"(sendmessage|\bsend\(|\breply\(|\.push\(|messages\.push|sendtext|postmessage|notify\(|api\.(send|reply|message))",
    "log":      r"(console\.(log|warn|info)|appendfile|writefile.*log|logger?\.|fs\.append|>>\s*\S+\.log|logline)",
    "snapshot": r"(backup|snapshot|archive|\bzip\b|\btar\b|cp\s+-r|rsync|dump\(|copyfile)",
    "inject":   r"(messages\.unshift|prepend|inject|context\.\w+\s*=|bootstrap|preload|systemprompt)",
    "cleanup":  r"(unlink\(|rm\s+-|rmdir|\bdelete\b|prune|purge|rotate|truncate|fs\.(rm|unlink))",
    "fetch":    r"(fetch\(|axios|http\.(get|request)|\bcurl\b|\bwget\b|\.get\(|download|requests\.)",
    "report":   r"(summar|digest|recap|report\(|buildreport|generatereport)",
}

# Prose keywords: ENGLISH fallback only (used when the payload carries no code tokens).
# 'message' is EXCLUDED from 'send' (it's the event noun, not the verb).
_PROSE_KINDS = {
    "send":     r"\b(send|reply|replies|respond|responds|notify|notifies|ping|announce|announces|dm|post|posts)\b",
    "log":      r"\b(log|logs|record|records|append|appends|audit|track|tracks)\b",
    "snapshot": r"\b(snapshot|snapshots|backup|back up|backs up|archive|archives|dump|dumps|zip|zips)\b",
    "inject":   r"\b(inject|injects|prepend|prepends|prime|primes|bootstrap|preload|preloads|remind|reminds)\b",
    "cleanup":  r"\b(clean|cleans|prune|prunes|delete|deletes|purge|purges|rotate|rotates|gc|retire|retires|sweep|sweeps)\b",
    "fetch":    r"\b(fetch|fetches|scrape|scrapes|pull|pulls|sync|syncs|download|downloads|poll|polls|refresh|refreshes)\b",
    "report":   r"\b(report|reports|summar|digest|digests|recap|recaps|brief|briefs)\b",
}

def _action_kinds(text: str) -> frozenset:
    """Language-INDEPENDENT action-kind detection. Prefer CODE/API tokens (neutral across
    all human languages); fall back to English prose keywords ONLY when no code is present.
    Returns 0..n kinds; empty is SAFE (structural sig still dedups, kind-tiebreaker abstains)."""
    t = (text or "").lower()
    if not t:
        return frozenset()
    code_hits = frozenset(k for k, pat in _CODE_KINDS.items() if _re.search(pat, t))
    if code_hits:
        return code_hits
    return frozenset(k for k, pat in _PROSE_KINDS.items() if _re.search(pat, t))


def _script_family(s: str) -> str:
    """Coarse script bucket of dominant non-ASCII range, for gating cross-language fuzzy."""
    counts = {"cjk": 0, "cyrillic": 0, "arabic": 0, "latin": 0, "other": 0}
    for ch in s or "":
        if ch.isascii():
            continue
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            counts["cjk"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x0600 <= o <= 0x06FF:
            counts["arabic"] += 1
        elif 0x00C0 <= o <= 0x024F:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    top = max(counts, key=counts.get)
    return top if counts[top] > 0 else "ascii"

def _same_script(a: str, b: str) -> bool:
    """Cross-language fuzzy prose comparison is meaningless; only trust it within the same
    script family. ASCII and latin are treated as compatible (English/romanized share it)."""
    fa, fb = _script_family(a), _script_family(b)
    compat = {"ascii", "latin"}
    if fa in compat and fb in compat:
        return True
    return fa == fb


# hook event aliases -> canonical (family listener == matches any action in family)
def _hook_signature(event: str) -> str:
    ev = _norm(event)
    if not ev:
        return ""
    # collapse family:action to family for matching a family listener
    fam = ev.split(":", 1)[0] if ":" in ev else ev
    return f"event:{ev}|family:{fam}"


def _sig_overlap(a: str, b: str) -> bool:
    """Do two signatures overlap? Split on '|' into token sets and require a shared token.
    For hooks: an exact event OR a family listener match counts (event:message:received
    overlaps family:message). For crons: exact schedule OR digits-skeleton match."""
    at = set(t for t in a.split("|") if t and not t.endswith(":"))
    bt = set(t for t in b.split("|") if t and not t.endswith(":"))
    # drop empty-value tokens like 'skel:' or 'family:'
    at = {t for t in at if t.split(":", 1)[-1]}
    bt = {t for t in bt if t.split(":", 1)[-1]}
    return bool(at & bt)


def _cron_signature(schedule: str) -> str:
    """Normalize a schedule to a comparable signature. every:DUR and cron:EXPR@TZ
    both reduce to a cadence token; wall-clock crons compare on the cron expr."""
    s = _norm(schedule)
    if not s:
        return ""
    # strip kind prefixes the two repos use interchangeably
    for pre in ("every:", "cron:", "at:", "schedule:"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    # a bare interval like '6h' vs '6 h' vs '0 */6 * * *' won't always align; we compare
    # the raw normalized token AND a digits-only skeleton to catch spacing/format drift
    skel = _re.sub(r"[^0-9*/]", "", s)
    return f"sched:{s}|skel:{skel}"


def _run_json(argv):
    """Run a CLI that emits JSON; return parsed list/dict or None on any failure."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# —— MULTILINGUAL semantic similarity (optional; reuses neuron's e5 model) ——————————————
# When two behavior descriptions are in DIFFERENT languages, both string-fuzzy and English
# keyword tiers go blind. Neuron already ships intfloat/multilingual-e5-small (EN/ID + all
# majors) via tools/_embed.py. We reuse it for a language-crossing behavior match: it embeds
# "balas pengirim" and "reply to sender" close together. Lazy + optional: if the embedding
# stack isn't installed, this abstains (returns None) and we fall to keyword/structural —
# the semantic tier only ever ADDS recall, never removes the safe degradation path.
SEMANTIC_SIM = 0.82   # cosine threshold; e5 normalized -> dot == cosine. Higher than the
                      # string-fuzzy 0.72 because e5 similarities run hotter (0.7+ common).
_sem_embedder = None
_sem_state = None     # None=untried, False=unavailable, True=ready

def _get_semantic():
    """Lazily load neuron's shared multilingual embedder. Returns callable(texts)->vectors
    or None if unavailable. Never raises."""
    global _sem_embedder, _sem_state
    if _sem_state is not None:
        return _sem_embedder if _sem_state else None
    try:
        import os as _os, sys as _sys
        # tools/_embed.py lives one dir up from procedures/
        _tools = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
        if _tools not in _sys.path:
            _sys.path.insert(0, _tools)
        import _embed  # type: ignore
        emb = _embed.get_embeddings()
        _sem_embedder = emb
        _sem_state = True
        return emb
    except Exception:
        _sem_state = False
        _sem_embedder = None
        return None

def _semantic_sim(a: str, b: str):
    """Cosine similarity of two descriptions via the multilingual model. Returns float in
    [0,1] or None if the model is unavailable (caller then abstains from this tier)."""
    emb = _get_semantic()
    if emb is None or not (a and b):
        return None
    try:
        va = emb.embed_query(a)
        vb = emb.embed_query(b)
        # e5 vectors are L2-normalized (encode_kwargs normalize_embeddings=True) -> dot=cosine
        dot = sum(x * y for x, y in zip(va, vb))
        return max(0.0, min(1.0, dot))
    except Exception:
        return None


def _iter_records(payload):
    """OpenClaw list JSON shapes vary (list, {hooks:[...]}, {jobs:[...]}, {items:[...]}).
    Yield dict records defensively from whatever we got."""
    if payload is None:
        return
    if isinstance(payload, list):
        for x in payload:
            if isinstance(x, dict):
                yield x
        return
    if isinstance(payload, dict):
        for key in ("hooks", "jobs", "crons", "items", "data", "result"):
            v = payload.get(key)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        yield x
                return
        # dict-of-records (name -> record)
        for k, v in payload.items():
            if isinstance(v, dict):
                v.setdefault("name", k)
                yield v


def _record_text(rec: dict) -> tuple:
    """Extract (name, behavior_text) from a live hook/cron record, tolerant of key names."""
    name = rec.get("name") or rec.get("id") or rec.get("slug") or ""
    parts = [
        rec.get("description"), rec.get("desc"), rec.get("title"),
        rec.get("event"), rec.get("action"), rec.get("systemEvent"),
        rec.get("message"), rec.get("command"), rec.get("schedule"),
    ]
    behavior = " ".join(str(p) for p in parts if p)
    return name, behavior


def _live_records(surface: str):
    """surface in {hook, cron}. Query live runtime state; fall back to fs if CLI absent."""
    if surface == "hook":
        payload = _run_json(["openclaw", "hooks", "list", "--json"])
        fs_dir = os.environ.get("DINOMEM_HOOKS_DIR")  # optional fs fallback (base hooks/)
    else:
        payload = _run_json(["openclaw", "cron", "list", "--json"])
        fs_dir = None
    recs = list(_iter_records(payload))
    if recs:
        return recs, "live"
    # filesystem fallback: just names, no behavior text (weaker but better than nothing)
    if fs_dir and os.path.isdir(fs_dir):
        try:
            return [{"name": n} for n in os.listdir(fs_dir)], "fs"
        except Exception:
            pass
    return [], "none"


def check_duplicate(surface: str, slug: str, title: str, behavior_text: str,
                    staged_slugs=None) -> dict:
    """
    surface: 'hook' | 'cron'
    slug: neuron's proposed slug
    title/behavior_text: what the proposed behavior does
    staged_slugs: iterable of slugs neuron ALREADY staged/confirmed (re-stage guard)

    Returns {duplicate: bool, source: 'live'|'staged'|None, match: <name>, reason, sim,
             query_status: 'live'|'fs'|'none'}.
    """
    proposed_behavior = f"{title} {behavior_text}".strip()
    prop_sig = _hook_signature(behavior_text) if surface == "hook" else _cron_signature(behavior_text)
    prop_kinds = _action_kinds(proposed_behavior)

    # 0) re-stage guard: neuron already emitted this exact slug -> don't re-stamp.
    if staged_slugs and slug in set(staged_slugs):
        return {"duplicate": True, "source": "staged", "match": slug, "tier": "staged",
                "reason": "already staged/confirmed by neuron in a prior cycle — not re-staging",
                "sim": 1.0, "query_status": "n/a"}

    recs, qstatus = _live_records(surface)
    ns = _norm(slug)
    name_match = None
    struct_match = None          # (name, shared_kinds)
    fuzzy_best = (0.0, "")       # (sim, name)
    semantic_best = (0.0, "")    # (cosine, name) — multilingual, cross-script

    for rec in recs:
        rname, rbehavior = _record_text(rec)
        nn = _norm(rname)

        # TIER 1 — exact/substring NAME match (strongest, unambiguous)
        if bool(nn) and (ns == nn or ns in nn or nn in ns):
            name_match = rname

        # TIER 2 — STRUCTURAL signature (event / schedule) + shared action-kind.
        # Same signature alone is NOT enough (two hooks on message:received can differ);
        # they must ALSO share at least one action-kind to be a real double-fire dup.
        rsig = (_hook_signature(rec.get("event", "") or rbehavior) if surface == "hook"
                else _cron_signature(rec.get("schedule", "") or rbehavior))
        if prop_sig and rsig and _sig_overlap(prop_sig, rsig):
            shared = prop_kinds & _action_kinds(rbehavior)
            if shared and struct_match is None:
                struct_match = (rname, shared)

        # TIER 3 — fuzzy behavior fallback (for records with no clean event/schedule field).
        # SCRIPT-GATED: cross-language prose similarity is meaningless ("kirim balasan" vs
        # "reply to sender" ~ 0). Only trust fuzzy WITHIN the same script family; otherwise
        # abstain — structural sig + name already cover the real double-fire case.
        if rbehavior and _same_script(proposed_behavior, rbehavior):
            bsim = _sim(proposed_behavior, rbehavior)
            if bsim > fuzzy_best[0]:
                fuzzy_best = (bsim, rname)
        # TIER 3.5 — MULTILINGUAL semantic match (cross-language, cross-script). Catches the
        # case string-fuzzy can't: same behavior described in different languages. Optional;
        # abstains cleanly if the embedding model isn't installed.
        if rbehavior:
            ssim = _semantic_sim(proposed_behavior, rbehavior)
            if ssim is not None and ssim > semantic_best[0]:
                semantic_best = (ssim, rname)

    if name_match:
        return {"duplicate": True, "source": "live", "match": name_match, "tier": "name",
                "reason": f"a {surface} named like '{slug}' is already installed",
                "sim": 1.0, "query_status": qstatus}
    if struct_match:
        nm, shared = struct_match
        return {"duplicate": True, "source": "live", "match": nm or "(unnamed)", "tier": "structural",
                "reason": (f"installed {surface} '{nm}' shares the same "
                           f"{'event' if surface == 'hook' else 'schedule'} signature AND action "
                           f"kind {sorted(shared)}; installing would DOUBLE-fire"),
                "sim": 1.0, "query_status": qstatus}
    if fuzzy_best[0] >= BEHAVIOR_SIM:
        return {"duplicate": True, "source": "live", "match": fuzzy_best[1] or "(unnamed)", "tier": "fuzzy",
                "reason": (f"an installed {surface} '{fuzzy_best[1]}' already does this behavior "
                           f"(text similarity {fuzzy_best[0]:.2f}); installing would DOUBLE-fire"),
                "sim": round(fuzzy_best[0], 3), "query_status": qstatus}
    if semantic_best[0] >= SEMANTIC_SIM:
        return {"duplicate": True, "source": "live", "match": semantic_best[1] or "(unnamed)", "tier": "semantic",
                "reason": (f"an installed {surface} '{semantic_best[1]}' does the same thing described "
                           f"in another language (multilingual similarity {semantic_best[0]:.2f}); "
                           f"installing would DOUBLE-fire"),
                "sim": round(semantic_best[0], 3), "query_status": qstatus}

    return {"duplicate": False, "source": None, "match": None, "tier": None,
            "reason": "no live or staged duplicate found",
            "sim": round(max(fuzzy_best[0], semantic_best[0]), 3),
            "query_status": qstatus}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4:
        surface, slug, title = sys.argv[1], sys.argv[2], sys.argv[3]
        behavior = " ".join(sys.argv[4:])
        print(json.dumps(check_duplicate(surface, slug, title, behavior), indent=2))
    else:
        print("usage: promotion_dedup.py <hook|cron> <slug> <title> <behavior...>")
