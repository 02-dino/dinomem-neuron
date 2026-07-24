#!/usr/bin/env python3
"""
Person Link (NEURON Layer-2 identity graph) — cross-platform peer identity linking.

WHY: One human can hit the bot from telegram AND signal AND discord under different
platform ids. Layer-1 (peers/<platform>_<id>.md) keeps them SEPARATE by design
(privacy-by-construction). Layer-2 OPTIONALLY links them under a stable
`person:<uuid>` — but ONLY when the OWNER asserts the link. NEVER auto-merge:
a wrong merge leaks person A's rep into person B's context, which is the exact
privacy failure the whole system exists to prevent.

MODEL:
  person:<uuid>  --has_identity-->  <platform>:<platform_id>
  Stored in memory/peers/person_links.json (separate from peers_graph.json).
  Each peer rep's `person_ref:` frontmatter field points back to its person uuid.

COMMANDS:
  link   <platform:id> <platform:id> [--as-owner] [--name NAME]  # assert two ids = same person
  show   <platform:id | person:uuid>                             # resolve identities
  list                                                           # all persons + their ids
  unlink <platform:id>                                           # detach an id from its person

HARD RULES:
  - link REQUIRES --as-owner (owner-only assertion). Refuse otherwise (exit 2).
  - Never auto-merge; deriver may only SUGGEST (writes a suggestion, not a link).
  - Fail-open on read; atomic write; flock-guarded.
"""
import argparse
import json
import os
import sys
import uuid as _uuid
from pathlib import Path
from datetime import datetime, timezone

_REPO_ROOT = Path(__file__).resolve().parent.parent
LINKS_FILE = _REPO_ROOT / "memory" / "peers" / "person_links.json"
LINKS_LOCK = Path("/tmp/dinomem_person_links.lock")
PEERS_DIR = _REPO_ROOT / "memory" / "peers"


# ─── store helpers (fail-open read, atomic + flock write) ────────────────────
def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_id(raw):
    """Accept platform:id or platform_id -> canonical 'platform:id'. person:uuid passes through."""
    s = (raw or "").strip()
    if s.startswith("person:"):
        return s
    if ":" in s:
        plat, pid = s.split(":", 1)
        return f"{plat.lower()}:{pid}"
    if "_" in s:
        plat, pid = s.split("_", 1)
        return f"{plat.lower()}:{pid}"
    return s.lower()


def load_links():
    """Return links dict, or fresh skeleton. Fail-open."""
    skel = {"version": 1, "kind": "person_links", "persons": {}, "id_index": {}, "suggestions": []}
    try:
        if LINKS_FILE.exists():
            data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "persons" in data:
                data.setdefault("id_index", {})
                data.setdefault("suggestions", [])
                data.setdefault("version", 1)
                data.setdefault("kind", "person_links")
                return data
    except Exception as e:
        print(f"Warning: person_links load failed ({e}); starting fresh", file=sys.stderr)
    return skel


def _rebuild_index(data):
    """id_index maps each platform-id -> its person uuid (derived from persons)."""
    idx = {}
    for uid, rec in data.get("persons", {}).items():
        for pid in rec.get("identities", []):
            idx[pid] = uid
    data["id_index"] = idx
    return data


def save_links(data):
    """Atomic + flock write. Fail-open (returns False)."""
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(LINKS_LOCK, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
    except Exception:
        lock_fh = None
    try:
        _rebuild_index(data)
        LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LINKS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, LINKS_FILE)
        return True
    except Exception as e:
        print(f"Error: person_links write failed ({e})", file=sys.stderr)
        return False
    finally:
        if lock_fh is not None:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


# ─── commands ────────────────────────────────────────────────────────────────
def cmd_link(a):
    """Assert two platform ids belong to the same person. OWNER-ONLY.
    Never auto-merge: this is only reachable via explicit --as-owner."""
    if not a.as_owner:
        print("Refused: link is OWNER-ONLY. Pass --as-owner to confirm the caller is the owner.",
              file=sys.stderr)
        return 2
    id_a, id_b = _norm_id(a.id_a), _norm_id(a.id_b)
    if id_a.startswith("person:") or id_b.startswith("person:"):
        print("Error: link takes two platform ids (platform:id), not person uuids.", file=sys.stderr)
        return 1
    if id_a == id_b:
        print("Error: cannot link an id to itself.", file=sys.stderr)
        return 1
    data = load_links()
    idx = {}
    for uid, rec in data["persons"].items():
        for pid in rec.get("identities", []):
            idx[pid] = uid
    ua, ub = idx.get(id_a), idx.get(id_b)
    now = _now_iso()
    if ua and ub and ua != ub:
        # both already belong to DIFFERENT persons -> merge is destructive; refuse, require explicit unlink first
        print(f"Refused: {id_a} (person {ua}) and {id_b} (person {ub}) are already SEPARATE persons.\n"
              f"Merging is destructive — unlink one first, then link. (Never auto-merge.)", file=sys.stderr)
        return 2
    uid = ua or ub or f"person:{_uuid.uuid4().hex[:12]}"
    rec = data["persons"].setdefault(uid, {"created": now, "name": a.name or "", "identities": []})
    if a.name and not rec.get("name"):
        rec["name"] = a.name
    for pid in (id_a, id_b):
        if pid not in rec["identities"]:
            rec["identities"].append(pid)
    rec["last_updated"] = now
    if not save_links(data):
        return 1
    print(f"Linked: {id_a} + {id_b} -> {uid}"
          + (f'  (name: {rec["name"]})' if rec.get("name") else ""))
    _sync_person_ref(rec["identities"], uid)
    return 0


