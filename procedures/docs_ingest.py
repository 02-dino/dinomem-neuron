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
from langchain_core.documents import Document

# Shared embedding resolver + e5 query:/passage: prefixing (single source of
# truth: tools/_embed.py). Migrated 2026-07-09 (L1) off the per-file _PrefixedHFE
# + local snapshot-resolution dupes so ingest (passage) and query vectors always
# align to one model/prefix definition. tools/ is a sibling of procedures/.
import sys as _sys_embed
_sys_embed.path.insert(0, str(Path(__file__).parent.parent / "tools"))
try:
    from tools._embed import get_embeddings, model_id, EMBED_MODEL_META_KEY
except ImportError:
    from _embed import get_embeddings, model_id, EMBED_MODEL_META_KEY

# ── Config ────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent.parent
DOCS_DIR = WORKSPACE / "docs"
DB_PATH = str(WORKSPACE / "kb/vector_db_docs")
DOCS_ENTITY_GRAPH = WORKSPACE / "kb" / "docs_entity_graph.json"
LOG_FILE = WORKSPACE / "logs/docs_ingest.log"


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

# Standard cheap-model tag is DINOMEM_CHEAP_MODEL (matches the rest of dinomem);
# legacy DINOMEM_NO_REASONING_MODEL kept as back-compat fallback. Unset = OpenClaw
# default (default-safe — no hardcoded gemini that 404s on buyers without it).
_NO_REASONING_MODEL = (
    os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
    or os.environ.get("DINOMEM_NO_REASONING_MODEL", "").strip()
    or None
)

def _call_llm(prompt: str, max_tokens: int = 1500, model: str = None) -> Tuple[bool, str]:
    """Call LLM via OpenClaw gateway (cheap model when set, else OpenClaw default)."""
    import shutil as _shutil
    _oc = _shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
    _model = model or _NO_REASONING_MODEL
    _cmd = [_oc, "capability", "model", "run",
            "--prompt", prompt, "--gateway", "--json"]
    if _model:
        _cmd += ["--model", _model]
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


# ── Vision OCR (reuses the SAME gateway model-run endpoint as _call_llm) ───────
# No GPU, no vLLM, no extra Python dep. A user installs dinomem-neuron and shares
# an image or scanned PDF — this reads it via the configured vision model.
def _ocr_image(image_path: Path, model: str = None) -> Tuple[bool, str]:
    """Transcribe text/tables/charts from an image via the OpenClaw gateway
    vision endpoint (openclaw capability model run --file). Returns (ok, text)."""
    import shutil as _shutil
    _oc = _shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
    _model = model or _NO_REASONING_MODEL
    _cmd = [_oc, "capability", "model", "run",
            "--file", str(image_path),
            "--prompt", _OCR_PROMPT, "--gateway", "--json"]
    if _model:
        _cmd += ["--model", _model]
    try:
        result = subprocess.run(_cmd, capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            out = json.loads(result.stdout)
            if out.get("ok") and out.get("outputs"):
                text = out["outputs"][0].get("text", "").strip()
                if text:
                    return True, text
        log(f"  ⚠️  OCR call failed: {(result.stderr or '')[:200]}")
    except subprocess.TimeoutExpired:
        log("  ⚠️  OCR call timed out")
    except Exception as e:
        log(f"  ⚠️  OCR call error: {e}")
    return False, ""


def _ocr_pdf_pages(src: Path, pages: List[int], model: str = None) -> str:
    """Rasterize the given PDF page numbers to PNG (pdftoppm) and OCR each.
    Returns concatenated markdown, one block per page. Pages that fail OCR are
    skipped silently (best-effort augmentation, never blocks ingest)."""
    import shutil as _shutil, tempfile
    if not _shutil.which("pdftoppm"):
        log("  ⚠️  pdftoppm not found — cannot OCR scanned PDF pages")
        return ""
    blocks = []
    with tempfile.TemporaryDirectory(prefix="ocr_pdf_") as td:
        for p in pages:
            base = os.path.join(td, f"page_{p}")
            r = subprocess.run(
                ["pdftoppm", "-png", "-r", "150", "-f", str(p), "-l", str(p),
                 str(src), base],
                capture_output=True, text=True, timeout=120)
            png = None
            for cand in sorted(Path(td).glob(f"page_{p}*.png")):
                png = cand
                break
            if r.returncode != 0 or png is None:
                log(f"  ⚠️  rasterize failed for page {p}")
                continue
            ok, text = _ocr_image(png, model=model)
            if ok and text:
                blocks.append(f"\n\n## (page {p} — OCR)\n\n{text}")
                log(f"  🖼️  OCR'd scanned/mixed page {p} ({len(text)} chars)")
    return "".join(blocks)


# ── Audio/video transcription (subtitle-first ladder → key-gated → local) ───────
def _extract_audio(src: Path, out_wav: Path) -> bool:
    """ffmpeg: extract/normalize any audio or video file to 16k mono wav (what
    ASR wants). Returns True on success. System ffmpeg only — no Python dep."""
    import shutil as _shutil
    if not _shutil.which("ffmpeg"):
        log("  ⚠️  ffmpeg not found — cannot process audio/video")
        return False
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
         "-vn", str(out_wav)],
        capture_output=True, text=True, timeout=900)
    return r.returncode == 0 and out_wav.exists()


