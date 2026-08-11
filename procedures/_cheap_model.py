#!/usr/bin/env python3
"""Single source of truth for the cheap/no-reasoning model.

Auto-linked to the compaction model so ONE anchor controls everything:
change agents.defaults.compaction.model in openclaw.json -> every cheap-LLM
call (dinomem bulk + analyst HyDE + no-reasoning crons) follows automatically.

Precedence (first wins):
  1. DINOMEM_CHEAP_MODEL env  -> explicit override (e.g. a cron that pins a model)
  2. agents.defaults.compaction.model in openclaw.json  -> the ANCHOR
  3. hardcoded haiku fallback  -> safety net (never 404s if config unreadable)

Reasoning stays on agents.defaults.model.primary; that is NOT this function's
job. This only resolves the cheap tier.
"""
import json
import os
from pathlib import Path

_HAIKU_FALLBACK = "ninerouter/cc/claude-haiku-4-5-20251001"

# Config path is overridable for tests / non-default installs.
_CONFIG_PATH = os.environ.get("OPENCLAW_CONFIG") or os.environ.get("DINOMEM_OPENCLAW_CONFIG") or str(Path.home() / ".openclaw" / "openclaw.json")


def _compaction_model(config_path: str = _CONFIG_PATH) -> str:
    """Read agents.defaults.compaction.model. Empty string on any failure."""
    try:
        with open(config_path, "r") as fh:
            cfg = json.load(fh)
        m = (
            cfg.get("agents", {})
            .get("defaults", {})
            .get("compaction", {})
            .get("model", "")
        )
        return (m or "").strip()
    except Exception:
        return ""


def cheap_model(config_path: str = _CONFIG_PATH) -> str:
    """Resolve the cheap model. env override -> compaction anchor -> haiku."""
    env = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
    if env:
        return env
    anchor = _compaction_model(config_path)
    if anchor:
        return anchor
    # legacy back-compat env, then hard fallback
    legacy = os.environ.get("DINOMEM_NO_REASONING_MODEL", "").strip()
    return legacy or _HAIKU_FALLBACK


if __name__ == "__main__":
    print(cheap_model())
