"""Normalizes raw timetable rows into the scheduler's canonical course/package
dataset. This is the one place package construction happens - both the CLI
importer and the backend update service call this module, so there is
exactly one implementation of "how do LEC/TUT/PRAC sections combine into a
valid package" (see CLAUDE.md for why: package construction is where a
subtle divergence would be most dangerous).
"""
from __future__ import annotations

from app.domain.catalog import canonical_checksum
from app.timetable_updates.models import ImportStats, NormalizeResult
from app.timetable_updates.validate import DAYS, to_minutes, validate_normalized, validate_raw_row

MAJOR_DEPT_PREFIX = "CSD"


def _term_overlap(a: str, b: str) -> bool:
    return a == b or a == "Full semester" or b == "Full semester"


def _meetings_overlap(a: tuple, b: tuple, term_a: str, term_b: str) -> bool:
    return a[0] == b[0] and _term_overlap(term_a, term_b) and a[1] < b[2] and b[1] < a[2]


def _day_index(d: str) -> int:
    return DAYS.index(d.strip())


def flatten_rows(data: dict, stats: ImportStats) -> list[dict]:
    """dept -> cohort-group -> rows, deduplicated by rowid first: the site
    lists the same physical meeting once per cohort it's relevant to."""
    seen_rowids: set = set()
    rows: list[dict] = []
    dropped_dupes = 0
    for dept3, groups in data.items():
        if not isinstance(groups, dict):
            stats.warn("BAD_GROUP_SHAPE", f"department {dept3!r} value is not an object; skipped")
            continue
        for group_key, group_rows in groups.items():
            if not isinstance(group_rows, list):
                stats.warn("BAD_ROWS_SHAPE", f"{dept3}/{group_key} is not an array; skipped")
                continue
            for r in group_rows:
                rid = r.get("rowid")
                if rid is not None and rid in seen_rowids:
                    dropped_dupes += 1
                    continue
                if rid is not None:
                    seen_rowids.add(rid)
                r = dict(r)
                r["_dept3"] = dept3
                r["_group"] = group_key
                rows.append(r)
    if dropped_dupes:
        stats.warn("MULTI_COHORT_ROW_DEDUPED",
                  f"{dropped_dupes} row(s) were listed under more than one cohort group with an "
                  f"identical rowid; collapsed to a single row each")
    stats.raw_rows = len(rows)
    return rows


def build_packages(rows: list[dict], code: str, stats: ImportStats) -> list[dict]:
    """Cross-product across every present component's distinct sections,
    rejecting internally-conflicting combinations and deduplicating identical
    meeting sets."""
    by_comp: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        by_comp.setdefault(r["comp"], {}).setdefault(r["sec"], []).append(r)

    comps = sorted(by_comp.keys())
    if not comps:
        return []

    def meetings_for(comp: str, sec: str) -> list[tuple]:
        return [(_day_index(r["day"]), to_minutes(r["start"]), to_minutes(r["end"]), comp, sec, r.get("room") or "")
               for r in by_comp[comp][sec]]

    sec_term: dict[tuple[str, str], str] = {}
    for comp, secs in by_comp.items():
        for sec, sec_rows in secs.items():
            sec_term[(comp, sec)] = sec_rows[0].get("term") or "Full semester"

    sections_per_comp = [sorted(by_comp[c].keys()) for c in comps]

    def cross(i: int, chosen: list[tuple[str, str]]):
        if i == len(comps):
            yield list(chosen)
            return
        for sec in sections_per_comp[i]:
            chosen.append((comps[i], sec))
            yield from cross(i + 1, chosen)
            chosen.pop()

    packages = []
    seen_meeting_sets: set[tuple] = set()
    for combo in cross(0, []):
        all_meetings: list[tuple] = []
        for comp, sec in combo:
            all_meetings.extend(meetings_for(comp, sec))
        conflict = False
        for a in range(len(all_meetings)):
            for b in range(a + 1, len(all_meetings)):
                ma, mb = all_meetings[a], all_meetings[b]
                if ma[3] == mb[3] and ma[4] == mb[4]:
                    continue
                if _meetings_overlap(ma, mb, sec_term[(ma[3], ma[4])], sec_term[(mb[3], mb[4])]):
                    conflict = True
                    break
            if conflict:
                break
        if conflict:
            continue
        key = tuple(sorted(all_meetings))
        if key in seen_meeting_sets:
            continue
        seen_meeting_sets.add(key)
        pkg_term = sec_term[combo[0]]
        label = " + ".join(f"{comp}:{sec}" for comp, sec in combo)
        packages.append({"t": pkg_term, "l": label, "m": [list(m) for m in sorted(all_meetings)]})
    if not packages:
        stats.error("NO_VALID_PACKAGE", f"every component combination for {code} internally clashes", code)
    stats.packages_built += len(packages)
    return packages


def _code_parts(code: str) -> set[str]:
    return {p.strip() for p in code.split("/") if p.strip()}


