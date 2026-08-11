#!/usr/bin/env python3
"""CLI wrapper around the canonical timetable-update modules in
`backend/app/timetable_updates/`. This script does NOT reimplement fetching,
parsing, normalization, validation, or diffing - it calls the exact same
functions the backend's own background poller uses (see
backend/app/timetable_updates/poller.py), so the CLI and the running
application can never drift apart on what counts as a real timetable change.

Usage:
    python3 tools/import_netlify_timetable.py [--url URL] [--offline-html PATH]
                                              [--apply] [--out-dir DIR]

Without --apply, this only produces the snapshot/normalized dataset/reports
under backend/app/data/timetable_versions/<version>/ and leaves the active
dataset untouched. --apply also updates backend/app/data/dataset_manifest.json
to point at the new version (after it validates with zero errors) via the
same transactional apply_version() the backend service uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.timetable_updates import apply as apply_mod  # noqa: E402
from app.timetable_updates import normalize as normalize_mod  # noqa: E402
from app.timetable_updates import parser as parser_mod  # noqa: E402
from app.timetable_updates import source as source_mod  # noqa: E402
from app.timetable_updates.diff import diff_datasets  # noqa: E402

DEFAULT_URL = "https://snioe-monsoon2026-tt.netlify.app/"
FRONTEND_DATA = REPO_ROOT / "frontend" / "src" / "data.json"
VERSIONS_DIR = apply_mod.VERSIONS_DIR


def write_report_md(path: Path, title: str, body_lines: list[str]) -> None:
    path.write_text(f"# {title}\n\n" + "\n".join(body_lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--offline-html", default=None, help="use a locally saved HTML file instead of fetching")
    ap.add_argument("--apply", action="store_true", help="make this the active dataset after validation")
    ap.add_argument("--version-id", default=None, help="override the generated version id")
    args = ap.parse_args()

    print(f"==> fetching {args.offline_html or args.url}")
    if args.offline_html:
        html = Path(args.offline_html).read_text(encoding="utf-8")
        retrieved_at = datetime.now(timezone.utc).isoformat()
        source_checksum = source_mod.sha256_text(html)
    else:
        fetch = source_mod.fetch(args.url, force=True)  # CLI always wants a full fetch, never conditional
        if fetch.error:
            print(f"ERROR: could not fetch upstream timetable: {fetch.error}", file=sys.stderr)
            print("Keeping the existing dataset untouched; no update was applied.", file=sys.stderr)
            return 2
        html = fetch.html
        retrieved_at = fetch.retrieved_at
        source_checksum = fetch.source_hash

    print("==> isolating the DATA literal (no JS execution)")
    try:
        extracted = parser_mod.parse(html)
    except parser_mod.ParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    baseline_path = VERSIONS_DIR / "monsoon-2026-excel-v1" / "courses.json"
    if not baseline_path.exists():
        baseline_path = FRONTEND_DATA
    existing_by_code = {}
    if baseline_path.exists():
        existing_by_code = {c["code"]: c for c in json.loads(baseline_path.read_text(encoding="utf-8"))}
    print(f"==> diffing/carrying-forward against baseline: {baseline_path}")

    norm = normalize_mod.normalize(extracted.parsed, existing_by_code)
    stats = norm.stats

    version_id = args.version_id or (
        f"monsoon-2026-netlify-revision-{retrieved_at[:10]}-{source_checksum[:8]}"
    )
    manifest_entry = {
        "version_id": version_id, "source_name": "SNU Monsoon 2026 Timetable Planner (Netlify)",
        "source_url": args.url if not args.offline_html else f"offline:{args.offline_html}",
        "retrieved_at": retrieved_at, "source_checksum": source_checksum,
        "dataset_checksum": norm.normalized_hash, "importer_version": "1.0.0",
        "effective_semester": "Monsoon 2026", "course_count": len(norm.courses),
        "package_count": sum(len(c["pk"]) for c in norm.courses),
        "error_count": stats.error_count, "warning_count": stats.warning_count,
        "validation_status": "clean" if stats.error_count == 0 else "has_errors",
    }
    try:
        out_dir = apply_mod.stage_version(version_id, norm.courses, norm.provenance, manifest_entry)
    except apply_mod.ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    (out_dir / "raw_snapshot.html").write_text(html, encoding="utf-8")
    (out_dir / "raw_data_literal.json").write_text(json.dumps(extracted.parsed, indent=2, sort_keys=True),
                                                   encoding="utf-8")

    issues_lines = [f"- [{i.level.upper()}] `{i.code}`" + (f" ({i.course})" if i.course else "") + f": {i.message}"
                    for i in stats.issues]
    write_report_md(out_dir / "import_report.md", f"Import report — {version_id}", [
        f"Retrieved: {retrieved_at}", f"Source: {args.url}", f"Source checksum: `{source_checksum}`",
        f"Dataset checksum: `{norm.normalized_hash}`", "Importer version: `1.0.0`", "",
        "## Counts", f"- Raw rows: {stats.raw_rows}", f"- Distinct courses: {stats.distinct_courses}",
        f"- Packages built: {stats.packages_built}", f"- Matched previous dataset: {stats.matched_existing}",
        f"- New / unmatched courses: {stats.unmatched_new}",
        f"- Errors: {stats.error_count}", f"- Warnings: {stats.warning_count}", "",
        "## Issues" if issues_lines else "## Issues\n\nNone.",
    ] + issues_lines)

    old_courses = list(existing_by_code.values())
    diff = diff_datasets(old_courses, norm.courses)
    (out_dir / "diff.json").write_text(json.dumps(diff, indent=2), encoding="utf-8")

    print(f"==> {stats.distinct_courses} courses, {stats.packages_built} packages built, "
         f"{stats.error_count} errors, {stats.warning_count} warnings")
    print(f"==> diff vs previous dataset: {len(diff['renamed_courses'])} renamed, "
         f"+{len(diff['added_courses'])} added, -{len(diff['removed_courses'])} removed, "
         f"{len(diff['changed_courses'])} changed")
    print(f"==> wrote {out_dir}")

    if stats.error_count:
        print(f"ERROR: {stats.error_count} validation error(s); refusing to apply. See import_report.md.",
             file=sys.stderr)
        return 4

    if args.apply:
        result = apply_mod.apply_version(version_id, expected_checksum=norm.normalized_hash)
        print(f"==> APPLIED: {version_id} is now the active dataset "
             f"(frontend/src/data.json + backend/app/data/courses.json updated) - {result}")
    else:
        print("==> dry run (pass --apply to make this the active dataset)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
