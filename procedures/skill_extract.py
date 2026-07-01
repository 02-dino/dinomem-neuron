#!/usr/bin/env python3
"""
skill_extract.py — Feature 3: Skill Promotion for dinomem-neuron.

Promotion mode (default):
  python3 skill_extract.py
  - Scans memory/_note_*.md for type:project + status:done
  - Extracts steps into a PROPOSAL.md candidate
  - Writes kb/memory_neuron/skill_candidates/<slug>/PROPOSAL.md + meta.json
  - Checks convergence (fuzzy match >=0.75 against existing candidates)
  - At convergence_count >= 3: promotes to skills/<name>/SKILL.md

Review mode (self-fix):
  python3 skill_extract.py --review <slug>
  - Re-reads skills/<slug>/SKILL.md + flags meta.json review_flagged
  - Rewrites SKILL.md with updated procedure (LLM-free: re-extracts from source note)
  - Clears review_flagged

Decay:
  python3 skill_extract.py --prune
  - Deletes candidates with convergence_count=0 older than 60 days
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

BASE = Path("/root/.openclaw/workspace-analyst")
MEMORY_DIR = BASE / "memory"
SKILLS_DIR = BASE / "skills"
CANDIDATES_DIR = BASE / "kb/memory_neuron/skill_candidates"

CONVERGENCE_THRESHOLD = 3
FUZZY_MATCH_THRESHOLD = 0.75
PRUNE_DAYS = 60  # delete zero-convergence candidates older than this

SAFETY_KEYWORDS = {"delete", "overwrite", "drop", "rm", "truncate", "unlink", "shutil.rmtree"}


def now_iso():
    return datetime.now().isoformat()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def slugify(text: str, max_len: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return s.strip("-")[:max_len]


def parse_note(path: Path) -> dict:
    """Parse a _note_*.md file into {meta, body, steps}."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    meta = {}
    body_lines = []
    in_frontmatter = False
    frontmatter_done = False

    for i, line in enumerate(lines):
        if i == 0 and line.startswith("# "):
            meta["title"] = line[2:].strip()
            continue
        if not frontmatter_done:
            kv = re.match(r"^(\w+):\s*(.+)$", line)
            if kv:
                meta[kv.group(1)] = kv.group(2).strip()
                continue
            if line.strip() == "---":
                frontmatter_done = True
                continue
        body_lines.append(line)

    body = "\n".join(body_lines)

    # Extract steps: lines matching "- [x] N. ..." or "- [ ] N. ..."
    steps = []
    for line in body_lines:
        m = re.match(r"\s*-\s*\[([x ])\]\s*\d+\.\s*(.+)", line)
        if m:
            steps.append({
                "done": m.group(1) == "x",
                "text": m.group(2).strip(),
            })

    # Extract resume_state block
    resume_state = ""
    rs_match = re.search(r"resume_state:\s*(.+?)(?=\n\w|\Z)", body, re.DOTALL)
    if rs_match:
        resume_state = rs_match.group(1).strip()

    return {"meta": meta, "body": body, "steps": steps, "resume_state": resume_state}


def safety_scan(steps: list) -> list:
    """Return steps with inline ⚠️ warning for destructive keywords."""
    flagged = []
    for step in steps:
        text = step["text"]
        hits = [kw for kw in SAFETY_KEYWORDS if kw in text.lower()]
        if hits:
            text = f"⚠️ DESTRUCTIVE ({', '.join(hits)}): {text}"
        flagged.append({**step, "text": text})
    return flagged


def render_proposal(note_path: Path, parsed: dict) -> str:
    """Generate a SKILL.md-format proposal from a parsed project note."""
    meta = parsed["meta"]
    title = meta.get("title", note_path.stem)
    steps = safety_scan(parsed["steps"])

    lines = [
        f"# Skill: {title}",
        "",
        "```yaml",
        f"name: {slugify(title)}",
        f"description: >",
        f"  Reusable procedure extracted from project: {title}",
        "triggers:",
        f"  - {title.lower()}",
        "version: 1",
        f"source_project: {note_path.name}",
        f"extracted: {today_str()}",
        "```",
        "",
        "## Procedure",
        "",
    ]

    if steps:
        for i, step in enumerate(steps, 1):
            status = "[x]" if step["done"] else "[ ]"
            lines.append(f"{i}. {status} {step['text']}")
    else:
        lines.append("_(No structured steps found — review source note manually)_")

    if parsed["resume_state"]:
        lines += ["", "## Resume State / Context", "", parsed["resume_state"]]

    lines += [
        "",
        "## Notes",
        "",
        f"- Source: `memory/{note_path.name}`",
        f"- Extracted: {today_str()}",
        "- Review before use: steps are auto-extracted, not LLM-distilled.",
    ]

    return "\n".join(lines) + "\n"


