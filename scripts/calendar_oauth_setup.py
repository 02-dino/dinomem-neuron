#!/usr/bin/env python3
"""
calendar_oauth_setup.py — One-time Google Calendar OAuth2 setup.

Run this once to authorize access to Google Calendar.
Saves token to workspace-analyst/.env as GOOGLE_CALENDAR_TOKEN (JSON).

Prerequisites:
1. Go to https://console.cloud.google.com/
2. Create a project → Enable Google Calendar API
3. Create OAuth2 credentials (Desktop app) → Download as client_secret.json
4. Place client_secret.json in workspace-analyst/

Usage:
  python3 scripts/calendar_oauth_setup.py
"""

import json
import os
import sys
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
CLIENT_SECRET = WORKSPACE / "client_secret.json"
ENV_FILE = WORKSPACE / ".env"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def main():
    if not CLIENT_SECRET.exists():
        print(f"ERROR: {CLIENT_SECRET} not found.")
        print("Download OAuth2 credentials from Google Cloud Console:")
        print("  APIs & Services → Credentials → Create → OAuth client ID → Desktop app")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("ERROR: Run: pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    print("Opening browser for Google Calendar authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0)

    token_json = creds.to_json()

    # Read existing .env
    env_lines = []
    if ENV_FILE.exists():
        env_lines = [l for l in ENV_FILE.read_text().splitlines()
                     if not l.startswith("GOOGLE_CALENDAR_TOKEN=")
                     and not l.startswith("GOOGLE_CALENDAR_ID=")]

    # Append token + default calendar ID
    env_lines.append(f"GOOGLE_CALENDAR_TOKEN={token_json}")
    env_lines.append("GOOGLE_CALENDAR_ID=primary")

    ENV_FILE.write_text("\n".join(env_lines) + "\n")
    print(f"Token saved to {ENV_FILE}")
    print("GOOGLE_CALENDAR_ID defaults to 'primary' — edit .env to change.")
    print("\nDone. You can now use calendar_tool.py.")

if __name__ == "__main__":
    main()