def cmd_show(a):
    tgt = _norm_id(a.target)
    data = load_links()
    if tgt.startswith("person:"):
        rec = data["persons"].get(tgt)
        if not rec:
            print(f"No person {tgt}")
            return 0
        print(f"{tgt}" + (f'  ({rec.get("name")})' if rec.get("name") else ""))
        for pid in rec.get("identities", []):
            print(f"  has_identity -> {pid}")
        return 0
    # platform id -> resolve person + siblings
    idx = {pid: uid for uid, r in data["persons"].items() for pid in r.get("identities", [])}
    uid = idx.get(tgt)
    if not uid:
        print(f"{tgt}: not linked to any person (Layer-1 only).")
        return 0
    rec = data["persons"][uid]
    print(f"{tgt} -> {uid}" + (f'  ({rec.get("name")})' if rec.get("name") else ""))
    for pid in rec.get("identities", []):
        tag = "  (this)" if pid == tgt else ""
        print(f"  has_identity -> {pid}{tag}")
    return 0


def cmd_list(a):
    data = load_links()
    persons = data.get("persons", {})
    if not persons:
        print("No linked persons yet.")
        return 0
    for uid, rec in persons.items():
        print(f"{uid}" + (f'  ({rec.get("name")})' if rec.get("name") else ""))
        for pid in rec.get("identities", []):
            print(f"  -> {pid}")
    sugg = data.get("suggestions", [])
    if sugg:
        print(f"\n{len(sugg)} pending suggestion(s) (deriver-proposed, NOT applied):")
        for s in sugg:
            print(f"  ? {s.get('id_a')} ~ {s.get('id_b')}  ({s.get('reason','')})")
    return 0


def cmd_unlink(a):
    tgt = _norm_id(a.target)
    data = load_links()
    changed = False
    for uid, rec in list(data["persons"].items()):
        if tgt in rec.get("identities", []):
            rec["identities"].remove(tgt)
            rec["last_updated"] = _now_iso()
            changed = True
            if not rec["identities"]:
                del data["persons"][uid]
                print(f"Unlinked {tgt}; person {uid} now empty -> removed.")
            else:
                print(f"Unlinked {tgt} from {uid}.")
    if not changed:
        print(f"{tgt}: was not linked.")
        return 0
    return 0 if save_links(data) else 1


def _sync_person_ref(identities, uid):
    """Write person_ref: <uid> into each linked peer rep's frontmatter (best-effort)."""
    import re
    for pid in identities:
        if ":" not in pid:
            continue
        plat, pdid = pid.split(":", 1)
        f = PEERS_DIR / f"{plat}_{pdid}.md"
        if not f.exists():
            continue
        try:
            txt = f.read_text(encoding="utf-8")
            new = re.sub(r"^person_ref:.*$", f"person_ref: {uid}", txt, count=1, flags=re.MULTILINE)
            if new != txt:
                f.write_text(new, encoding="utf-8")
        except Exception:
            pass


def cmd_suggest(a):
    """Deriver-facing: record a SUGGESTED link WITHOUT applying it. Owner reviews
    via `list` then confirms via `link --as-owner`. Never auto-merges."""
    id_a, id_b = _norm_id(a.id_a), _norm_id(a.id_b)
    data = load_links()
    for s in data.get("suggestions", []):
        if {s.get("id_a"), s.get("id_b")} == {id_a, id_b}:
            print("Suggestion already pending.")
            return 0
    data.setdefault("suggestions", []).append(
        {"id_a": id_a, "id_b": id_b, "reason": a.reason or "", "ts": _now_iso()})
    if not save_links(data):
        return 1
    print(f"Suggested (NOT applied): {id_a} ~ {id_b}. Owner confirms via: link {id_a} {id_b} --as-owner")
    return 0


def main():
    p = argparse.ArgumentParser(description="Person Link — NEURON Layer-2 cross-platform identity graph")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("link", help="Assert two platform ids = same person (OWNER-ONLY)")
    pl.add_argument("id_a"); pl.add_argument("id_b")
    pl.add_argument("--as-owner", action="store_true", dest="as_owner")
    pl.add_argument("--name", default=None)
    pl.set_defaults(fn=cmd_link)

    ps = sub.add_parser("show", help="Resolve a platform id or person uuid")
    ps.add_argument("target"); ps.set_defaults(fn=cmd_show)

    ply = sub.add_parser("list", help="List all persons + pending suggestions")
    ply.set_defaults(fn=cmd_list)

    pu = sub.add_parser("unlink", help="Detach a platform id from its person")
    pu.add_argument("target"); pu.set_defaults(fn=cmd_unlink)

    pg = sub.add_parser("suggest", help="Record a deriver-proposed link (NOT applied)")
    pg.add_argument("id_a"); pg.add_argument("id_b"); pg.add_argument("--reason", default=None)
    pg.set_defaults(fn=cmd_suggest)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
