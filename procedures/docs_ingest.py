#!/usr/bin/env python3
"""
docs_ingest.py — Structure-aware document ingestion for long-form docs.

Ingests markdown files from workspace/docs/ into kb/vector_db_docs/ (ChromaDB).
Designed for very long documents (legal, UU, books, contracts, manuals).

Chunking strategy:
  1. Split per heading (# / ## / ###) — one section = one chunk
  2. If section > MAX_SECTION_CHARS → split per paragraph (double newline)
  3. If paragraph > MAX_PARA_CHARS → split per sentence

Reuses patterns from ingest_docs_data.py:
  - Hash-based incremental ingestion (skip unchanged files)
  - enrich_chunk() — inject metadata prefix into chunk content
  - YAML frontmatter support
  - ChromaDB + HuggingFace embeddings

Usage:
  python3 procedures/docs_ingest.py              # ingest all docs/
  python3 procedures/docs_ingest.py --file <path> # ingest single file
  python3 procedures/docs_ingest.py --dry-run     # show what would be ingested
"""

import json
import os
import re
import subprocess
import sys
import yaml
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional

import pandas as pd
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
DOCS_DIR = WORKSPACE / "docs"
DB_PATH = str(WORKSPACE / "kb/vector_db_docs")
DOCS_ENTITY_GRAPH = WORKSPACE / "kb" / "docs_entity_graph.json"
LOG_FILE = WORKSPACE / "logs/docs_ingest.log"

# Resolve the local model snapshot dynamically (see docs_search.py). Must match
# what docs_search uses, or ingested vectors won't align with query vectors.
# Single source of truth via env (fallback const). Must match docs_search.py.
_MODEL_ID = os.environ.get("DINOMEM_EMBED_MODEL", "intfloat/multilingual-e5-small")
_hf_cache = Path(
    os.environ.get("HF_HOME")
    or (Path.home() / ".cache" / "huggingface")
)
_model_root = _hf_cache / "hub" / ("models--" + _MODEL_ID.replace("/", "--")) / "snapshots"
try:
    _snaps = sorted(p for p in _model_root.iterdir() if p.is_dir()) if _model_root.exists() else []
    LOCAL_MODEL_PATH = str(_snaps[-1]) if _snaps else _MODEL_ID
except Exception:
    LOCAL_MODEL_PATH = _MODEL_ID
_LOCAL_MODEL_RESOLVED = LOCAL_MODEL_PATH != _MODEL_ID

# e5 asymmetric prefixing (query:/passage:) — must match docs_search.py so ingest
# (passage) and query vectors align. Disable via DINOMEM_EMBED_PREFIX=0.
_PREFIX_ENABLED = os.environ.get("DINOMEM_EMBED_PREFIX", "1") != "0"
class _PrefixedHFE(HuggingFaceEmbeddings):
    def embed_documents(self, texts):
        if _PREFIX_ENABLED:
            texts = ["passage: " + t for t in texts]
        return super().embed_documents(texts)
    def embed_query(self, text):
        if _PREFIX_ENABLED:
            text = "query: " + text
        return super().embed_query(text)

MAX_SECTION_CHARS = 2000   # split section into paragraphs if larger
MAX_PARA_CHARS    = 1500   # split paragraph into sentences if larger
MIN_CHUNK_CHARS   = 80     # discard chunks smaller than this

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(LOG_FILE, 'a', encoding='utf-8')

def log(msg: str):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    _log_fh.write(line + '\n')
    _log_fh.flush()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()

def load_frontmatter(content: str) -> Tuple[dict, str]:
    """Parse optional YAML frontmatter. Returns (metadata, body)."""
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1])
                return (meta if isinstance(meta, dict) else {}), parts[2].strip()
            except yaml.YAMLError:
                pass
    return {}, content

def auto_title(path: Path, content: str) -> str:
    m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return path.stem.replace('_', ' ').replace('-', ' ').title()

