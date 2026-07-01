#!/usr/bin/env python3
"""
context_watchdog.py — Context Budget Watchdog (Feature 4, Part B).

System cron script (NOT OpenClaw cron). No LLM calls. Pure REST.
Fires every 5 minutes via Linux crontab:
  */5 * * * * python3 /path/to/scripts/context_watchdog.py

Checks all active OpenClaw sessions for context% > THRESHOLD.
If over threshold, sends a wake event: "CHECKPOINT NOW — context at X%, write resume_state and stop."
The session receives it as a system event mid-turn and acts on it.

Setup:
  1. Set OPENCLAW_GATEWAY_URL and OPENCLAW_GATEWAY_TOKEN in environment or .env
  2. Add to crontab: */5 * * * * python3 /path/to/scripts/context_watchdog.py >> /tmp/context_watchdog.log 2>&1
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
THRESHOLD_PCT = 60          # warn at this context%
CRITICAL_PCT = 80           # urgent warn at this context%
CHECKPOINT_MSG = "CHECKPOINT NOW — context at {pct}%, write resume_state and stop cleanly. Do not start new steps."
CRITICAL_MSG = "URGENT CHECKPOINT — context at {pct}%, stop immediately, write resume_state, do not execute further steps."

# Load .env if present (for gateway token)
def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        # Try workspace root
        env_path = Path(os.environ.get("OPENCLAW_WORKSPACE", Path.home() / ".openclaw/workspace")) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()

GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:3000")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

def now():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def log(msg):
    print(f"[{now()}] context_watchdog: {msg}", flush=True)

def api(method, path, body=None):
    """Minimal REST call using urllib (no external deps)."""
    import urllib.request
    import urllib.error
    url = GATEWAY_URL.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log(f"error on {method} {path}: {e}")
        return None

def get_sessions():
    """List active sessions."""
    result = api("GET", "/api/sessions?kind=main,subagent&limit=50")
    if not result:
        return []
    # Handle both list and {sessions: [...]} shapes
    if isinstance(result, list):
        return result
    return result.get("sessions", [])

def get_session_status(session_key):
    """Get context% for a session."""
    result = api("GET", f"/api/sessions/{urllib.parse.quote(session_key, safe='')}/status")
    return result

def send_wake(session_key, text, mode="now"):
    """Send a wake event to a session."""
    return api("POST", "/api/cron/wake", {
        "sessionKey": session_key,
        "text": text,
        "mode": mode,
    })

def main():
    import urllib.parse

    if not GATEWAY_TOKEN:
        log("OPENCLAW_GATEWAY_TOKEN not set — skipping (set in .env or environment)")
        sys.exit(0)

    sessions = get_sessions()
    if not sessions:
        log("no active sessions found or gateway unreachable")
        sys.exit(0)

    checked = 0
    warned = 0

    for session in sessions:
        session_key = session.get("key") or session.get("sessionKey")
        if not session_key:
            continue

        # Only check sessions that are actively running (not idle/done)
        status = session.get("status")
        if status in ("done", "idle", "archived"):
            continue

        # Get context tokens
        context_tokens = session.get("contextTokens")
        context_window = None

        # Try to get from session status endpoint if not in list
        if context_tokens is None:
            status_data = get_session_status(session_key)
            if status_data:
                context_tokens = status_data.get("contextTokens")
                context_window = status_data.get("contextWindow")
        else:
            # Infer context window from model (default 200k for Claude)
            context_window = session.get("contextWindow", 200000)

        if not context_tokens or not context_window:
            continue

        pct = round((context_tokens / context_window) * 100, 1)
        checked += 1

        if pct >= CRITICAL_PCT:
            msg = CRITICAL_MSG.format(pct=pct)
            result = send_wake(session_key, msg, mode="now")
            if result:
                log(f"CRITICAL wake sent to {session_key} (context {pct}%)")
                warned += 1
            else:
                log(f"failed to send wake to {session_key}")
        elif pct >= THRESHOLD_PCT:
            msg = CHECKPOINT_MSG.format(pct=pct)
            result = send_wake(session_key, msg, mode="now")
            if result:
                log(f"checkpoint wake sent to {session_key} (context {pct}%)")
                warned += 1
            else:
                log(f"failed to send wake to {session_key}")

    log(f"done — checked {checked} active sessions, sent {warned} checkpoint wake(s)")

if __name__ == "__main__":
    main()