def find_existing_match(code: str, existing_by_code: dict[str, dict]) -> dict | None:
    """Exact match first; falls back to a rename match (shared '/'-separated
    code component, e.g. CCC2101 -> CCC826/CCC2101) so a renamed course still
    carries forward its known credits/category/school/dept."""
    if code in existing_by_code:
        return existing_by_code[code]
    parts = _code_parts(code)
    for old_code, old_course in existing_by_code.items():
        if _code_parts(old_code) & parts:
            return old_course
    return None


def derive_category(dept3: str, ttype: str, uwe_flag: bool) -> str:
    if ttype == "CCC":
        return "CCC"
    if ttype == "UWE" or uwe_flag:
        return "UWE"
    if dept3 == MAJOR_DEPT_PREFIX and ttype == "Major":
        return "CORE"
    if dept3 == MAJOR_DEPT_PREFIX and ttype == "Major Elective":
        return "ME"
    return "NB"


def normalize(data: dict, existing_by_code: dict[str, dict]) -> NormalizeResult:
    stats = ImportStats()
    rows = flatten_rows(data, stats)
    good_rows = [r for r in rows if validate_raw_row(r, stats)]

    by_code: dict[str, list[dict]] = {}
    for r in good_rows:
        by_code.setdefault(r["code"], []).append(r)
    stats.distinct_courses = len(by_code)

    courses = []
    provenance: dict[str, dict] = {}
    for code, code_rows in sorted(by_code.items()):
        first = code_rows[0]
        dept3 = first["_dept3"]
        ttype_raw = first.get("type") or "Major"
        uwe_flag = str(first.get("uwe", "")).strip().lower() == "yes"

        title = next((r["title"].strip() for r in code_rows if r.get("title", "").strip()), "")
        existing = find_existing_match(code, existing_by_code)
        if not title:
            existing_title = (existing or {}).get("title", "")
            if existing_title:
                title = existing_title
                stats.warn("TITLE_MISSING_CARRIED_FORWARD",
                          f"{code}: no row in the revised timetable carries a title; "
                          f"carried forward {existing_title!r} from the previous dataset", code)
            else:
                title = f"NEEDS MANUAL REVIEW ({code})"
                stats.warn("TITLE_MISSING_NO_FALLBACK",
                          f"{code}: no title anywhere in the revised timetable and no previous "
                          f"dataset match to carry one forward", code)

        blocks_raw = sorted({r.get("block", "").strip() for r in code_rows if r.get("block", "").strip()})
        terms = sorted({r.get("term") or "Full semester" for r in code_rows})
        seats_vals = [float(r["cap"]) for r in code_rows if r.get("cap") not in (None, "")]
        seats = int(max(seats_vals)) if seats_vals else 0

        field_status = {}
        if existing:
            stats.matched_existing += 1
            school = existing.get("school", "")
            dept_full = existing.get("dept", "")
            cat = existing.get("cat")
            ttype = existing.get("ttype", ttype_raw)
            cr = existing.get("cr")
            cr_official = existing.get("crOfficial", False)
            cr_basis = existing.get("crBasis", "")
            if existing.get("code") != code:
                stats.warn("COURSE_CODE_RENAMED",
                          f"{code}: matched previous dataset entry {existing.get('code')!r} by shared "
                          f"code component (rename/recode); credits/category carried forward from it", code)
            field_status["category_credits_identity"] = "carried_forward_from_previous_workbook_dataset"
        else:
            stats.unmatched_new += 1
            stats.warn("NEW_COURSE_NEEDS_REVIEW",
                      f"{code} is not in the previously bundled dataset; category/credits are best-effort "
                      f"and need manual confirmation", code)
            school = ""
            dept_full = ""
            cat = derive_category(dept3, ttype_raw, uwe_flag)
            ttype = ttype_raw
            cr = None
            cr_official = False
            cr_basis = "NEEDS_MANUAL_REVIEW: course not present in the prior dataset; no credit source available"
            field_status["category_credits_identity"] = "best_effort_new_course_needs_review"

        pk = build_packages(code_rows, code, stats)
        courses.append({
            "code": code, "title": title, "school": school, "dept": dept_full,
            "ttype": ttype, "uwe": bool(uwe_flag), "cr": cr, "crOfficial": cr_official,
            "crBasis": cr_basis, "terms": terms, "blocks": blocks_raw, "cat": cat,
            "seats": seats, "unsched": len(pk) == 0,
            "why": "no internally-consistent section combination exists in the revised timetable" if len(pk) == 0 else "",
            "pk": pk,
        })
        provenance[code] = {
            "source_rowids": sorted({r.get("rowid") for r in code_rows if r.get("rowid") is not None}),
            "dept3": dept3, "field_status": field_status,
            "matched_previous_dataset": existing is not None,
        }

    validate_normalized(courses, stats)
    return NormalizeResult(courses=courses, provenance=provenance,
                           normalized_hash=canonical_checksum(courses), stats=stats)
