#!/usr/bin/env python3
"""Validates a private profile bootstrap file before it's loaded into the app.

The bootstrap file (`user_profile.local.json`) is deliberately the SAME shape
PLANS.exportJson() already produces in the browser (Profile tab -> Export
JSON), plus one extra top-level field:

    {
      "schemaVersion": <int, matches frontend/src/plans.js SCHEMA>,
      "exportedAt": "<ISO 8601>",
      "generator": "snu-bid-simulator",
      "datasetVersion": "<the timetable dataset this was built against, or null>",
      "ruleVersion": "...", "modelVersion": "...",
      "payload": { ... same shape as a saved plan's payload ... }
    }

Re-using the plan-export shape means the browser's own existing "Import..."
button on the Profile tab (PLANS.importAsNewPlan / importReplacingActive)
already accepts this file with no code changes - this tool exists to
validate it *before* handing it to a student or a machine, and to stage it
for the desktop app's own first-run bootstrap logic (see
backend/desktop_launcher.py's --import-profile flag, which performs the same
validation and copies the file into this user's %LOCALAPPDATA%\\SNU
Scheduler\\ directory - the two are kept intentionally consistent).

Never commit the real output of this tool: see .gitignore.

Usage:
    python3 tools/import_personal_profile.py --input my-plan.json
    python3 tools/import_personal_profile.py --input my-plan.json --stage
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCHEMA_VERSION_MAX = 7  # must not exceed frontend/src/plans.js's current SCHEMA
DANGEROUS_KEYS = {"__proto__", "constructor", "prototype"}


def _appdata_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "SNU Scheduler"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_dangerous_keys(obj, path="$") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in DANGEROUS_KEYS:
                found.append(f"{path}.{k}")
            found.extend(_find_dangerous_keys(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_find_dangerous_keys(v, f"{path}[{i}]"))
    return found


def validate(data: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["top level must be a JSON object"]
    sv = data.get("schemaVersion")
    if not isinstance(sv, int):
        errors.append("missing/non-integer 'schemaVersion'")
    elif sv > SCHEMA_VERSION_MAX:
        errors.append(f"schemaVersion {sv} is newer than this tool recognizes "
                      f"(max {SCHEMA_VERSION_MAX}) - update the app first")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        errors.append("missing 'payload' object (the actual plan data)")
    else:
        dangerous = _find_dangerous_keys(payload)
        if dangerous:
            errors.append(f"payload contains unsafe key(s), rejected: {dangerous}")
        courses = payload.get("courses")
        if courses is not None and not isinstance(courses, list):
            errors.append("payload.courses must be an array")
        elif isinstance(courses, list):
            seen = set()
            for i, c in enumerate(courses):
                if not isinstance(c, dict) or not c.get("code"):
                    errors.append(f"payload.courses[{i}] is missing a code")
                    continue
                if c["code"] in seen:
                    errors.append(f"duplicate course code in wishlist: {c['code']}")
                seen.add(c["code"])
    return errors


def summarize(data: dict) -> dict:
    payload = data.get("payload") or {}
    return {
        "dataset_version_recorded": data.get("datasetVersion"),
        "fixed_courses": len(payload.get("fixed") or []),
        "wishlist_courses": len(payload.get("courses") or []),
        "choice_groups": len(payload.get("choiceGroups") or []),
        "done_electives": len(payload.get("doneElectives") or []),
        "has_credit_policy": bool(payload.get("creditPolicy")),
        "has_profile": bool(payload.get("profile")),
        "programme": (payload.get("profile") or {}).get("programme"),
        "has_degree_audit_overrides": bool(payload.get("auditRequirements")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="path to the profile bootstrap JSON file")
    ap.add_argument("--stage", action="store_true",
                   help="copy the validated file to this user's app-data directory as "
                        "user_profile.local.json (same location the desktop app uses)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"ERROR: {src} does not exist", file=sys.stderr)
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: {src} is not valid JSON: {e}", file=sys.stderr)
        return 1

    errors = validate(data)
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    summary = summarize(data)
    print("Validation OK. Summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    if args.stage:
        dest = _appdata_dir() / "user_profile.local.json"
        if dest.exists():
            backup = dest.with_name(f"user_profile.local.backup-{int(time.time())}.json")
            dest.replace(backup)
            print(f"Existing staged profile backed up to {backup}")
        dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Staged at: {dest}")
        print("Launch the app normally (web: use the Profile tab's Import button on this "
             "file directly; desktop: the app offers to load this on first run).")
    else:
        print("Dry run (pass --stage to copy into this user's app-data directory).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
