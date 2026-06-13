#!/usr/bin/env python3
"""
calendar_tool.py — Google Calendar integration for dinomem _note_ lifecycle.

Creates, lists, and deletes Google Calendar events linked to memory/_note_*.md files.
Called by the agent when saving a _note_ that implies a time/date, or on user request.

Usage (CLI):
  python3 tools/calendar_tool.py create "Add dark mode to app" --date 2026-06-20
  python3 tools/calendar_tool.py create "Doctor appointment" --date 2026-06-25 --time 14:00 --note _note_doctor.md
  python3 tools/calendar_tool.py list
  python3 tools/calendar_tool.py list --upcoming 7
  python3 tools/calendar_tool.py delete <event_id>

Usage (import):
  from tools.calendar_tool import create_event, list_events, delete_event, get_past_events
"""

import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

WORKSPACE = Path(__file__).parent.parent
ENV_FILE = WORKSPACE / ".env"

# ── Load credentials from .env ────────────────────────────────────────────────
def _load_env():
    if not ENV_FILE.exists():
        return {}
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

def _get_service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print("ERROR: Run: pip install google-api-python-client google-auth", file=sys.stderr)
        sys.exit(1)

    env = _load_env()
    token_json = env.get("GOOGLE_CALENDAR_TOKEN")
    if not token_json:
        print("ERROR: GOOGLE_CALENDAR_TOKEN not set. Run: python3 scripts/calendar_oauth_setup.py", file=sys.stderr)
        sys.exit(1)

    creds = Credentials.from_authorized_user_info(json.loads(token_json))

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Save refreshed token back to .env
        env_lines = [l for l in ENV_FILE.read_text().splitlines()
                     if not l.startswith("GOOGLE_CALENDAR_TOKEN=")]
        env_lines.append(f"GOOGLE_CALENDAR_TOKEN={creds.to_json()}")
        ENV_FILE.write_text("\n".join(env_lines) + "\n")

    return build("calendar", "v3", credentials=creds), env.get("GOOGLE_CALENDAR_ID", "primary")

# ── Core functions ────────────────────────────────────────────────────────────
def create_event(
    title: str,
    date: str,                          # YYYY-MM-DD
    time: Optional[str] = None,         # HH:MM (24h), None = all-day
    description: Optional[str] = None,
    note_file: Optional[str] = None,    # _note_*.md filename linked to this event
    reminders: Optional[List[Dict]] = None,  # [{"method": "popup"|"email", "minutes": int}]
    attendees: Optional[List[str]] = None,   # list of email addresses
) -> Dict:
    """
    Create a Google Calendar event.

    Args:
        title:       Event title
        date:        Date string YYYY-MM-DD
        time:        Optional time HH:MM (24h). None = all-day event.
        description: Optional event description
        note_file:   Optional _note_*.md filename to link (stored in event description)
        reminders:   Optional list of reminders, e.g. [{"method": "popup", "minutes": 30}]
                     method: "popup" (notification) or "email"
                     minutes: how many minutes before the event
                     Note: sound/vibration are device-level settings, not controllable via API.
        attendees:   Optional list of email addresses to invite

    Returns:
        dict with event_id, title, date, html_link
    """
    service, cal_id = _get_service()

    desc_parts = []
    if description:
        desc_parts.append(description)
    if note_file:
        desc_parts.append(f"dinomem_note:{note_file}")
    desc = "\n".join(desc_parts)

    if time:
        dt_start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        dt_end = dt_start + timedelta(hours=1)
        event_body = {
            "summary": title,
            "description": desc,
            "start": {"dateTime": dt_start.isoformat(), "timeZone": "UTC"},
            "end":   {"dateTime": dt_end.isoformat(),   "timeZone": "UTC"},
        }
    else:
        # All-day event
        event_body = {
            "summary": title,
            "description": desc,
            "start": {"date": date},
            "end":   {"date": date},
        }

    # Reminders
    # Always useDefault: False — 30 min popup is hardcoded baseline regardless of user input.
    # If user specifies custom time X: add popup:X + any other methods (email, etc) at X.
    baseline = {"method": "popup", "minutes": 30}
    if reminders:
        extra = []
        for r in reminders:
            extra.append(r)  # user-specified (e.g. email:60)
            # Also add popup at same time if not already popup
            if r["method"] != "popup":
                extra.append({"method": "popup", "minutes": r["minutes"]})
        overrides = [baseline] + extra
    else:
        overrides = [baseline]
    # Deduplicate
    seen = set()
    deduped = []
    for r in overrides:
        key = (r["method"], r["minutes"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    event_body["reminders"] = {"useDefault": False, "overrides": deduped}

    # Attendees
    if attendees:
        event_body["attendees"] = [{"email": email} for email in attendees]

    result = service.events().insert(
        calendarId=cal_id,
        body=event_body,
        sendUpdates="all" if attendees else "none",  # send invite emails if attendees
    ).execute()
    return {
        "event_id": result["id"],
        "title": result["summary"],
        "date": date,
        "html_link": result.get("htmlLink", ""),
    }

def list_events(upcoming_days: int = 7) -> List[Dict]:
    """
    List upcoming events within the next N days.

    Returns:
        List of dicts: {event_id, title, date, time, note_file, html_link}
    """
    service, cal_id = _get_service()

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=upcoming_days)

    result = service.events().list(
        calendarId=cal_id,
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    events = []
    for e in result.get("items", []):
        start = e.get("start", {})
        date = start.get("date") or start.get("dateTime", "")[:10]
        time = start.get("dateTime", "")[11:16] if "dateTime" in start else None
        desc = e.get("description", "")
        note_file = None
        for line in desc.splitlines():
            if line.startswith("dinomem_note:"):
                note_file = line.replace("dinomem_note:", "").strip()
        events.append({
            "event_id": e["id"],
            "title": e.get("summary", ""),
            "date": date,
            "time": time,
            "note_file": note_file,
            "html_link": e.get("htmlLink", ""),
        })
    return events

def get_past_events(days_back: int = 1) -> List[Dict]:
    """
    Get events that ended within the last N days (for auto-resolve).

    Returns:
        List of dicts with note_file set (only events linked to a _note_)
    """
    service, cal_id = _get_service()

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)

    result = service.events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=now.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=100,
    ).execute()

    events = []
    for e in result.get("items", []):
        desc = e.get("description", "")
        note_file = None
        for line in desc.splitlines():
            if line.startswith("dinomem_note:"):
                note_file = line.replace("dinomem_note:", "").strip()
        if note_file:
            start_raw = e.get("start", {})
            date = start_raw.get("date") or start_raw.get("dateTime", "")[:10]
            events.append({
                "event_id": e["id"],
                "title": e.get("summary", ""),
                "date": date,
                "note_file": note_file,
            })
    return events