# ── Structure-aware chunker ───────────────────────────────────────────────────
def split_sentences(text: str) -> List[str]:
    """Naive sentence splitter on '. ', '! ', '? '."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]

def split_by_paragraphs(text: str, max_chars: int) -> List[str]:
    """Split text by double newline; further split large paragraphs by sentence."""
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    chunks = []
    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            # Split by sentence
            sentences = split_sentences(para)
            current = ''
            for sent in sentences:
                if len(current) + len(sent) + 1 <= max_chars:
                    current = (current + ' ' + sent).strip()
                else:
                    if current:
                        chunks.append(current)
                    current = sent
            if current:
                chunks.append(current)
    return chunks

def heading_level(line: str) -> Optional[int]:
    """Return heading level (1-3) or None."""
    m = re.match(r'^(#{1,3})\s+', line)
    return len(m.group(1)) if m else None

def structure_chunk(content: str) -> List[Tuple[str, str, int]]:
    """
    Split document into structure-aware chunks.

    Returns list of (chunk_text, section_title, section_level).
    Strategy:
      - Split on headings (h1/h2/h3)
      - Sections > MAX_SECTION_CHARS → split by paragraph
      - Paragraphs > MAX_PARA_CHARS → split by sentence
    """
    lines = content.split('\n')
    sections: List[Tuple[str, str, int]] = []  # (body, title, level)

    current_title = ''
    current_level = 0
    current_lines: List[str] = []

    def flush():
        body = '\n'.join(current_lines).strip()
        if body and len(body) >= MIN_CHUNK_CHARS:
            sections.append((body, current_title, current_level))

    for line in lines:
        lvl = heading_level(line)
        if lvl is not None:
            flush()
            current_title = line.lstrip('#').strip()
            current_level = lvl
            current_lines = []
        else:
            current_lines.append(line)

    flush()

    # Now expand sections that are too large
    result: List[Tuple[str, str, int]] = []
    for body, title, level in sections:
        if len(body) <= MAX_SECTION_CHARS:
            result.append((body, title, level))
        else:
            sub_chunks = split_by_paragraphs(body, MAX_PARA_CHARS)
            for i, sub in enumerate(sub_chunks):
                if len(sub) >= MIN_CHUNK_CHARS:
                    sub_title = f"{title} (part {i+1})" if len(sub_chunks) > 1 else title
                    result.append((sub, sub_title, level))

    return result

# ── Metadata + enrichment ─────────────────────────────────────────────────────
def build_doc_metadata(path: Path, frontmatter: dict, content: str) -> dict:
    meta = dict(frontmatter)
    meta.setdefault('title', auto_title(path, content))
    meta.setdefault('doc_type', 'document')
    meta.setdefault('date', datetime.now().strftime('%Y-%m-%d'))
    meta['source_file'] = str(path)
    meta['file_hash'] = get_file_hash(path)
    # Numeric date for Chroma $gte filtering
    try:
        dt = datetime.strptime(str(meta['date']), '%Y-%m-%d')
        meta['date_int'] = int(dt.strftime('%Y%m%d'))
    except Exception:
        meta['date_int'] = 0
    return meta

def make_chunk_doc(chunk_text: str, section_title: str, section_level: int,
                   chunk_idx: int, doc_meta: dict) -> Document:
    """Build a LangChain Document for one chunk with enriched content."""
    meta = dict(doc_meta)
    meta['section_title'] = section_title
    meta['section_level'] = section_level
    meta['chunk_index'] = chunk_idx

    # Enrich chunk content with metadata prefix (improves retrieval quality)
    parts = []
    if doc_meta.get('title'):
        parts.append(f"Document: {doc_meta['title']}")
    if doc_meta.get('doc_type') and doc_meta['doc_type'] != 'document':
        parts.append(f"Type: {doc_meta['doc_type']}")
    if doc_meta.get('date'):
        parts.append(f"Date: {doc_meta['date']}")
    if section_title:
        parts.append(f"Section: {section_title}")

    prefix = ' | '.join(parts)
    enriched = f"{prefix}\n\n{chunk_text}" if prefix else chunk_text

    return Document(page_content=enriched, metadata=meta)

# ── LLM helpers (reuse extract_memory.py pattern) ───────────────────────────
def _get_openclaw_model():
    """Read default model from openclaw.json."""
    cfg = Path.home() / ".openclaw" / "openclaw.json"
    if cfg.exists():
        try:
            with open(cfg) as f:
                c = json.load(f)
            m = c.get('agents', {}).get('defaults', {}).get('model', {}).get('primary')
            if m:
                return m
        except Exception:
            pass
    return None

_NO_REASONING_MODEL = os.environ.get(
    "DINOMEM_NO_REASONING_MODEL", "ninerouter/gc/gemini-2.5-flash"
)

def _call_llm(prompt: str, max_tokens: int = 1500, model: str = None) -> Tuple[bool, str]:
    """Call LLM via OpenClaw gateway (cheap model by default)."""
    import shutil as _shutil
    _oc = _shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
    _model = model or _NO_REASONING_MODEL
    _cmd = [_oc, "capability", "model", "run",
            "--prompt", prompt, "--gateway", "--json",
            "--model", _model]
    try:
        result = subprocess.run(_cmd, capture_output=True, text=True, timeout=90)
        if result.returncode == 0:
            out = json.loads(result.stdout)
            if out.get("ok") and out.get("outputs"):
                text = out["outputs"][0].get("text", "")
                if text:
                    return True, text
        log(f"  ⚠️  LLM call failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("  ⚠️  LLM call timed out")
    except Exception as e:
        log(f"  ⚠️  LLM call error: {e}")
    return False, ""


# ── Entity + relation extraction ──────────────────────────────────────────────
_ENTITY_PROMPT_TMPL = """\
You are an entity and relation extractor for a document knowledge graph.

