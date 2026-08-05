#!/usr/bin/env python3
"""Copy the frontend's course catalog into the backend package.

frontend/src/data.json is the single source of truth (it is what the university
workbooks were extracted into). The backend needs its own copy inside its Docker
build context so schedule search has authoritative course/package/meeting data
without depending on whatever the browser happens to send. Run this after any
change to frontend/src/data.json; tests/test_catalog.py fails the build if the
two copies drift out of sync.
"""
from __future__ import annotations
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND_SRC = BACKEND.parent / "frontend" / "src" / "data.json"
BACKEND_DST = BACKEND / "app" / "data" / "courses.json"


def main() -> int:
    if not FRONTEND_SRC.exists():
        print(f"ERROR: source not found: {FRONTEND_SRC}", file=sys.stderr)
        return 1
    BACKEND_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FRONTEND_SRC, BACKEND_DST)
    print(f"synced {FRONTEND_SRC} -> {BACKEND_DST} ({BACKEND_DST.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
