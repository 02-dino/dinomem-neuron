#!/usr/bin/env python3
"""
_torch_cap.py — cap CPU thread usage for local (non-TEI) torch-backed model loads.

Multi-agent host: several dinomem agents can fall back to a LOCAL embedding/
cross-encoder model load at the same time (TEI down, or a caller not routed
through TEI). Each such process otherwise defaults to torch's max-core
threadpool, which thread-storms a shared box when N of them line up.

Import this module FIRST — before torch/langchain_huggingface/sentence_transformers
— in any file that may construct a local model. Side-effect only; no public API.
Override the cap via env DINOMEM_TORCH_THREADS. Fail-open if torch isn't
installed (e.g. exact-SQL-only environments).
"""
import os

_TORCH_THREADS = os.environ.get("DINOMEM_TORCH_THREADS", "4")
os.environ.setdefault("OMP_NUM_THREADS", _TORCH_THREADS)
os.environ.setdefault("MKL_NUM_THREADS", _TORCH_THREADS)
try:
    import torch as _torch
    _torch.set_num_threads(int(_TORCH_THREADS))
except Exception:
    pass
