#!/usr/bin/env python3
"""
license_check.py — dinomem-neuron runtime soft-expiry guard.

Imported at the top of each cron-driven procedure. Reads .license_expiry from the
workspace and, if the license term has ended, prints an expiry reminder and stops
the procedure (sys.exit). On perpetual builds (no .license_expiry file) it is a
no-op. Within a grace/reminder window before expiry it prints a renewal reminder
but DOES NOT block.

SOFT, client-side check: honest contract-end enforcement, not anti-piracy DRM.
A technical buyer can remove the file or edit this — that is an accepted ceiling.

Usage (top of a procedure's main()):
    from license_check import enforce_license
    enforce_license()

The workspace path is substituted at install time.
"""
import os
import sys
from datetime import datetime, timezone

# Workspace dir is substituted at install (falls back to script dir / cwd).
_WS = "DINOMEM_WORKSPACE_PLACEHOLDER"
SELLER = "https://t.me/dinotlgrm"
REMIND_DAYS = 14  # start showing a renewal reminder this many days before expiry


def _expiry_file():
    # .license_expiry lives in the workspace root next to procedures/.
    candidates = []
    if _WS and not _WS.startswith("DINOMEM_"):
        candidates.append(os.path.join(_WS, ".license_expiry"))
    # Fallbacks: env, cwd, parent of this file.
    env_ws = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("DINOMEM_WORKSPACE")
    if env_ws:
        candidates.append(os.path.join(env_ws, ".license_expiry"))
    candidates.append(os.path.join(os.getcwd(), ".license_expiry"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), ".license_expiry"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _parse(path):
    data = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    except Exception:
        return {}
    return data


def check_license():
    """Return (status, expires, licensee). status: 'perpetual'|'valid'|'reminder'|'expired'."""
    path = _expiry_file()
    if not path:
        return ("perpetual", None, None)
    data = _parse(path)
    expires = data.get("expires")
    licensee = data.get("licensee", "unknown")
    if not expires or expires == "never":
        return ("perpetual", None, licensee)
    try:
        exp = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return ("perpetual", expires, licensee)  # unparseable -> don't block
    now = datetime.now(timezone.utc)
    days_left = (exp - now).days
    if now.date() > exp.date():
        return ("expired", expires, licensee)
    if days_left <= REMIND_DAYS:
        return ("reminder", expires, licensee)
    return ("valid", expires, licensee)


def enforce_license(procedure_name=None):
    """Block (sys.exit 1) if expired; print reminder if near expiry; else continue."""
    status, expires, licensee = check_license()
    name = procedure_name or os.path.basename(sys.argv[0] or "dinomem-neuron")
    if status == "expired":
        sys.stderr.write(
            "\n"
            "  ============================================================\n"
            "  dinomem-neuron LICENSE EXPIRED\n"
            "  ============================================================\n"
            f"  This license (licensee: {licensee}) ended on {expires}.\n"
            "\n"
            "  Your dinomem memory upgrade has STOPPED WORKING:\n"
            "    - no more learning from new sessions\n"
            "    - no more automatic step-by-step note building\n"
            "    - no more automatic note task tracking (the cron that advances\n"
            "      and auto-resolves your _note_ tasks stops running)\n"
            "    - no more memory graph linking, synthesis, or promotion\n"
            "    - no more contradiction checks or topic indexing\n"
            "  Your agent falls back to plain dinomem (base, without the upgrade)\n"
            "  until the license is renewed.\n"
            "\n"
            f"  To renew and restore the full dinomem-neuron upgrade, contact:\n"
            f"  {SELLER}\n"
            "\n"
        )
        sys.exit(1)
    if status == "reminder":
        days = ""
        try:
            from datetime import datetime as _dt, timezone as _tz
            _d = (_dt.strptime(expires, "%Y-%m-%d").replace(tzinfo=_tz.utc) - _dt.now(_tz.utc)).days
            days = f" (in {_d} day{'s' if _d != 1 else ''})"
        except Exception:
            pass
        sys.stderr.write(
            "\n"
            f"  [dinomem-neuron] Heads up: your dinomem memory upgrade expires on {expires}{days}.\n"
            "  After that it STOPS WORKING - no more learning, no more auto note\n"
            "  building, no graph/synthesis/promotion. Renew to keep it running:\n"
            f"  {SELLER}\n\n"
        )
    return status


if __name__ == "__main__":
    st, exp, lic = check_license()
    print(f"status={st} expires={exp} licensee={lic}")
    sys.exit(1 if st == "expired" else 0)
