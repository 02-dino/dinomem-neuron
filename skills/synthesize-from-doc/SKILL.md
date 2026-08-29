---
name: synthesize-from-doc
description: Answer broad, synthesis-style tasks over one OR MORE ingested long documents ("buatkan/make/summarize/analyze/plan X based on <book>", "combine all my books", "compare book X vs Y on Z", or scenario questions "according to this book, what should I do if XYZ") by fanning out multiple sub-queries to docs_search, merging (or, for comparisons, bucketing per document) the retrieved chunks, and synthesizing a grounded, cited answer — with domain-aware guardrails. Read this when a user gives a WIDE task, a multi-document task, or a scenario question over document(s), not a single narrow lookup — especially for self-help, legal, or medical books. Turns a noob's one-line request into an even, well-grounded, correctly-attributed answer without asking them to break it into steps.
---

# Synthesize from a document (dinomem-neuron)

`docs_search` (see the `retrieval-routing` skill + `TOOLS.md`) retrieves the
**top-k most relevant chunks** for ONE query. That is perfect for a narrow lookup
("what does the book say about compounding?") but **too shallow for a WIDE task**
("make a financial plan based on this book"). One query returns ~5-10 chunks, so a
20-chapter book gets answered from a sliver of itself → a lopsided answer that
*looks* complete but silently skips most of the source.

This skill closes that gap. The document is already fully indexed; the fix is in
**how the agent queries its own index** (fan-out) and **when it frames vs brakes**
(guardrails). It changes NOTHING in ingestion or the index — it is a thin
orchestration + guardrail layer on top of `docs_search`.

## When to use this skill

Read + run this skill when BOTH are true:

1. The task is over **a specific ingested document** (user names a book/doc, or a
   doc was just uploaded and the task is clearly about it), AND
2. It is a **WIDE / synthesis / scenario** request, not a single narrow lookup.

Trigger phrasings (any language — match intent, not keywords):
- "buatkan/bikin rencana … based on \<book\>" · "make/build a plan from this book"
- "rangkum/summarize the whole book" · "analyze what this book teaches about …"
- "menurut buku ini, apa yang harus dilakukan kalau XYZ" · "according to this book,
  what should I do if XYZ" (scenario question — see Attributed-conditional below)

**Do NOT use this skill** for a single narrow lookup ("what's the definition of X",
"quote Pasal 340") — that is a plain `docs_search` call. This skill is for breadth.

## When the user names NO document (auto-consult + source honesty)

Users often upload docs then ask a plain question *without naming a book*, expecting
you to answer "from my library" — e.g. they uploaded finance books, then just ask
*"how should I manage my money?"*. **Uploading docs does NOT make the model itself
smarter** — the docs live in a separate index (`kb/vector_db_docs`), not in the
model's weights. If you answer from general knowledge while the user believes it
came from their books, that is a **silent misattribution** — the failure this
section prevents.

So when the docs corpus is non-empty AND the user asks a **substantive, topical
question** that their uploaded material could plausibly answer (not a greeting,
not chit-chat, not an unrelated task):

1. **Auto-consult first.** Silently run a `docs_search` probe on the question
   before answering — no need to be told which book. (`python3
   tools/docs_search.py "<the user's question>" --k 5`.)
2. **Hit** (relevant chunks) → treat it as a doc-grounded task: enter this skill's
   pipeline (fan out, merge, synthesize) and **cite the source(s)**.
3. **Miss** (nothing relevant) → answer from general knowledge, but **flag it**:
   *"this isn't from your uploaded documents — general knowledge."* Never let the
   user assume otherwise.

**Source-honesty rule (always):** every answer must make clear whether it is
**grounded in the user's documents** (with citations) or **general knowledge**.
Never blur the two. And do not over-promise: you can *consult* the whole library
per question (top-k relevant across all docs), but you cannot fuse "all N books"
into one standing wisdom — retrieval is per-query and relevance-bounded, not a
permanent merge into the model.

**Scope discipline (avoid waste):** only auto-consult on substantive/topical
queries. Do NOT fire a probe on greetings, acknowledgements, or clearly unrelated
requests — that just burns retrieval for nothing.

## The pipeline

### Step 0 — How many documents, and which multi-doc MODE?
Run `python3 tools/docs_search.py --list` to see the indexed corpus. Decide the
task's document scope:

| Scope | Trigger | Retrieval strategy |
| --- | --- | --- |
| **single** | task names/targets ONE doc | fan-out with `--doc "<title>"` (scoped) |
| **pooled** | "from ALL my books", "combine my finance books", no single doc named | fan-out with **NO `--doc`** → whole corpus; keep per-chunk source for attribution |
| **comparative** | "**compare** X vs Y", "what does each book say about Z", "which book …" | fan-out **once per document** (`--doc X`, then `--doc Y`, …); keep each doc's chunks in a SEPARATE bucket |

Critical distinctions:
- **comparative ≠ pooled.** If the user wants a comparison but you pool, you lose
  *which book said what* → mush. If they want one merged plan but you force
  per-book, you over-complicate. Read the intent.
- **`docs_search --doc` is a single-substring filter** — it CANNOT OR-match
  several titles at once. So multi-doc = either drop `--doc` (pooled) or loop it
  once per doc (comparative). There is no `--doc "a,b"`.
- **Attribution survives pooling** because every chunk carries `metadata.doc_title`
  + `metadata.source_file`. In pooled/comparative modes you MUST track each
  chunk's source so the synthesis can still say *"Book X says…"* accurately —
  never blur two books into one unattributed claim.

