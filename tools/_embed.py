#!/usr/bin/env python3
"""
_embed.py — shared embedding-model resolver for the structured-data semantic layer.

Reuses the SAME model the rest of neuron's semantic tools use
(sentence-transformers/all-MiniLM-L6-v2, resolved from the local HF cache), so the
structured-data --semantic path is consistent with docs_search / session_search.
No new model, no multilingual switch (that is a separate parked project).

Optional dependency: langchain_chroma + langchain_huggingface. If they are not
installed, get_embeddings()/get_chroma() raise SemanticUnavailable, and callers
degrade cleanly (exit 3) leaving the exact-SQL path completely unaffected.
"""
import os
import pathlib

_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


class SemanticUnavailable(RuntimeError):
    """Raised when the optional embedding stack (langchain/chroma) is not installed."""


def _resolve_local_model():
    """Return (model_path, is_local) mirroring docs_search.py's resolution exactly."""
    hf_cache = pathlib.Path(
        os.environ.get("HF_HOME") or (pathlib.Path.home() / ".cache" / "huggingface")
    )
    root = hf_cache / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots"
    try:
        snaps = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
        path = str(snaps[-1]) if snaps else _MODEL_ID
    except Exception:
        path = _MODEL_ID
    return path, (path != _MODEL_ID)


_embeddings = None


def get_embeddings():
    """Lazily build the HuggingFaceEmbeddings; raise SemanticUnavailable if deps missing."""
    global _embeddings
    if _embeddings is not None:
        return _embeddings
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except Exception as e:
        raise SemanticUnavailable(
            "semantic rank needs langchain-huggingface + sentence-transformers "
            "(pip install langchain-huggingface langchain-chroma sentence-transformers). "
            f"import failed: {e}"
        )
    path, is_local = _resolve_local_model()
    _embeddings = HuggingFaceEmbeddings(
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
