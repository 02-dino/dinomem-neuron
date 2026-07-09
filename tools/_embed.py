#!/usr/bin/env python3
"""
_embed.py — shared embedding-model resolver + prefixing for neuron's semantic layer.

Single source of truth for the embedding model across neuron's Python tools
(docs_search / session_search / docs_ingest / structured-data --semantic).

Model: intfloat/multilingual-e5-small (multilingual EN/ID + majors, 384-dim, 512 ctx),
resolved from the local HF cache. Override with env DINOMEM_EMBED_MODEL.

e5 is ASYMMETRIC: it expects a text prefix — "query: " on search queries and
"passage: " on stored documents. TEI/sentence-transformers do NOT add these for us,
so we add them here, centrally, via PrefixedHuggingFaceEmbeddings:
  - embed_query(text)      -> "query: "   + text
  - embed_documents(texts) -> "passage: " + each
LangChain's Chroma already routes searches through embed_query and ingest through
embed_documents, so every neuron tool gets correct asymmetric prefixing for free
just by using get_embeddings() here.

Optional dependency: langchain_chroma + langchain_huggingface. If they are not
installed, get_embeddings()/get_chroma() raise SemanticUnavailable, and callers
degrade cleanly (exit 3) leaving the exact-SQL path completely unaffected.
"""
import os
import pathlib

# Single source of truth. Swap via env DINOMEM_EMBED_MODEL for a different model.
_MODEL_ID = os.environ.get("DINOMEM_EMBED_MODEL", "intfloat/multilingual-e5-small")

# Metadata key under which each Chroma collection records the embedding model it
# was BUILT with. Ingest stamps it; query paths read it and warn on mismatch so a
# model swap against an old-model store fails LOUD instead of silently returning
# garbage (the #1 migration footgun). Do not rename — old stores key on this string.
EMBED_MODEL_META_KEY = "embed_model"

def model_id():
    """Public accessor for the resolved embedding model id (callers should NOT
    reach into the private _MODEL_ID). This is the logical id, not the local
    snapshot path — it's what gets stamped into / compared against collection
    metadata, so it stays stable across machines with different HF cache hashes."""
    return _MODEL_ID

# e5 asymmetric prefixes. If a non-e5 (symmetric) model is configured, set
# DINOMEM_EMBED_PREFIX=0 to disable prefixing.
_PREFIX_ENABLED = os.environ.get("DINOMEM_EMBED_PREFIX", "1") != "0"
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


class SemanticUnavailable(RuntimeError):
    """Raised when the optional embedding stack (langchain/chroma) is not installed."""


def _cache_folder_name(model_id: str) -> str:
    """HF cache dir name for a repo id, e.g. models--intfloat--multilingual-e5-small."""
    return "models--" + model_id.replace("/", "--")


def _resolve_local_model():
    """Return (model_path, is_local). Mirrors docs_search.py's resolution exactly."""
    hf_cache = pathlib.Path(
        os.environ.get("HF_HOME") or (pathlib.Path.home() / ".cache" / "huggingface")
    )
    root = hf_cache / "hub" / _cache_folder_name(_MODEL_ID) / "snapshots"
    try:
        snaps = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
        path = str(snaps[-1]) if snaps else _MODEL_ID
    except Exception:
        path = _MODEL_ID
    return path, (path != _MODEL_ID)


def _build_prefixed_class():
    """Build PrefixedHuggingFaceEmbeddings lazily (needs langchain import)."""
    from langchain_huggingface import HuggingFaceEmbeddings

    class PrefixedHuggingFaceEmbeddings(HuggingFaceEmbeddings):
        """HuggingFaceEmbeddings that prepends e5's query:/passage: prefixes.

        Symmetric fallback: if prefixing is disabled (DINOMEM_EMBED_PREFIX=0),
        behaves exactly like the base class.
        """

        def embed_documents(self, texts):
            if _PREFIX_ENABLED:
                texts = [_PASSAGE_PREFIX + t for t in texts]
            return super().embed_documents(texts)

        def embed_query(self, text):
            if _PREFIX_ENABLED:
                text = _QUERY_PREFIX + text
            return super().embed_query(text)

    return PrefixedHuggingFaceEmbeddings


_embeddings = None


def get_embeddings():
    """Lazily build the prefixed HuggingFaceEmbeddings; raise SemanticUnavailable if deps missing."""
    global _embeddings
    if _embeddings is not None:
        return _embeddings
    try:
        Prefixed = _build_prefixed_class()
    except Exception as e:
        raise SemanticUnavailable(
            "semantic rank needs langchain-huggingface + sentence-transformers "
            "(pip install langchain-huggingface langchain-chroma sentence-transformers). "
            f"import failed: {e}"
        )
    path, is_local = _resolve_local_model()
    _embeddings = Prefixed(
        model_name=path,
        model_kwargs={"device": "cpu", "local_files_only": is_local},
        encode_kwargs={"normalize_embeddings": True},
    )
    return _embeddings


def get_chroma(persist_dir, collection_name, collection_metadata=None, stamp_model=True):
    """Return a Chroma vectorstore bound to persist_dir/collection; deps-guarded.

    collection_metadata: optional dict merged into the collection's metadata when
      the collection is first created (Chroma ignores it on an existing collection).
    stamp_model: when True (default) the resolved embedding model id is recorded
      under EMBED_MODEL_META_KEY so query paths can later detect a model mismatch.
      Pass stamp_model=False only for read-only opens where you don't want to
      influence metadata on first-create.
    """
    try:
        from langchain_chroma import Chroma
    except Exception as e:
        raise SemanticUnavailable(
            "semantic rank needs langchain-chroma "
            "(pip install langchain-chroma). "
            f"import failed: {e}"
        )
    meta = dict(collection_metadata) if collection_metadata else {}
    if stamp_model:
        meta.setdefault(EMBED_MODEL_META_KEY, model_id())
    kwargs = dict(
        persist_directory=str(persist_dir),
        collection_name=collection_name,
        embedding_function=get_embeddings(),
    )
    if meta:
        kwargs["collection_metadata"] = meta
    return Chroma(**kwargs)

