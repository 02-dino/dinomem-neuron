#!/usr/bin/env python3
"""task_brief.py — extract a single step from a project note for scoped subagent dispatch.

WHY: The Advancer subagent often receives the full _note_ file as context. As notes
grow (rulings, logs, past resume_states), the subagent gets 10-40k chars of which
~99% is irrelevant to the current step. This script extracts ONLY what the
implementer needs: the target step text, interfaces from the previous step, global
constraints, and done_when — keeping dispatch prompts tight.

Usage:
  python3 task_brief.py <note_path> <step_N>
  python3 task_brief.py memory/_note_project_foo.md 3

Output: writes to /tmp/task-brief-<slug>-step<N>.md and prints the path.
Stdout = path only (so callers can capture it cleanly).

Exit 0 on success, 1 on error.
"""
import re
import sys
from pathlib import Path


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Fail-open: returns ({}, text) on parse error."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end].strip()
    body = text[end + 4:].strip()
    fm = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def extract_steps(body: str) -> list[tuple[int, str]]:
    """Return list of (step_number, step_text) from the steps: block."""
    # Match checkbox lines: "  - [ ] N. text" or "  - [x] N. text"
    pattern = re.compile(r"^\s*-\s*\[[ xX]\]\s*(\d+)\.\s*(.+)$", re.MULTILINE)
    return [(int(m.group(1)), m.group(2).strip()) for m in pattern.finditer(body)]


def extract_done_when(body: str) -> str:
    """Pull the top-level done_when from frontmatter (already parsed) or inline."""
    # Inline: look for done_when in the body as a fallback
    m = re.search(r"done_when:\s*(.+)", body)
    return m.group(1).strip() if m else ""


def extract_rulings(body: str) -> str:
    """Extract the rulings: block content if present."""
    m = re.search(r"^rulings:\s*\n((?:  .+\n?)*)", body, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip()


def slug(path: str) -> str:
    name = Path(path).stem  # e.g. _note_project_foo
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if "--help" in sys.argv or "-h" in sys.argv else 1)

    note_path = sys.argv[1]
    try:
        step_n = int(sys.argv[2])
    except ValueError:
        sys.stderr.write(f"[task_brief] step must be an integer, got: {sys.argv[2]}\n")
        sys.exit(1)

    try:
        text = Path(note_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.stderr.write(f"[task_brief] note not found: {note_path}\n")
        sys.exit(1)

    fm, body = parse_frontmatter(text)
    steps = extract_steps(body)

    step_map = dict(steps)
    if step_n not in step_map:
        available = sorted(step_map.keys())
        sys.stderr.write(f"[task_brief] step {step_n} not found. Available: {available}\n")
        sys.exit(1)

    target_text = step_map[step_n]
    prev_text = step_map.get(step_n - 1, "")
    done_when_top = fm.get("done_when", "")
    rulings = extract_rulings(body)

    # Extract per-step done_when if present in the step line (-- done_when: ...)
    step_done_when = ""
    m = re.search(r"--\s*done_when:\s*(.+)", target_text)
    if m:
        step_done_when = m.group(1).strip()
        target_text = target_text[:m.start()].strip()

    # Build the brief
    lines = [
        f"# Task Brief — Step {step_n}",
        f"**Source note:** `{note_path}`",
        f"**Project:** {fm.get('# ', Path(note_path).stem)}",
        "",
        "## Your task",
        f"{target_text}",
        "",
    ]

    if step_done_when:
        lines += [f"**Done when:** `{step_done_when}`", ""]

    if prev_text:
        prev_clean = re.sub(r"\s*--\s*done_when:.+", "", prev_text).strip()
        lines += [
            "## Context from previous step",
            f"Step {step_n - 1} completed: {prev_clean}",
            "",
        ]

    if rulings:
        lines += [
            "## Active rulings (decisions already made — do not re-litigate)",
            rulings,
            "",
        ]

    if done_when_top:
        lines += [
            "## Project completion criteria (for context only)",
            f"`{done_when_top}`",
            "",
        ]

    lines += [
        "---",
        "**Constraints:** Do only this step. Do not read or modify other steps.",
        "Report back: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED + one-line summary.",
    ]

    brief_text = "\n".join(lines)

    out_path = Path(f"/tmp/task-brief-{slug(note_path)}-step{step_n}.md")
    out_path.write_text(brief_text, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