def fuzzy_match_candidates(title: str) -> tuple:
    """Return (best_slug, best_ratio) against existing candidates."""
    best_slug, best_ratio = None, 0.0
    if not CANDIDATES_DIR.exists():
        return best_slug, best_ratio
    for meta_path in CANDIDATES_DIR.glob("*/meta.json"):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            candidate_title = m.get("title", meta_path.parent.name)
            ratio = SequenceMatcher(None, title.lower(), candidate_title.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_slug = meta_path.parent.name
        except Exception:
            continue
    return best_slug, best_ratio


def load_meta(slug: str) -> dict:
    path = CANDIDATES_DIR / slug / "meta.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_meta(slug: str, meta: dict):
    path = CANDIDATES_DIR / slug / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def promote_to_skill(slug: str, proposal_path: Path):
    """Copy PROPOSAL.md → skills/<slug>/SKILL.md."""
    skill_dir = SKILLS_DIR / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(proposal_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  [promoted] {slug} → skills/{slug}/SKILL.md")


def run_promote():
    """Main promotion loop: scan done project notes → extract → converge → promote."""
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    notes = sorted(MEMORY_DIR.glob("_note_*.md"))
    processed = 0

    for note_path in notes:
        parsed = parse_note(note_path)
        meta = parsed["meta"]

        if meta.get("type") != "project":
            continue
        if meta.get("status") != "done":
            continue

        title = meta.get("title", note_path.stem)
        slug = slugify(title)

        # Check if already promoted to a skill
        if (SKILLS_DIR / slug / "SKILL.md").exists():
            print(f"  [skip] {slug} already promoted to skills/")
            continue

        # Fuzzy match against existing candidates
        best_slug, best_ratio = fuzzy_match_candidates(title)

        if best_ratio >= FUZZY_MATCH_THRESHOLD and best_slug:
            # Reinforce existing candidate
            target_slug = best_slug
            existing_meta = load_meta(target_slug)
            existing_meta["convergence_count"] = existing_meta.get("convergence_count", 1) + 1
            existing_meta["last_seen"] = today_str()
            save_meta(target_slug, existing_meta)
            print(f"  [converge] {slug} → {target_slug} (count={existing_meta['convergence_count']})")

            # Promote if threshold reached
            if existing_meta["convergence_count"] >= CONVERGENCE_THRESHOLD:
                proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
                if proposal_path.exists():
                    promote_to_skill(target_slug, proposal_path)
        else:
            # New candidate
            target_slug = slug
            proposal = render_proposal(note_path, parsed)
            proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
            proposal_path.parent.mkdir(parents=True, exist_ok=True)
            proposal_path.write_text(proposal, encoding="utf-8")

            new_meta = {
                "title": title,
                "source_project": note_path.name,
                "date": today_str(),
                "convergence_count": 1,
                "last_seen": today_str(),
                "last_used": None,
                "use_count": 0,
                "success_count": 0,
                "error_count": 0,
                "review_flagged": False,
            }
            save_meta(target_slug, new_meta)
            print(f"  [new candidate] {target_slug} → kb/memory_neuron/skill_candidates/{target_slug}/")

        processed += 1

    if processed == 0:
        print("  [ok] no done project notes found to extract")
    else:
        print(f"  [done] processed {processed} project note(s)")


def run_review(slug: str):
    """Self-fix mode: re-extract from source note, overwrite SKILL.md."""
    meta = load_meta(slug)
    if not meta:
        print(f"  [error] no meta.json found for candidate '{slug}'")
        sys.exit(1)

    source_name = meta.get("source_project")
    if not source_name:
        print(f"  [error] meta.json for '{slug}' has no source_project field")
        sys.exit(1)

    note_path = MEMORY_DIR / source_name
    if not note_path.exists():
        print(f"  [error] source note not found: {note_path}")
        sys.exit(1)

    parsed = parse_note(note_path)
    proposal = render_proposal(note_path, parsed)

    skill_path = SKILLS_DIR / slug / "SKILL.md"
    if skill_path.exists():
        skill_path.write_text(proposal, encoding="utf-8")
        print(f"  [reviewed] rewrote skills/{slug}/SKILL.md from source note")
    else:
        # Write to candidate proposal instead
        proposal_path = CANDIDATES_DIR / slug / "PROPOSAL.md"
        proposal_path.write_text(proposal, encoding="utf-8")
        print(f"  [reviewed] rewrote candidate PROPOSAL.md (skill not yet promoted)")

    # Clear review flag
    meta["review_flagged"] = False
    meta["last_reviewed"] = today_str()
    save_meta(slug, meta)


def run_prune():
    """Delete zero-convergence candidates older than PRUNE_DAYS."""
    if not CANDIDATES_DIR.exists():
        print("  [ok] no candidates dir")
        return
    pruned = 0
    cutoff = datetime.now() - timedelta(days=PRUNE_DAYS)
    for meta_path in CANDIDATES_DIR.glob("*/meta.json"):
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            if m.get("convergence_count", 1) > 1:
                continue
            date_str = m.get("date")
            if not date_str:
                continue
            if datetime.fromisoformat(date_str) < cutoff:
                slug = meta_path.parent.name
                import shutil
                shutil.rmtree(meta_path.parent)
                print(f"  [pruned] {slug} (convergence=1, age>{PRUNE_DAYS}d)")
                pruned += 1
        except Exception as e:
            print(f"  [warn] {meta_path}: {e}")
    print(f"  [done] pruned {pruned} stale candidate(s)")


def main():
    ap = argparse.ArgumentParser(description="skill_extract.py — Skill Promotion (Feature 3)")
    ap.add_argument("--review", metavar="SLUG", help="Self-fix mode: re-extract skill from source note")
    ap.add_argument("--prune", action="store_true", help="Delete zero-convergence candidates older than 60d")
    args = ap.parse_args()

    if args.review:
        run_review(args.review)
    elif args.prune:
        run_prune()
    else:
        run_promote()


if __name__ == "__main__":
    main()