def delete_event(event_id: str) -> bool:
    """Delete a calendar event by ID."""
    service, cal_id = _get_service()
    try:
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        return True
    except Exception as e:
        print(f"ERROR deleting event {event_id}: {e}", file=sys.stderr)
        return False

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Google Calendar tool for dinomem")
    sub = parser.add_subparsers(dest="cmd")

    # create
    p_create = sub.add_parser("create", help="Create a calendar event")
    p_create.add_argument("title", help="Event title")
    p_create.add_argument("--date", required=True, help="Date YYYY-MM-DD")
    p_create.add_argument("--time", default=None, help="Time HH:MM (24h), omit for all-day")
    p_create.add_argument("--desc", default=None, help="Event description")
    p_create.add_argument("--note", default=None, help="_note_*.md filename to link")
    p_create.add_argument("--reminder", action="append", metavar="METHOD:MINUTES",
                          help="Reminder e.g. popup:30 or email:60. Repeat for multiple.")
    p_create.add_argument("--attendee", action="append", metavar="EMAIL",
                          help="Invite attendee by email. Repeat for multiple.")

    # list
    p_list = sub.add_parser("list", help="List upcoming events")
    p_list.add_argument("--upcoming", type=int, default=7, help="Days ahead (default: 7)")

    # delete
    p_del = sub.add_parser("delete", help="Delete an event")
    p_del.add_argument("event_id", help="Event ID")

    args = parser.parse_args()

    if args.cmd == "create":
        # Parse reminders: "popup:30" -> {"method": "popup", "minutes": 30}
        reminders = None
        if args.reminder:
            reminders = []
            for r in args.reminder:
                method, _, mins = r.partition(":")
                reminders.append({"method": method.strip(), "minutes": int(mins.strip())})
        result = create_event(args.title, args.date, args.time, args.desc, args.note,
                              reminders=reminders, attendees=args.attendee)
        print(f"Created: {result['title']} on {result['date']}")
        print(f"Event ID: {result['event_id']}")
        print(f"Link: {result['html_link']}")

    elif args.cmd == "list":
        events = list_events(args.upcoming)
        if not events:
            print(f"No events in the next {args.upcoming} days.")
        else:
            print(f"{'Date':<12} {'Time':<6} {'Title':<40} {'Note'}")
            print("-" * 75)
            for e in events:
                t = e["time"] or "all-day"
                n = e["note_file"] or ""
                print(f"{e['date']:<12} {t:<6} {e['title'][:39]:<40} {n}")

    elif args.cmd == "delete":
        ok = delete_event(args.event_id)
        print("Deleted." if ok else "Failed.")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