### Step 1 — Identify the document(s) + classify the domain
- Resolve which indexed doc(s) the task targets (from Step 0). If a single-doc
  task is ambiguous about *which* doc, ask the user one short question.
- Classify each target doc's domain into ONE of:
  **`self-help` · `legal` · `medical` · `general`**. This picks the fan-out
  strategy AND the guardrail tier below. When unsure between general and a
  high-stakes domain (legal/medical), **choose the stricter one**. In a mixed
  corpus (e.g. a medical book + a self-help book), apply the **strictest tier
  present** to the whole answer.

### Step 2 — Fan out into sub-queries (the core fix)
Decompose the wide task into **6-10 focused sub-queries** yourself — do NOT ask
the user to do this. Each sub-query targets a distinct facet the answer needs.

- *self-help / general*: one sub-query per major theme/principle
  (e.g. for a money book: saving, compounding, risk & luck, room for error,
  behavior over knowledge, long-term horizon, …).
- *legal / medical* (**domain-aware fan-out**): for each relevant topic, also
  query for **its exceptions / cross-references / contraindications / general
  provisions** — not just the main rule. This is what fights the silent gap
  (the missed exception that makes a confident answer wrong).

Run each sub-query, matching the Step-0 mode:
```
# single:      scope to the one doc
python3 tools/docs_search.py "<sub-query>" --doc "<doc-title>" --k 5
# pooled:      whole corpus, no --doc (attribution kept via chunk metadata)
python3 tools/docs_search.py "<sub-query>" --k 8
# comparative: loop the SAME sub-query once per doc, into separate buckets
python3 tools/docs_search.py "<sub-query>" --doc "<doc-A>" --k 5
python3 tools/docs_search.py "<sub-query>" --doc "<doc-B>" --k 5
```
(`--k` per sub-query; bump it a bit for pooled so each theme still surfaces
across a bigger corpus. Use `--section` to bias toward a known chapter.)

### Step 3 — Merge + dedup the retrieved chunks
Dedup by `source_file` + `chunk_index` (both in each result's metadata).
- **single / pooled:** merge into ONE pool — but keep each chunk's `doc_title`
  so attribution survives (esp. pooled, where books are mixed).
- **comparative:** keep **one pool per document** (do NOT merge across books) —
  the whole point is contrasting them side by side.

You now "see" the source(s) **evenly across structure**, not just the top-5 of a
single query.

### Step 4 — Synthesize, with every claim grounded
Write the answer from the retrieved chunks. **Each substantive point must cite its
source** — for multi-doc, cite the **book/document title too** (`metadata.doc_title`),
plus chapter/section (`metadata.section_title`) and, for legal, the article/pasal.
- **pooled:** synthesize one merged answer, but attribute each claim to the book
  it came from ("Book A on saving …; Book B adds …").
- **comparative:** structure the answer as a contrast — per theme, what each book
  says, then where they agree/differ. Never collapse two books into one voice.

- If the pooled chunks don't cover part of the task → say **"not covered in the
  document"**. Never fill the gap from general knowledge and present it as the
  book's content.
- Prefer the book's own framing/wording over generic advice. The whole value is
  that the answer is traceable back to *this* document.

### Step 5 — Apply the domain guardrail (see below) before sending.

## Guardrail: attributed-conditional answering (the core rule)

The line is **NOT** "scenario questions are forbidden". The line is
**attribution + finality**:

| Request shape | Allowed? |
| --- | --- |
| "**According to this book**, what does it advise for situation XYZ?" → report what the book says, framed *"the book says…"*, with citations | ✅ **answer** |
| "I have condition X, what should I do?" → asks the agent to be the final authority for the asker's **real, personal** situation (data not in the book) | ⛔ **brake + redirect** |

So: **answer scenario questions** as long as the output is (a) **attributed** to
the book ("the book says…", not "you should…"), (b) **grounded** (every claim
cited), and (c) **not claiming finality** for the asker's real personal case.
The brake only fires when the task needs **personal data the book cannot have**
(the asker's own diagnosis, dosage, exam, case facts).

## Domain tiers (how far to go)

| Domain | May go up to | Brake |
| --- | --- | --- |
| `self-help` / `general` | full synthesis (plans, summaries, analysis) | light — normal grounding |
| `legal` | lookup + explanation + comparing provisions + **attributed** scenario answers | cite the article/pasal for every claim; **no finality for a real case**; append: *"based on the uploaded document, not legal advice — for a real decision, consult a lawyer."* |
| `medical` | education + **attributed** scenario answers ("the book says the handling for XYZ is…") | cite every claim; **brake on personal data** (diagnosis/dosage/triage for the asker); append: *"educational, from the document — not medical advice; for your own health decisions, consult a doctor."* |

For `legal` and `medical`, **grounding is mandatory** (no un-cited claims) and
**extrapolation to the asker's real case is forbidden** — you report what the
document says; you do not become the deciding authority.

## Output shape
- Lead with the synthesized answer (the plan / summary / scenario response).
- Attach citations inline or as a short source list (doc › section).
- For legal/medical, include the one-line disclaimer from the table above.
- If you braked on any part, say so plainly and point to the right professional —
  don't silently drop it.

## Why this stays portable
This skill is **pure procedure** — it only calls `tools/docs_search.py`
(workspace-relative, the same path every dinomem-neuron install uses) and reads
metadata fields `docs_search` already returns. No hardcoded absolute paths, no
machine-specific assumptions. It ships in `skills/synthesize-from-doc/` and the
neuron installer auto-copies + allowlists it like every other neuron skill.