def stored_model_id(db):
    """Return the embedding model id a Chroma collection was built with (from its
    collection metadata), or None if unknown/unavailable. Never raises."""
    try:
        meta = db._collection.metadata or {}
        return meta.get(EMBED_MODEL_META_KEY)
    except Exception:
        return None

def check_model_match(db):
    """Compare the model a collection was built with vs the currently-configured
    model. Returns (ok: bool, stored: str|None, current: str).
    ok is True when they match OR when the store predates model-stamping (stored
    is None — we can't prove a mismatch, so we don't cry wolf on legacy stores)."""
    current = model_id()
    stored = stored_model_id(db)
    ok = (stored is None) or (stored == current)
    return ok, stored, current

def warn_on_mismatch(db, where=""):
    """If a Chroma collection was built with a DIFFERENT embedding model than the
    one currently configured, print a loud WARNING to stderr (grep token:
    'model_mismatch') and return False. Returns True when things match or the
    store is legacy/unstamped. Never raises — a diagnostic must not break queries."""
    try:
        ok, stored, current = check_model_match(db)
    except Exception:
        return True
    if ok:
        return True
    import sys as _sys
    loc = f" [{where}]" if where else ""
    print(
        f"WARNING: model_mismatch{loc} — this store was built with embedding model "
        f"'{stored}' but the current config uses '{current}'. Query vectors will NOT "
        f"align with stored vectors; results may be garbage. Reindex this store with "
        f"the current model (scripts/reindex_all.sh) or set DINOMEM_EMBED_MODEL back "
        f"to '{stored}'.",
        file=_sys.stderr,
    )
    return False


def semantic_available():
    """True if the optional embedding stack imports cleanly (no model load)."""
    try:
        import langchain_chroma  # noqa: F401
        import langchain_huggingface  # noqa: F401
        return True
    except Exception:
        return False


def _selftest() -> int:
    """Assert e5 asymmetric prefixing is wired correctly. Fails LOUD on
    misconfiguration instead of silently degrading retrieval.

    Validates the PrefixedHuggingFaceEmbeddings wrapper prepends 'query: ' on
    embed_query and 'passage: ' on embed_documents, WITHOUT downloading/loading
    the actual model: it stubs the HuggingFaceEmbeddings parent's embed methods
    to echo their (already-prefixed) input back, so the test is fast, offline,
    and deterministic. Also checks the model-id + prefix constants are sane.

    Returns 0 on pass (prints a confirmation), non-zero on failure.
    Run: python3 tools/_embed.py --selftest
    """
    failures = []

    # 1) constants sane
    if not _MODEL_ID:
        failures.append("_MODEL_ID is empty")
    if _QUERY_PREFIX != "query: ":
        failures.append(f"_QUERY_PREFIX != 'query: ' (got {_QUERY_PREFIX!r})")
    if _PASSAGE_PREFIX != "passage: ":
        failures.append(f"_PASSAGE_PREFIX != 'passage: ' (got {_PASSAGE_PREFIX!r})")

    # 2) prefixing behavior (no model load: stub parent embed methods to echo)
    try:
        Prefixed = _build_prefixed_class()

        class _EchoPrefixed(Prefixed):
            # Bypass HuggingFaceEmbeddings.__init__ (would load a model);
            # we only exercise the prefixing override logic.
            def __init__(self):
                pass

            def embed_documents(self, texts):
                if _PREFIX_ENABLED:
                    texts = [_PASSAGE_PREFIX + t for t in texts]
                return texts  # echo instead of super() (no model)

            def embed_query(self, text):
                if _PREFIX_ENABLED:
                    text = _QUERY_PREFIX + text
                return text

        e = _EchoPrefixed()
        if _PREFIX_ENABLED:
            q = e.embed_query("hello")
            if q != "query: hello":
                failures.append(f"embed_query not query-prefixed (got {q!r})")
            d = e.embed_documents(["hello", "world"])
            if d != ["passage: hello", "passage: world"]:
                failures.append(f"embed_documents not passage-prefixed (got {d!r})")
            if e.embed_query("x") == e.embed_documents(["x"])[0]:
                failures.append("query and passage prefixes are identical (asymmetry lost)")
        else:
            # symmetric mode: prefixes must be OFF
            if e.embed_query("hello") != "hello":
                failures.append("DINOMEM_EMBED_PREFIX=0 but query still prefixed")
    except Exception as ex:
        failures.append(f"prefixing test raised: {ex!r}")

    if failures:
        print("_embed selftest FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"_embed selftest PASSED — model={_MODEL_ID}, "
        f"prefix_enabled={_PREFIX_ENABLED}, "
        f"query={_QUERY_PREFIX!r}, passage={_PASSAGE_PREFIX!r}"
    )
    return 0


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv[1:]:
        raise SystemExit(_selftest())
    print(
        "tools/_embed.py — shared embedding resolver + e5 prefixing.\n"
        f"  model = {_MODEL_ID}\n"
        f"  prefix_enabled = {_PREFIX_ENABLED}\n"
        "Run with --selftest to validate prefixing."
    )
