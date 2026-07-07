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
import os
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

BASE = Path(os.environ.get("OPENCLAW_WORKSPACE", "/root/.openclaw/workspace-analyst"))
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


def _extract_triggers(title: str, steps: list) -> list:
    """Extract specific, actionable when_to_use triggers from title + steps."""
    triggers = []
    # Derive from title: make it a concrete user-facing trigger
    title_lower = title.lower()
    # Map common project title patterns to actionable triggers
    action_verbs = ["build", "create", "add", "implement", "set up", "configure",
                    "fix", "patch", "migrate", "deploy", "extract", "generate",
                    "write", "update", "refactor", "integrate", "expand"]
    has_action = any(v in title_lower for v in action_verbs)
    if has_action:
        triggers.append(f"User asks to {title_lower}")
    else:
        triggers.append(f"Task involves: {title_lower}")

    # Derive from step text — pick steps with clear action verbs as secondary triggers
    seen = set()
    for step in steps[:6]:  # cap at first 6 steps
        text = step["text"].lower()
        for verb in ["run", "write", "patch", "build", "create", "update", "commit",
                     "add", "fix", "configure", "deploy", "extract", "generate"]:
            if re.match(rf"^{verb}\b", text):
                trigger = f"Step requires: {step['text'][:60].rstrip()}"
                if trigger not in seen:
                    seen.add(trigger)
                    triggers.append(trigger)
                break
    return triggers[:4]  # max 4 triggers


def _extract_anti_patterns(steps: list) -> list:
    """Derive anti-patterns from safety-flagged steps and common pitfalls."""
    anti = []
    for step in steps:
        text = step["text"]
        if text.startswith("⚠️"):
            anti.append(f"Do not skip safety check before: {text[text.find(':')+2:80].strip()}")
    if not anti:
        anti.append("Do not skip steps — each step may have side effects")
    return anti


