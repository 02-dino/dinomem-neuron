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


def get_chroma(persist_dir, collection_name):
    """Return a Chroma vectorstore bound to persist_dir/collection; deps-guarded."""
    try:
        from langchain_chroma import Chroma
    except Exception as e:
        raise SemanticUnavailable(
            "semantic rank needs langchain-chroma "
            "(pip install langchain-chroma). "
            f"import failed: {e}"
        )
    return Chroma(
        persist_directory=str(persist_dir),
        collection_name=collection_name,
        embedding_function=get_embeddings(),
    )


def semantic_available():
    """True if the optional embedding stack imports cleanly (no model load)."""
    try:
        import langchain_chroma  # noqa: F401
        import langchain_huggingface  # noqa: F401
        return True
    except Exception:
        return False
