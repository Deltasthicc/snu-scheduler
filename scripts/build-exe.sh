#!/usr/bin/env bash
# Builds the Windows desktop app as a one-folder distribution
# (backend/dist/SNU-Bid-Scheduler/), then zips it for distribution.
# Run from the repo root. Requires pyinstaller + pywebview installed in the
# same Python environment as the backend's own requirements.txt.
#
# --onedir, not --onefile: measured directly on 2026-08-04 after fixing the
# OR-Tools frozen-worker crash (see CLAUDE.md and
# docs/DESKTOP_PACKAGING.md) - onedir cold-starts in ~1.4s vs onefile's
# ~12.7s, because onefile re-extracts its whole ~90MB archive to a fresh temp
# directory on every single launch while onedir just runs the already-
# unpacked files. Onedir's on-disk footprint is larger (~215MB vs ~90MB) but
# a slower, larger single file is not a better user experience than a
# faster, larger folder - see request section 19's own framing. The old
# onefile command is kept below, commented out, as a documented fallback:
# it does still work correctly after the same source fix, just slower to
# start every time.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$HERE/backend/dist/SNU-Bid-Scheduler"

echo "==> building the frontend bundle"
(cd "$HERE/frontend" && python3 build_frontend.py)

echo "==> freezing the desktop app with PyInstaller (--onedir)"
# dataset_manifest.json + timetable_versions/ are required, not optional:
# without them the frozen app cannot identify its own active dataset version
# (found by actually running the packaged exe and checking /api/v1/timetable-updates/status,
# not assumed - see CLAUDE.md). onedir's data directory is a real, writable,
# persistent folder (not a per-launch temp extraction like onefile), so the
# timetable-update service's own writes (staged candidates, applied versions)
# survive an app restart correctly.
(cd "$HERE/backend" && python3 -m PyInstaller --onedir --windowed --name SNU-Bid-Scheduler \
  --add-data "app/data/courses.json;app/data" \
  --add-data "app/data/programs.json;app/data" \
  --add-data "app/data/pathways.json;app/data" \
  --add-data "app/data/course_outlines.json;app/data" \
  --add-data "app/data/dataset_manifest.json;app/data" \
  --add-data "app/data/timetable_versions;app/data/timetable_versions" \
  --add-data "../frontend/dist/index.html;frontend_dist" \
  --collect-all uvicorn \
  --collect-all ortools \
  --collect-all pypdf \
  --collect-all cryptography \
  --exclude-module pytest \
  --exclude-module hypothesis \
  --exclude-module httpx \
  --hidden-import app.main \
  --noconfirm \
  desktop_launcher.py)

echo "==> zipping the portable distribution"
(cd "$HERE/backend/dist" && rm -f SNU-Bid-Scheduler-portable.zip && \
 python3 -c "import shutil; shutil.make_archive('SNU-Bid-Scheduler-portable', 'zip', '.', 'SNU-Bid-Scheduler')")

echo
echo "Built: $DIST/  (run SNU-Bid-Scheduler.exe inside it)"
echo "Zipped: $HERE/backend/dist/SNU-Bid-Scheduler-portable.zip"
echo
echo "--onefile fallback (slower startup, single file), if ever needed:"
echo "  python3 -m PyInstaller --onefile --windowed --name SNU-Bid-Scheduler \\"
echo "    --add-data \"app/data/courses.json;app/data\" \\"
echo "    --add-data \"../frontend/dist/index.html;frontend_dist\" \\"
echo "    --collect-all uvicorn --collect-all ortools --hidden-import app.main \\"
echo "    --noconfirm desktop_launcher.py"