def render_proposal(note_path: Path, parsed: dict, source: str = "project_note",
                    convergence_count: int = 1) -> str:
    """Generate a SKILL.md-format proposal from a parsed project note."""
    meta = parsed["meta"]
    title = meta.get("title", note_path.stem)
    steps = safety_scan(parsed["steps"])
    slug = slugify(title)
    triggers = _extract_triggers(title, steps)
    anti_patterns = _extract_anti_patterns(steps)

    # YAML frontmatter
    fm_lines = [
        "---",
        f"name: {slug}",
        f"description: Reusable procedure extracted from project: {title}",
        "when_to_use:",
    ]
    for t in triggers:
        fm_lines.append(f'  - "{t}"')
    fm_lines.append("anti_patterns:")
    for ap in anti_patterns:
        fm_lines.append(f'  - "{ap}"')
    fm_lines += [
        f"source: {source}",
        f"extracted: {today_str()}",
        f"convergence_count: {convergence_count}",
        "---",
    ]

    # Markdown body
    body_lines = [
        "",
        f"# Skill: {title}",
        "",
        "## Procedure",
        "",
    ]

    if steps:
        for i, step in enumerate(steps, 1):
            body_lines.append(f"{i}. {step['text']}")
    else:
        body_lines.append("_(No structured steps found — review source note manually)_")

    if parsed.get("resume_state"):
        body_lines += ["", "## Context", "", parsed["resume_state"]]

    body_lines += [
        "",
        "## Notes",
        "",
        f"- Source: `memory/{note_path.name}`",
        f"- Extracted: {today_str()}",
        "- Review before use: auto-extracted, not LLM-distilled",
    ]

    return "\n".join(fm_lines + body_lines) + "\n"


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
            proposal = render_proposal(note_path, parsed, source="project_note", convergence_count=1)
            proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
            proposal_path.parent.mkdir(parents=True, exist_ok=True)
            proposal_path.write_text(proposal, encoding="utf-8")

            new_meta = {
                "title": title,
                "source": "project_note",
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
    existing_meta = load_meta(slug)
    proposal = render_proposal(note_path, parsed,
                               source=existing_meta.get("source", "project_note"),
                               convergence_count=existing_meta.get("convergence_count", 1))

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


# ── Cluster-based extraction (Source 2) ──────────────────────────────────────

PROCEDURAL_KEYWORDS = {
    "how to", "steps", "process", "procedure", "workflow", "pipeline",
    "run", "write", "patch", "build", "create", "update", "commit",
    "deploy", "configure", "install", "generate", "extract", "add",
    "fix", "migrate", "integrate", "implement", "set up",
}


def _cluster_is_procedural(insights: list) -> bool:
    """Return True if the cluster looks procedural (actionable how-to content)."""
    combined = " ".join(insights).lower()
    return any(kw in combined for kw in PROCEDURAL_KEYWORDS)


def run_from_cluster(cluster_title: str, cluster_insights: list, source_file: str):
    """Extract a skill candidate from an L3 cluster (Source 2).

    Called externally by memory_synthesis.py after cluster analysis.
    cluster_insights = list of insight strings from the cluster.
    source_file = path to the synthesis file that produced this cluster.
    """
    if len(cluster_insights) < 3:
        return  # too small to be a reliable pattern
    if not _cluster_is_procedural(cluster_insights):
        return  # not actionable — skip

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(cluster_title)
    title = cluster_title

    # Build a synthetic parsed dict from cluster insights
    steps = [{"done": False, "text": insight.strip()} for insight in cluster_insights]
    steps = safety_scan(steps)

    triggers = _extract_triggers(title, steps)
    anti_patterns = _extract_anti_patterns(steps)

    # YAML frontmatter
    fm_lines = [
        "---",
        f"name: {slug}",
        f"description: Skill candidate extracted from L3 memory cluster: {title}",
        "when_to_use:",
    ]
    for t in triggers:
        fm_lines.append(f'  - "{t}"')
    fm_lines.append("anti_patterns:")
    for ap in anti_patterns:
        fm_lines.append(f'  - "{ap}"')

    # Check existing convergence
    best_slug, best_ratio = fuzzy_match_candidates(title)
    if best_ratio >= FUZZY_MATCH_THRESHOLD and best_slug:
        existing_meta = load_meta(best_slug)
        convergence_count = existing_meta.get("convergence_count", 1)
    else:
        convergence_count = 1

    fm_lines += [
        "source: l3_cluster",
        f"extracted: {today_str()}",
        f"convergence_count: {convergence_count}",
        "---",
    ]

    body_lines = [
        "",
        f"# Skill: {title}",
        "",
        "## Procedure",
        "",
    ]
    for i, step in enumerate(steps, 1):
        body_lines.append(f"{i}. {step['text']}")
    body_lines += [
        "",
        "## Notes",
        "",
        f"- Source: `{Path(source_file).name}`",
        f"- Extracted: {today_str()}",
        "- Review before use: auto-extracted from L3 cluster, not LLM-distilled",
    ]

    proposal = "\n".join(fm_lines + body_lines) + "\n"

    # Convergence + promotion logic (same as project note path)
    if best_ratio >= FUZZY_MATCH_THRESHOLD and best_slug:
        target_slug = best_slug
        existing_meta = load_meta(target_slug)
        existing_meta["convergence_count"] = existing_meta.get("convergence_count", 1) + 1
        existing_meta["last_seen"] = today_str()
        save_meta(target_slug, existing_meta)
        print(f"  [cluster converge] {slug} → {target_slug} (count={existing_meta['convergence_count']})")
        if existing_meta["convergence_count"] >= CONVERGENCE_THRESHOLD:
            proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
            if proposal_path.exists():
                promote_to_skill(target_slug, proposal_path)
    else:
        target_slug = slug
        proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(proposal, encoding="utf-8")
        new_meta = {
            "title": title,
            "source": "l3_cluster",
            "source_file": source_file,
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
        print(f"  [cluster new candidate] {target_slug} → kb/memory_neuron/skill_candidates/{target_slug}/")


def run_from_cluster_file(json_file: str):
    """CLI entry point: load cluster from JSON file and run extraction."""
    data = json.loads(Path(json_file).read_text(encoding="utf-8"))
    title = data.get("title", "unknown-cluster")
    insights = data.get("insights", [])
    source_file = data.get("source_file", json_file)
    run_from_cluster(title, insights, source_file)


# ── Persistent insight extraction (Source 3) ─────────────────────────────────

PERSISTENT_PATTERN = re.compile(
    r"^(always|prefer|when|use|avoid|never|ensure|run|write|check|verify)\b",
    re.IGNORECASE,
)


def _insight_is_best_practice(insight_text: str) -> bool:
    """Return True if the insight looks like a best practice / how-to directive."""
    text = insight_text.strip()
    if PERSISTENT_PATTERN.match(text):
        return True
    lower = text.lower()
    return any(phrase in lower for phrase in ["best practice", "how to", "when x do y", "prefer x over"])


def run_from_persistent(insight_text: str, source_file: str):
    """Extract a skill candidate from a PERSISTENT.md best practice (Source 3).

    Called externally by memory_promote.py after writing to PERSISTENT.md.
    Does NOT touch PERSISTENT.md — additive only.
    convergence_count starts at 1 (needs 3 to promote — same threshold).
    """
    if not _insight_is_best_practice(insight_text):
        return  # not a best practice directive — skip

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    # Use first 60 chars of insight as title
    title = insight_text.strip()[:60].rstrip(".,:;")
    slug = slugify(title)

    # Check existing convergence
    best_slug, best_ratio = fuzzy_match_candidates(title)
    if best_ratio >= FUZZY_MATCH_THRESHOLD and best_slug:
        existing_meta = load_meta(best_slug)
        convergence_count = existing_meta.get("convergence_count", 1)
    else:
        convergence_count = 1

    # Build synthetic step from the insight itself
    steps = safety_scan([{"done": False, "text": insight_text.strip()}])
    triggers = _extract_triggers(title, steps)
    anti_patterns = _extract_anti_patterns(steps)

    fm_lines = [
        "---",
        f"name: {slug}",
        f"description: Best practice extracted from persistent memory: {title}",
        "when_to_use:",
    ]
    for t in triggers:
        fm_lines.append(f'  - "{t}"')
    fm_lines.append("anti_patterns:")
    for ap in anti_patterns:
        fm_lines.append(f'  - "{ap}"')
    fm_lines += [
        "source: persistent_insight",
        f"extracted: {today_str()}",
        f"convergence_count: {convergence_count}",
        "---",
    ]

    body_lines = [
        "",
        f"# Skill: {title}",
        "",
        "## Procedure",
        "",
    ]
    for i, step in enumerate(steps, 1):
        body_lines.append(f"{i}. {step['text']}")
    body_lines += [
        "",
        "## Notes",
        "",
        f"- Source: `{Path(source_file).name}`",
        f"- Extracted: {today_str()}",
        "- Review before use: auto-extracted from persistent insight, not LLM-distilled",
    ]

    proposal = "\n".join(fm_lines + body_lines) + "\n"

    # Convergence + promotion logic (same threshold as other sources)
    if best_ratio >= FUZZY_MATCH_THRESHOLD and best_slug:
        target_slug = best_slug
        existing_meta = load_meta(target_slug)
        existing_meta["convergence_count"] = existing_meta.get("convergence_count", 1) + 1
        existing_meta["last_seen"] = today_str()
        save_meta(target_slug, existing_meta)
        print(f"  [persistent converge] {slug} → {target_slug} (count={existing_meta['convergence_count']})")
        if existing_meta["convergence_count"] >= CONVERGENCE_THRESHOLD:
            proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
            if proposal_path.exists():
                promote_to_skill(target_slug, proposal_path)
    else:
        target_slug = slug
        proposal_path = CANDIDATES_DIR / target_slug / "PROPOSAL.md"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(proposal, encoding="utf-8")
        new_meta = {
            "title": title,
            "source": "persistent_insight",
            "source_file": source_file,
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
        print(f"  [persistent new candidate] {target_slug} → kb/memory_neuron/skill_candidates/{target_slug}/")


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
    ap.add_argument("--from-cluster", metavar="JSON_FILE",
                    help="Extract skill from L3 cluster JSON {title, insights: [], source_file}")
    ap.add_argument("--from-persistent", metavar="INSIGHT_TEXT",
                    help="Extract skill from a PERSISTENT.md best practice insight")
    ap.add_argument("--persistent-source", metavar="SOURCE_FILE", default="",
                    help="Source file path for --from-persistent (optional)")
    args = ap.parse_args()

    if args.review:
        run_review(args.review)
    elif args.prune:
        run_prune()
    elif args.from_cluster:
        run_from_cluster_file(args.from_cluster)
    elif args.from_persistent:
        source = args.persistent_source or "PERSISTENT.md"
        run_from_persistent(args.from_persistent, source)
    else:
        run_promote()


if __name__ == "__main__":
    main()
