#!/usr/bin/env python3
"""Validate source provenance, catalog structure, and every audit path."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.models.audit_schemas import DegreeAuditRequest  # noqa: E402
from app.services.degree_audit import ProgrammeCatalog, audit_degree  # noqa: E402

CATALOG_PATH = ROOT / "backend" / "app" / "data" / "programs.json"
ALLOWED_HOSTS = {"snu.edu.in", "www.snu.edu.in", "snulinks.snu.edu.in"}
VERIFICATION = {"verified_public_curriculum", "verified_public_milestones", "source_linked_partial"}


def check_url(url: str) -> tuple[str, int | None, str]:
    try:
        request = Request(url, method="HEAD",
                          headers={"User-Agent": "Mozilla/5.0 SNU-Scheduler-Catalog-Audit/1.0"})
        with urlopen(request, timeout=30) as response:
            return url, response.status, response.headers.get_content_type()
    except HTTPError as exc:
        return url, exc.code, f"HTTP {exc.code}"
    except Exception as exc:
        return url, None, f"{type(exc).__name__}: {exc}"


def validate(check_urls: bool) -> int:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    programs = payload["programs"]
    errors: list[str] = []
    warnings: list[str] = []
    if payload.get("program_count") != len(programs):
        errors.append("program_count does not match the number of records")
    for key in ("id", "title", "official_page"):
        values = [program[key] for program in programs]
        if len(values) != len(set(values)):
            errors.append(f"duplicate programme {key}")

    catalog = ProgrammeCatalog(CATALOG_PATH)
    urls: set[str] = {payload["catalog_url"]}
    for program in programs:
        prefix = program["id"]
        verification = program.get("verification")
        requirements = program.get("requirements", [])
        if verification not in VERIFICATION:
            errors.append(f"{prefix}: invalid verification state {verification!r}")
        if verification != "source_linked_partial" and not requirements:
            errors.append(f"{prefix}: claims verification but has no requirements")
        if verification == "verified_public_curriculum" and not any(
            rule.get("kind", "credits") == "credits" for rule in requirements
        ):
            errors.append(f"{prefix}: verified curriculum has no credit rule")
        ids = [rule.get("id") for rule in requirements]
        if len(ids) != len(set(ids)):
            errors.append(f"{prefix}: duplicate requirement id")
        for rule in requirements:
            kind = rule.get("kind", "credits")
            if kind not in {"credits", "milestone"}:
                errors.append(f"{prefix}/{rule.get('id')}: unsupported kind {kind!r}")
            if float(rule.get("required", 0)) <= 0:
                errors.append(f"{prefix}/{rule.get('id')}: required must be positive")
        total = next((rule for rule in requirements if rule.get("id") == "total"), None)
        if total and not 24 <= float(total["required"]) <= 300:
            errors.append(f"{prefix}: implausible total-credit rule {total['required']}")

        for url in [program["official_page"], *[source["url"] for source in program.get("sources", [])]]:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
                errors.append(f"{prefix}: non-official or non-HTTPS source {url}")
            urls.add(url)
        try:
            result = audit_degree(DegreeAuditRequest(programme_id=prefix), catalog)
            if result["requirements_total"] != len(requirements):
                errors.append(f"{prefix}: audit response dropped requirements")
        except Exception as exc:
            errors.append(f"{prefix}: audit failed: {type(exc).__name__}: {exc}")

    if check_urls:
        # SNU rate-limits bursts. Keep this deliberately small and classify 429
        # as inconclusive instead of falsely declaring a published source dead.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(check_url, url) for url in sorted(urls)]
            for future in as_completed(futures):
                url, status, detail = future.result()
                if status == 429:
                    warnings.append(f"rate limited; reachability inconclusive: {url}")
                elif status is None or not 200 <= status < 400:
                    errors.append(f"source unreachable: {url} ({detail})")
                elif detail not in {"text/html", "application/pdf", "image/jpeg", "image/png"}:
                    warnings.append(f"unexpected content type: {url} ({detail})")

    counts: dict[str, int] = {}
    for program in programs:
        state = program["verification"]
        counts[state] = counts.get(state, 0) + 1
    print(f"programmes={len(programs)} verification={counts} urls={len(urls)}")
    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"result={'PASS' if not errors else 'FAIL'} errors={len(errors)} warnings={len(warnings)}")
    return 0 if not errors else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-urls", action="store_true", help="GET every official source URL")
    raise SystemExit(validate(parser.parse_args().check_urls))