Document: {doc_title}
Section: {section_title}

Text:
{chunk_text}

Extract:
1. Named entities (people, organizations, tools, concepts, projects, laws, places).
2. Relations between entities in this text.

Respond ONLY with valid JSON (no markdown fences, no explanation):
{{
  "entities": [
    {{"name": "<entity name>", "type": "person|org|tool|concept|project|law|place|other"}}
  ],
  "relations": [
    {{"subject": "<entity name>", "predicate": "<verb phrase>", "object": "<entity name>"}}
  ]
}}

Rules:
- Only include entities clearly present in the text.
- Normalize names (title case, no trailing punctuation).
- Relations must use entity names from the entities list.
- If no entities found, return {{"entities": [], "relations": []}}.
"""

def extract_entities_relations(chunk_text: str, doc_title: str,
                               section_title: str) -> Dict:
    """Call LLM to extract entities + relations from one chunk.
    Returns {entities: [...], relations: [...]} or empty on failure."""
    prompt = _ENTITY_PROMPT_TMPL.format(
        doc_title=doc_title,
        section_title=section_title or "(no section)",
        chunk_text=chunk_text[:3000],  # cap to avoid token overflow
    )
    ok, text = _call_llm(prompt, max_tokens=800)
    if not ok or not text.strip():
        return {"entities": [], "relations": []}
    # Strip markdown fences if model adds them
    text = re.sub(r'^```[a-z]*\n?', '', text.strip())
    text = re.sub(r'\n?```$', '', text.strip())
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data.setdefault("entities", [])
            data.setdefault("relations", [])
            return data
    except json.JSONDecodeError:
        log(f"  ⚠️  Entity extraction JSON parse failed for section '{section_title}'")
    return {"entities": [], "relations": []}


# ── Graph persistence ─────────────────────────────────────────────────────────
def load_entity_graph() -> Dict:
    """Load existing docs_entity_graph.json or return empty structure."""
    if DOCS_ENTITY_GRAPH.exists():
        try:
            with open(DOCS_ENTITY_GRAPH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"nodes": [], "edges": [], "chunk_entity_map": {}}


def save_entity_graph(graph: Dict):
    """Persist graph to kb/docs_entity_graph.json."""
    DOCS_ENTITY_GRAPH.parent.mkdir(parents=True, exist_ok=True)
    with open(DOCS_ENTITY_GRAPH, 'w') as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)


def merge_into_graph(graph: Dict, doc_title: str, source_file: str,
                     chunk_idx: int, section_title: str,
                     extracted: Dict) -> Dict:
    """Merge extracted entities + relations into the graph dict in-place."""
    chunk_key = f"{source_file}::{chunk_idx}"

    # Build entity name → node index map (existing)
    name_to_idx: Dict[str, int] = {}
    for i, node in enumerate(graph["nodes"]):
        if node.get("type") == "entity":
            name_to_idx[node["name"].lower()] = i

    # Upsert entity nodes
    chunk_entity_names = []
    for ent in extracted.get("entities", []):
        name = ent.get("name", "").strip()
        etype = ent.get("type", "other")
        if not name:
            continue
        key = name.lower()
        if key not in name_to_idx:
            idx = len(graph["nodes"])
            graph["nodes"].append({
                "type": "entity",
                "name": name,
                "entity_type": etype,
                "first_seen_doc": doc_title,
                "first_seen_file": source_file,
            })
            name_to_idx[key] = idx
        else:
            # Update entity_type if currently 'other'
            node = graph["nodes"][name_to_idx[key]]
            if node.get("entity_type") == "other" and etype != "other":
                node["entity_type"] = etype
        chunk_entity_names.append(name)

    # Record which entities appear in this chunk
    graph["chunk_entity_map"][chunk_key] = {
        "doc_title": doc_title,
        "section_title": section_title,
        "entities": chunk_entity_names,
    }

    # Add entity→chunk edges
    for name in chunk_entity_names:
        key = name.lower()
        if key in name_to_idx:
            graph["edges"].append({
                "type": "entity_in_chunk",
                "entity_name": name,
                "entity_idx": name_to_idx[key],
                "chunk_key": chunk_key,
                "doc_title": doc_title,
                "section_title": section_title,
            })

    # Add relation edges
    for rel in extracted.get("relations", []):
        subj = rel.get("subject", "").strip()
        pred = rel.get("predicate", "").strip()
        obj = rel.get("object", "").strip()
        if not subj or not pred or not obj:
            continue
        a_idx = name_to_idx.get(subj.lower())
        b_idx = name_to_idx.get(obj.lower())
        if a_idx is None or b_idx is None:
            continue
        graph["edges"].append({
            "type": "relation",
            "label": pred,
            "a": a_idx,
            "b": b_idx,
            "chunk_key": chunk_key,
            "doc_title": doc_title,
        })

    return graph


# ── Load single file ──────────────────────────────────────────────────────────
# ── Format extraction (PDF/docx/etc → markdown) ────────────────────────────
import subprocess
import shutil

# Extensions read directly as text (no conversion needed)
_TEXT_EXTS = {'.md', '.markdown', '.txt', '.text'}
# Extensions routed through pandoc → markdown
_PANDOC_EXTS = {'.docx', '.doc', '.rtf', '.odt', '.html', '.htm', '.epub'}

def _slugify(name: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', name.lower()).strip('-')
    return (s or 'doc')[:60]

def _sniff_format(path: Path) -> str:
    """Detect format by extension, falling back to magic bytes.
    Returns one of: 'text', 'pdf', 'pandoc', 'unknown'.
    Inbound files (e.g. ~/.openclaw/media/inbound/<uuid>) may lack an extension."""
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        return 'text'
    if ext == '.pdf':
        return 'pdf'
    if ext in _PANDOC_EXTS:
        return 'pandoc'
    # No (or unknown) extension → sniff magic bytes
    try:
        head = path.read_bytes()[:8]
    except Exception:
        return 'unknown'
    if head[:4] == b'%PDF':
        return 'pdf'
    if head[:4] == b'PK\x03\x04':
        # zip container — docx/odt/epub/xlsx; pandoc handles the doc types
        return 'pandoc'
    if head[:5].lower() == b'<html' or head[:9].lower() == b'<!doctype':
        return 'pandoc'
    # Assume utf-8 text as a last resort (covers extension-less .md/.txt)
    return 'text'

def extract_to_markdown(src: Path) -> Path:
    """Convert a non-markdown source into a markdown file under DOCS_DIR.
    Returns the path of the markdown file to ingest. Text sources already inside
    DOCS_DIR are returned as-is (no copy). Uses system binaries only (pdftotext,
    pandoc) — no extra Python deps."""
    fmt = _sniff_format(src)

    # Plain text/markdown already living in docs/ → ingest in place.
    if fmt == 'text' and src.suffix.lower() in _TEXT_EXTS and DOCS_DIR in src.resolve().parents:
        return src

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(src.stem)
    out = DOCS_DIR / f"{slug}.md"
    # Avoid clobbering an existing different doc with the same slug.
    n = 2
    while out.exists() and out.resolve() != src.resolve():
        out = DOCS_DIR / f"{slug}-{n}.md"
        n += 1

    if fmt == 'text':
        text = src.read_text(encoding='utf-8', errors='replace')
        out.write_text(text, encoding='utf-8')
        log(f"  extracted (text): {src.name} → {out.name}")
        return out

    if fmt == 'pdf':
        if not shutil.which('pdftotext'):
            raise RuntimeError(
                "PDF ingest needs 'pdftotext' (poppler-utils), which isn't installed. "
                "Install it (Debian/Ubuntu: poppler-utils, macOS/Arch: poppler) and retry. "
                "Text/Markdown docs ingest without it.")
        r = subprocess.run(['pdftotext', '-layout', str(src), str(out)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"pdftotext failed: {r.stderr.strip()[:200]}")
        log(f"  extracted (pdf): {src.name} → {out.name}")
        return out

    if fmt == 'pandoc':
        if not shutil.which('pandoc'):
            raise RuntimeError(
                "docx/Office/HTML ingest needs 'pandoc', which isn't installed. "
                "Install it (https://pandoc.org/installing.html) and retry. "
                "Text/Markdown and PDF docs ingest without it.")
        r = subprocess.run(['pandoc', str(src), '-t', 'gfm', '-o', str(out)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"pandoc failed: {r.stderr.strip()[:200]}")
        log(f"  extracted (pandoc): {src.name} → {out.name}")
        return out

    raise RuntimeError(f"unsupported/undetectable format: {src.name}")

def load_file(path: Path) -> Tuple[List[Document], dict]:
    """Load and chunk a single document. Non-markdown sources (PDF, docx, etc.)
    are first extracted to a markdown file under docs/. Returns (chunks, doc_meta)."""
    if _sniff_format(path) != 'text' or path.suffix.lower() not in _TEXT_EXTS:
        path = extract_to_markdown(path)
    content = path.read_text(encoding='utf-8')
    frontmatter, body = load_frontmatter(content)
    doc_meta = build_doc_metadata(path, frontmatter, body)

    chunks_raw = structure_chunk(body)
    if not chunks_raw:
        log(f"  WARN: no chunks from {path.name}")
        return [], doc_meta

    docs = []
    for i, (chunk_text, section_title, section_level) in enumerate(chunks_raw):
        doc = make_chunk_doc(chunk_text, section_title, section_level, i, doc_meta)
        docs.append(doc)

    return docs, doc_meta

# ── Incremental ingestion ─────────────────────────────────────────────────────
def get_existing_hashes(db: Chroma) -> Dict[str, str]:
    """Return {source_file: file_hash} for all docs in DB."""
    existing = db.get()
    hashes: Dict[str, str] = {}
    for meta in (existing.get('metadatas') or []):
        src = meta.get('source_file', '')
        fh = meta.get('file_hash', '')
        if src and fh:
            hashes[src] = fh
    return hashes

def ingest_files(paths: List[Path], db: Chroma, existing_hashes: Dict[str, str],
                 dry_run: bool = False, skip_entity_extraction: bool = False) -> Dict[str, int]:
    stats = {'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
             'entities_extracted': 0}

    # Load existing entity graph once; merge incrementally
    graph = load_entity_graph() if not dry_run else None

    for path in paths:
        try:
            current_hash = get_file_hash(path)
            src_key = str(path)
            stored_hash = existing_hashes.get(src_key)

            if stored_hash == current_hash:
                stats['skipped'] += 1
                continue

            is_update = stored_hash is not None
            action = 'UPDATE' if is_update else 'NEW'

            chunks, doc_meta = load_file(path)
            if not chunks:
                stats['errors'] += 1
                continue

            log(f"  {action}: {path.name} → {len(chunks)} chunks")

            if not dry_run:
                if is_update:
                    db.delete(where={"source_file": src_key})
                db.add_documents(chunks)

                # ── Entity + relation extraction ──────────────────────────
                if not skip_entity_extraction:
                    doc_title = doc_meta.get('title', path.stem)
                    log(f"  Extracting entities from {len(chunks)} chunks ({doc_title})...")
                    for chunk_doc in chunks:
                        chunk_idx = chunk_doc.metadata.get('chunk_index', 0)
                        section_title = chunk_doc.metadata.get('section_title', '')
                        # Strip the metadata prefix from enriched content
                        raw_text = chunk_doc.page_content
                        # Content is: "prefix\n\nchunk_text" — extract chunk_text
                        if '\n\n' in raw_text:
                            raw_text = raw_text.split('\n\n', 1)[1]
                        extracted = extract_entities_relations(
                            raw_text, doc_title, section_title
                        )
                        n_ents = len(extracted.get('entities', []))
                        if n_ents:
                            stats['entities_extracted'] += n_ents
                            merge_into_graph(
                                graph, doc_title, src_key,
                                chunk_idx, section_title, extracted
                            )

            stats['updated' if is_update else 'added'] += 1

        except Exception as e:
            log(f"  ERROR: {path.name}: {e}")
            stats['errors'] += 1

    # Persist updated graph
    if not dry_run and graph is not None:
        save_entity_graph(graph)
        log(f"  Entity graph saved → {DOCS_ENTITY_GRAPH} "
            f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")

    return stats

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Ingest long-form docs into vector DB')
    parser.add_argument('--file', type=Path, help='Ingest single file')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be ingested, write nothing')
    parser.add_argument('--skip-entity-extraction', action='store_true',
                        help='Skip LLM entity/relation extraction (faster, no graph update)')
    args = parser.parse_args()

    log("=" * 60)
    log("docs_ingest.py starting")
    if args.dry_run:
        log("DRY RUN — no writes")

    # Init embeddings
    log("Loading embeddings model...")
    embeddings = _PrefixedHFE(
        model_name=LOCAL_MODEL_PATH,
        model_kwargs={'device': 'cpu', 'local_files_only': _LOCAL_MODEL_RESOLVED},
        encode_kwargs={'normalize_embeddings': True}
    )

    # Init DB
    Path(DB_PATH).mkdir(parents=True, exist_ok=True)
    db = Chroma(
        persist_directory=DB_PATH,
        embedding_function=embeddings,
        collection_metadata={"description": "Long-form document store (structure-aware chunking)"}
    )

    existing_hashes = get_existing_hashes(db)
    log(f"Existing DB: {len(existing_hashes)} source files")

    # Collect files to process
    if args.file:
        if not args.file.exists():
            log(f"ERROR: file not found: {args.file}")
            sys.exit(1)
        paths = [args.file]
    else:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        paths = sorted(DOCS_DIR.glob("**/*.md"))
        if not paths:
            log(f"No .md files found in {DOCS_DIR}")
            sys.exit(0)

    log(f"Files to check: {len(paths)}")

    stats = ingest_files(paths, db, existing_hashes, dry_run=args.dry_run,
                         skip_entity_extraction=args.skip_entity_extraction)

    log("=" * 60)
    log(f"Done — added: {stats['added']} | updated: {stats['updated']} | skipped: {stats['skipped']} | errors: {stats['errors']}")
    if not args.dry_run and not args.skip_entity_extraction:
        log(f"Entities extracted: {stats.get('entities_extracted', 0)} | Graph: {DOCS_ENTITY_GRAPH}")
    log(f"DB: {DB_PATH}")

if __name__ == '__main__':
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
    main()