def _transcribe_deepgram(wav: Path) -> str:
    """Key-gated fast path. Only used if DEEPGRAM_API_KEY is in env. Best-effort;
    returns '' on any failure so the caller falls through to local ASR."""
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        return ""
    try:
        import urllib.request
        data = wav.read_bytes()
        req = urllib.request.Request(
            "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true",
            data=data,
            headers={"Authorization": f"Token {key}",
                     "Content-Type": "audio/wav"},
            method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            j = json.loads(resp.read().decode())
        alt = j["results"]["channels"][0]["alternatives"][0]
        txt = alt.get("transcript", "").strip()
        if txt:
            log("  🎙️  transcribed via Deepgram (key-gated fast path)")
        return txt
    except Exception as e:
        log(f"  ⚠️  Deepgram path failed ({e}); falling back to local ASR")
        return ""


def _transcribe_local(wav: Path) -> str:
    """No-key/no-GPU fallback: faster-whisper (CTranslate2, no torch). LAZY,
    GUARDED import — if the running interpreter lacks faster-whisper, we log a
    one-line install hint and return '' (ingest continues for everything else,
    never crashes). ~850MB peak RAM, ~0.22x realtime on CPU as benchmarked."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        log("  ⚠️  faster-whisper not installed for this interpreter — audio/video "
            "skipped. Install: pip install faster-whisper (no GPU/torch needed).")
        return ""
    try:
        model = WhisperModel(_ASR_MODEL, device="cpu", compute_type=_ASR_COMPUTE)
        segments, info = model.transcribe(str(wav), beam_size=1)
        parts = [seg.text for seg in segments]
        txt = " ".join(p.strip() for p in parts).strip()
        if txt:
            log(f"  🎙️  transcribed locally (faster-whisper {_ASR_MODEL}, "
                f"lang={info.language}, {info.duration:.0f}s audio)")
        return txt
    except Exception as e:
        log(f"  ⚠️  local ASR failed: {e}")
        return ""


def _transcribe_media(src: Path) -> str:
    """Full transcription ladder for a LOCAL audio/video file:
      ffmpeg → wav  →  (Deepgram if key)  →  (local faster-whisper)  →  ''.
    Returns transcript text ('' if every rung unavailable — caller writes stub)."""
    import tempfile
    # Size cap so a giant upload can't wedge a noob's box.
    try:
        mb = src.stat().st_size / (1024 * 1024)
        if mb > _MEDIA_MAX_MB:
            log(f"  ⚠️  media {mb:.0f}MB exceeds cap {_MEDIA_MAX_MB}MB — skipped")
            return ""
    except Exception:
        pass
    with tempfile.TemporaryDirectory(prefix="asr_") as td:
        wav = Path(td) / "audio.wav"
        if not _extract_audio(src, wav):
            log(f"  ⚠️  audio extraction failed for {src.name}")
            return ""
        txt = _transcribe_deepgram(wav)      # rung 2: key-gated fast path
        if not txt:
            txt = _transcribe_local(wav)     # rung 3: local fallback
        return txt


def _fetch_url_subtitles(url: str, td: str) -> str:
    """Rung 1 of the URL ladder: try to pull an existing subtitle/caption track
    (manual first, else auto-captions) via yt-dlp — ZERO transcription cost.
    Returns cleaned plain text, or '' if none available. Best-effort."""
    import shutil as _shutil, glob as _glob
    if not _shutil.which("yt-dlp"):
        return ""
    base = os.path.join(td, "sub")
    subprocess.run(
        ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
         "--sub-langs", "en.*,id.*,.*", "--sub-format", "vtt/srt/best",
         "-o", base, url],
        capture_output=True, text=True, timeout=120)
    subs = sorted(_glob.glob(base + "*.vtt") + _glob.glob(base + "*.srt"))
    if not subs:
        return ""
    raw = Path(subs[0]).read_text(encoding="utf-8", errors="replace")
    # Strip WebVTT/SRT timing + tags → dedup consecutive lines → plain text.
    out_lines, seen = [], ""
    for ln in raw.splitlines():
        s = ln.strip()
        if (not s or s == "WEBVTT" or "-->" in s or s.isdigit()
                or s.startswith(("Kind:", "Language:", "NOTE"))):
            continue
        s = re.sub(r"<[^>]+>", "", s)  # inline <c> timing tags
        if s and s != seen:
            out_lines.append(s)
            seen = s
    return "\n".join(out_lines).strip()


def _transcribe_url(url: str) -> Tuple[str, str]:
    """URL transcription ladder for a stream link (YouTube/X/TikTok/Vimeo/...):
      1. subtitle/caption grab (free, no ASR)
      2. download audio via yt-dlp → Deepgram-if-key → local faster-whisper
    Returns (title, transcript). transcript '' if every rung fails.
    Reuses the SAME ffmpeg+ASR ladder as local media — no new dependency."""
    import shutil as _shutil, tempfile, glob as _glob
    if not _shutil.which("yt-dlp"):
        log("  ⚠️  yt-dlp not found — cannot ingest URL. Install yt-dlp to enable.")
        return (url, "")
    # Best-effort title for the doc heading.
    title = url
    try:
        r = subprocess.run(["yt-dlp", "--skip-download", "--print", "title", url],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            title = r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    with tempfile.TemporaryDirectory(prefix="url_") as td:
        # Rung 1: subtitles (free).
        sub = _fetch_url_subtitles(url, td)
        if sub:
            log(f"  📜  used existing captions (no ASR) for {title[:60]}")
            return (title, sub)
        # Rung 2/3: download audio, then Deepgram-if-key → local ASR.
        abase = os.path.join(td, "audio")
        dl = subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
             "-o", abase + ".%(ext)s", url],
            capture_output=True, text=True, timeout=900)
        got = sorted(_glob.glob(abase + ".*"))
        if dl.returncode != 0 or not got:
            log(f"  ⚠️  URL download failed (may need cookies/login): {title[:60]}")
            return (title, "")
        return (title, _transcribe_media(Path(got[0])))


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
# Image extensions → routed straight through vision-OCR → markdown.
# First-class ingest inputs: a user can drop a screenshot / photo / scanned page
# and it gets read + embedded automatically, no manual step, no config toggle.
_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}

# Vision OCR prompt — reused for standalone images AND scanned/mixed PDF pages.
_OCR_PROMPT = (
    "Transcribe ALL text visible in this image exactly and completely, as plain "
    "text. If it contains a table, render it as a GitHub-flavored markdown table. "
    "If it contains a chart or diagram, first transcribe any labels/legend/axis "
    "text, then add a one-line description of what the chart shows. Preserve "
    "reading order. Output ONLY the transcription/markdown, no preamble, no "
    "commentary."
)
# Extensions routed through pandoc → markdown
_PANDOC_EXTS = {'.docx', '.doc', '.rtf', '.odt', '.html', '.htm', '.epub'}

# Audio/video → transcribe (subtitle-first for URLs, else ASR) → markdown.
# Fully automatic, dependency-light: reuses ffmpeg/yt-dlp (system) + a lazy,
# GUARDED faster-whisper import as the no-key/no-GPU fallback. If nothing is
# available the branch degrades gracefully (writes a stub + logs a hint) and
# NEVER breaks the rest of ingest.
_AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus', '.aac', '.wma', '.aiff'}
_VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv', '.wmv', '.m4v', '.mpeg', '.mpg', '.3gp'}
# ZIP/archive → unpack → recurse each member back through the router.
_ARCHIVE_EXTS = {'.zip'}

# faster-whisper model + compute — noob-safe defaults (no GPU, no key, ~850MB
# peak RAM, ~0.22x realtime on CPU as benchmarked). Overridable via env for
# users who want a bigger/smaller model.
_ASR_MODEL = os.environ.get("DINOMEM_ASR_MODEL", "base")
_ASR_COMPUTE = os.environ.get("DINOMEM_ASR_COMPUTE", "int8")
# Safety caps so a giant media file can't wedge a noob's box.
_MEDIA_MAX_MB = int(os.environ.get("DINOMEM_MEDIA_MAX_MB", "500"))
_ARCHIVE_MAX_MEMBERS = int(os.environ.get("DINOMEM_ARCHIVE_MAX_MEMBERS", "200"))

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
    if ext in _IMAGE_EXTS:
        return 'image'
    if ext in _AUDIO_EXTS:
        return 'audio'
    if ext in _VIDEO_EXTS:
        return 'video'
    if ext in _ARCHIVE_EXTS:
        return 'archive'
    # No (or unknown) extension → sniff magic bytes
    try:
        head = path.read_bytes()[:12]
    except Exception:
        return 'unknown'
    if head[:4] == b'%PDF':
        return 'pdf'
    if head[:4] == b'PK\x03\x04':
        # zip container — could be a docx/odt/epub/xlsx (pandoc) OR a plain .zip
        # archive to walk. Disambiguate by extension when present; a bare
        # PK-header with a .zip (or no) extension → treat as archive to unpack.
        if ext in _ARCHIVE_EXTS or ext == '':
            return 'archive'
        return 'pandoc'
    # Media magic bytes (extension-less inbound files from media/inbound/<uuid>)
    if head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):  # MP3
        return 'audio'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':  # WAV
        return 'audio'
    if head[:4] == b'OggS':                            # Ogg (audio or video)
        return 'audio'
    if head[:4] == b'fLaC':                            # FLAC
        return 'audio'
    if head[4:8] == b'ftyp':                           # MP4/MOV/M4A container
        # ftyp brand distinguishes audio-only (M4A) from video (MP4/MOV); ffmpeg
        # handles both the same way, so route to 'video' (audio strip works too).
        brand = head[8:12] if len(head) >= 12 else b''
        if brand[:3] in (b'M4A', b'M4B'):
            return 'audio'
        return 'video'
    if head[:4] == b'\x1aE\xdf\xa3':                   # Matroska/WebM
        return 'video'
    if head[:3] == b'FLV':                             # FLV
        return 'video'
    if head[:5].lower() == b'<html' or head[:9].lower() == b'<!doctype':
        return 'pandoc'
    # Image magic bytes (extension-less inbound files, e.g. media/inbound/<uuid>)
    if head[:3] == b'\xff\xd8\xff':                     # JPEG
        return 'image'
    if head[:8] == b'\x89PNG\r\n\x1a\n':                # PNG
        return 'image'
    if head[:6] in (b'GIF87a', b'GIF89a'):             # GIF
        return 'image'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':  # WEBP
        return 'image'
    if head[:2] == b'BM':                              # BMP
        return 'image'
    if head[:4] in (b'II*\x00', b'MM\x00*'):           # TIFF
        return 'image'
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

    if fmt == 'image':
        # First-class image ingest: OCR the image via the gateway vision model
        # (no GPU, no extra dep) and write the transcription as markdown. Fully
        # automatic — a user just shares a screenshot/photo/scan and it lands in
        # the KB. If OCR yields nothing, we still write a stub so the file is
        # tracked (and the failure is visible in the log) rather than silently
        # vanishing.
        ok, text = _ocr_image(src)
        title = src.stem.replace('_', ' ').replace('-', ' ').strip() or 'Image'
        if ok and text:
            body = f"# {title}\n\n{text}\n"
            log(f"  extracted (image OCR): {src.name} → {out.name} ({len(text)} chars)")
        else:
            body = f"# {title}\n\n_(image ingested but OCR returned no text)_\n"
            log(f"  ⚠️  image OCR empty: {src.name} → {out.name} (stub written)")
        out.write_text(body, encoding='utf-8')
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
        # AUTO visual-gap recovery: probe the PDF for scanned/mixed pages whose
        # visual content pdftotext can't see (full-page scans OR embedded
        # charts/tables-as-image). Any such pages get OCR'd and appended. Fully
        # automatic — no flag, no manual step. Best-effort: never blocks ingest.
        try:
            import importlib.util as _ilu
            probe_path = Path(__file__).parent / 'pdf_visual_probe.py'
            if probe_path.exists() and shutil.which('pdftoppm'):
                _spec = _ilu.spec_from_file_location('pdf_visual_probe', probe_path)
                _pvp = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_pvp)
                verdict = _pvp.probe(src)
                targets = verdict.get('ocr_targets', []) if isinstance(verdict, dict) else []
                pages = sorted({t['page'] for t in targets if 'page' in t})
                if pages:
                    log(f"  🔎 visual-gap: {len(pages)} page(s) need OCR ({verdict.get('ocr_needed')}) → recovering")
                    ocr_md = _ocr_pdf_pages(src, pages)
                    if ocr_md:
                        with open(out, 'a', encoding='utf-8') as _f:
                            _f.write(ocr_md)
        except Exception as _e:
            log(f"  ⚠️  visual-gap probe skipped: {_e}")
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

    if fmt in ('audio', 'video'):
        # First-class audio/video ingest: transcribe via the ladder
        # (ffmpeg → Deepgram-if-key → local faster-whisper) and write the
        # transcript as markdown. Fully automatic — a user shares an mp3/mp4 and
        # it lands in the KB, searchable via docs_search. If no ASR rung is
        # available we still write a stub so the file is tracked and the reason
        # is visible in the log (never crashes ingest).
        text = _transcribe_media(src)
        title = src.stem.replace('_', ' ').replace('-', ' ').strip() or 'Recording'
        kind = 'video' if fmt == 'video' else 'audio'
        if text:
            body = f"# {title}\n\n_({kind} transcript)_\n\n{text}\n"
            log(f"  extracted ({kind} transcript): {src.name} → {out.name} ({len(text)} chars)")
        else:
            body = (f"# {title}\n\n_({kind} ingested but no transcript — no ASR "
                    f"backend available; see log)_\n")
            log(f"  ⚠️  {kind} transcript empty: {src.name} → {out.name} (stub written)")
        out.write_text(body, encoding='utf-8')
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


def _expand_archives(paths: List[Path]) -> List[Path]:
    """Replace any .zip in `paths` with its extracted, ingestible members.
    Each member is unpacked into docs/_unzipped/<zipslug>/ (so it lives under
    DOCS_DIR and gets picked up + re-sniffed like any other file) and returned
    as a normal ingest path. Non-ingestible members are skipped. Guards:
      - path traversal (Zip Slip): members escaping the target dir are dropped.
      - member count cap (_ARCHIVE_MAX_MEMBERS) so a zip-bomb can't wedge the box.
    Best-effort: a broken zip logs a warning and is skipped, never crashes."""
    import zipfile
    _MEMBER_EXTS = (_TEXT_EXTS | {'.pdf'} | _PANDOC_EXTS | _IMAGE_EXTS
                    | _AUDIO_EXTS | _VIDEO_EXTS)  # NOTE: no _ARCHIVE_EXTS — one level
    out: List[Path] = []
    for p in paths:
        if p.suffix.lower() not in _ARCHIVE_EXTS:
            out.append(p)
            continue
        dest = DOCS_DIR / "_unzipped" / _slugify(p.stem)
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = str(dest.resolve())
        try:
            with zipfile.ZipFile(p) as zf:
                members = [m for m in zf.namelist() if not m.endswith('/')]
                if len(members) > _ARCHIVE_MAX_MEMBERS:
                    log(f"  ⚠️  archive {p.name}: {len(members)} members exceeds cap "
                        f"{_ARCHIVE_MAX_MEMBERS} — taking first {_ARCHIVE_MAX_MEMBERS}")
                    members = members[:_ARCHIVE_MAX_MEMBERS]
                extracted = 0
                for m in members:
                    ext = os.path.splitext(m)[1].lower()
                    if ext not in _MEMBER_EXTS:
                        continue  # skip non-ingestible members (exe, bin, nested zip)
                    target = (dest / m).resolve()
                    # Zip Slip guard: member must stay inside dest.
                    if not str(target).startswith(dest_resolved + os.sep):
                        log(f"  ⚠️  archive {p.name}: skipped unsafe path '{m}'")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as srcf, open(target, 'wb') as dstf:
                        shutil.copyfileobj(srcf, dstf)
                    out.append(target)
                    extracted += 1
            log(f"  📦 archive {p.name} → {extracted} ingestible member(s) unpacked")
        except Exception as e:
            log(f"  ⚠️  archive {p.name} skipped (unreadable zip): {e}")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Ingest long-form docs into vector DB')
    parser.add_argument('--file', type=Path, help='Ingest single file')
    parser.add_argument('--url', type=str,
                        help='Ingest a stream/video URL (YouTube/X/TikTok/Vimeo/...): '
                             'subtitle-first, else transcribe via the ASR ladder')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be ingested, write nothing')
    parser.add_argument('--skip-entity-extraction', action='store_true',
                        help='Skip LLM entity/relation extraction (faster, no graph update)')
    args = parser.parse_args()

    log("=" * 60)
    log("docs_ingest.py starting")
    if args.dry_run:
        log("DRY RUN — no writes")

    # Init embeddings (shared resolver: model id + local-snapshot resolution +
    # asymmetric query:/passage: prefixing all centralized in tools/_embed.py).
    log("Loading embeddings model...")
    embeddings = get_embeddings()

    # Init DB
    Path(DB_PATH).mkdir(parents=True, exist_ok=True)
    db = Chroma(
        persist_directory=DB_PATH,
        embedding_function=embeddings,
        # Stamp the embedding model id so query paths can detect a model mismatch
        # (model swap vs an old-model store => silent garbage). See _embed.warn_on_mismatch.
        collection_metadata={
            "description": "Long-form document store (structure-aware chunking)",
            EMBED_MODEL_META_KEY: model_id(),
        }
    )

    existing_hashes = get_existing_hashes(db)
    log(f"Existing DB: {len(existing_hashes)} source files")

    # Collect files to process
    if getattr(args, 'url', None):
        # URL ingest: transcribe (subtitle-first → ASR ladder) → write a markdown
        # doc under docs/, then fall through to normal ingest of that one file.
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        title, text = _transcribe_url(args.url)
        slug = _slugify(title if title and title != args.url else 'url-transcript')
        out = DOCS_DIR / f"{slug}.md"
        n = 2
        while out.exists():
            out = DOCS_DIR / f"{slug}-{n}.md"; n += 1
        if text:
            out.write_text(f"# {title}\n\n_(source: {args.url})_\n\n{text}\n",
                           encoding='utf-8')
            log(f"  extracted (URL transcript): {args.url} → {out.name} ({len(text)} chars)")
        else:
            out.write_text(f"# {title}\n\n_(source: {args.url} — no transcript "
                           f"available; see log)_\n", encoding='utf-8')
            log(f"  ⚠️  URL transcript empty: {args.url} → {out.name} (stub written)")
        paths = [out]
    elif args.file:
        if not args.file.exists():
            log(f"ERROR: file not found: {args.file}")
            sys.exit(1)
        paths = [args.file]
    else:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        # Pick up EVERY supported input, not just markdown: text, PDFs, Office
        # docs, images (jpg/png/etc → vision-OCR), audio/video (mp3/mp4/etc →
        # transcribe), and archives (.zip → walked, each member re-routed).
        # Scanned/mixed PDFs get auto-OCR'd for their visual pages. Fully
        # automatic — a user just shares any file and it ingests.
        _INGEST_EXTS = (_TEXT_EXTS | {'.pdf'} | _PANDOC_EXTS | _IMAGE_EXTS
                        | _AUDIO_EXTS | _VIDEO_EXTS | _ARCHIVE_EXTS)
        paths = sorted(
            p for p in DOCS_DIR.glob("**/*")
            if p.is_file() and p.suffix.lower() in _INGEST_EXTS
        )
        if not paths:
            log(f"No ingestible files found in {DOCS_DIR} "
                f"(supported: text, pdf, office docs, images, audio, video, zip)")
            sys.exit(0)

    # Expand any archives (.zip) in-place: unpack → each member becomes its own
    # ingest path, re-routed through the same sniffer. Nested archives are
    # unpacked one level (a zip-in-zip member is expanded on the next daily run
    # once extracted). Capped by _ARCHIVE_MAX_MEMBERS so a zip-bomb can't wedge
    # the box. Fully automatic — a user shares a .zip of docs and all readable
    # members land in the KB.
    paths = _expand_archives(paths)

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
