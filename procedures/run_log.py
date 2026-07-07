#!/usr/bin/env python3
"""
run_log.py — Per-Run Audit Trail (Feature 2).

Lightweight append-only JSONL logger for dinomem-neuron cron runs.
Every significant script logs start/end here — inspectable, searchable,
not just stdout logs.

Log path: logs/run_history.jsonl

Usage (as CLI):
  python3 run_log.py --list [--last N] [--script NAME] [--status STATUS]
  python3 run_log.py --tail [N]

Usage (as library):
  from procedures.run_log import RunLog
  with RunLog("memory_synthesis") as log:
      log.artifact("graph_json", str(GRAPH_JSON))
      # ... do work ...
      log.summary("synthesized 3 clusters, 1 insight promoted")
      # status auto-set to "success" on clean exit, "failed" on exception
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run (ran from ZIP/skill dir)
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
BASE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
LOG_DIR = BASE / "logs"
LOG_FILE = LOG_DIR / "run_history.jsonl"


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def new_run_id():
    return uuid.uuid4().hex[:12]


class RunLog:
    """Context manager for logging a single script run."""

    def __init__(self, script: str, run_id: str = None):
        self.script = script
        self.run_id = run_id or new_run_id()
        self.started_at = now_iso()
        self.finished_at = None
        self.status = "running"
        self.summary_text = ""
        self.artifacts = {}
        self._record = None

    def artifact(self, key: str, value: str):
        """Record a named artifact path or value."""
        self.artifacts[key] = value

    def summary(self, text: str):
        """Set the human-readable summary for this run."""
        self.summary_text = text

    def _write(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": self.run_id,
            "script": self.script,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "summary": self.summary_text,
            "artifacts": self.artifacts,
            "pid": os.getpid(),
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._record = record

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finished_at = now_iso()
        if exc_type is not None:
            self.status = "failed"
            if not self.summary_text:
                self.summary_text = f"{exc_type.__name__}: {exc_val}"
        else:
            if self.status == "running":
                self.status = "success"
        self._write()
        return False  # don't suppress exceptions


def log_run(script: str, status: str, summary: str = "", artifacts: dict = None,
            started_at: str = None, finished_at: str = None, run_id: str = None):
    """One-shot log write (non-context-manager usage)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id or new_run_id(),
        "script": script,
        "started_at": started_at or now_iso(),
        "finished_at": finished_at or now_iso(),
        "status": status,
        "summary": summary,
        "artifacts": artifacts or {},
        "pid": os.getpid(),
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _load_records(script_filter=None, status_filter=None, last_n=None):
    if not LOG_FILE.exists():
        return []
    records = []
    with LOG_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if script_filter and r.get("script") != script_filter:
                continue
            if status_filter and r.get("status") != status_filter:
                continue
            records.append(r)
    if last_n:
        records = records[-last_n:]
    return records


def cmd_list(args):
    records = _load_records(
        script_filter=args.script,
        status_filter=args.status,
        last_n=args.last,
    )
    if not records:
        print("(no records)")
        return
    for r in records:
        started = r.get("started_at", "?")[:19].replace("T", " ")
        finished = r.get("finished_at", "?")[:19].replace("T", " ")
        status = r.get("status", "?")
        script = r.get("script", "?")
        summary = r.get("summary", "")[:80]
        run_id = r.get("run_id", "?")
        status_icon = {"success": "✓", "failed": "✗", "partial": "~", "running": "…"}.get(status, "?")
        print(f"{status_icon} [{started}] {script} ({run_id}) — {summary or status}")
        if r.get("artifacts"):
            for k, v in r["artifacts"].items():
                print(f"    {k}: {v}")


def cmd_tail(args):
    n = args.tail if args.tail else 5
    records = _load_records(last_n=n)
    if not records:
        print("(no records)")
        return
    for r in records:
        print(json.dumps(r, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="run_log.py — dinomem-neuron audit trail")
    ap.add_argument("--list", action="store_true", help="List run history")
    ap.add_argument("--last", type=int, default=20, metavar="N", help="Show last N records (default 20)")
    ap.add_argument("--script", metavar="NAME", help="Filter by script name")
    ap.add_argument("--status", metavar="STATUS", help="Filter by status (success/failed/partial)")
    ap.add_argument("--tail", type=int, nargs="?", const=5, metavar="N", help="Tail last N raw JSON records")
    args = ap.parse_args()

    if args.tail is not None:
        cmd_tail(args)
    elif args.list or args.script or args.status:
        cmd_list(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
