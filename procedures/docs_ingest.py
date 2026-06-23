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

import os
import re
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
LOG_FILE = WORKSPACE / "logs/docs_ingest.log"

# Resolve the local model snapshot dynamically (see docs_search.py). Must match
# what docs_search uses, or ingested vectors won't align with query vectors.
_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
_hf_cache = Path(
    os.environ.get("HF_HOME")
    or (Path.home() / ".cache" / "huggingface")
)
_model_root = _hf_cache / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots"
try:
    _snaps = sorted(p for p in _model_root.iterdir() if p.is_dir()) if _model_root.exists() else []
    LOCAL_MODEL_PATH = str(_snaps[-1]) if _snaps else _MODEL_ID
except Exception:
    LOCAL_MODEL_PATH = _MODEL_ID
_LOCAL_MODEL_RESOLVED = LOCAL_MODEL_PATH != _MODEL_ID

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

# ── Load single file ──────────────────────────────────────────────────────────
def load_file(path: Path) -> Tuple[List[Document], dict]:
    """Load and chunk a single markdown file. Returns (chunks, doc_meta)."""
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
                 dry_run: bool = False) -> Dict[str, int]:
    stats = {'added': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

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

            stats['updated' if is_update else 'added'] += 1

        except Exception as e:
            log(f"  ERROR: {path.name}: {e}")
            stats['errors'] += 1

    return stats

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Ingest long-form docs into vector DB')
    parser.add_argument('--file', type=Path, help='Ingest single file')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be ingested, write nothing')
    args = parser.parse_args()

    log("=" * 60)
    log("docs_ingest.py starting")
    if args.dry_run:
        log("DRY RUN — no writes")

    # Init embeddings
    log("Loading embeddings model...")
    embeddings = HuggingFaceEmbeddings(
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

    stats = ingest_files(paths, db, existing_hashes, dry_run=args.dry_run)

    log("=" * 60)
    log(f"Done — added: {stats['added']} | updated: {stats['updated']} | skipped: {stats['skipped']} | errors: {stats['errors']}")
    log(f"DB: {DB_PATH}")

if __name__ == '__main__':
    main()
