"""Normalizes raw timetable rows into the scheduler's canonical course/package
dataset. This is the one place package construction happens - both the CLI
importer and the backend update service call this module, so there is
exactly one implementation of "how do LEC/TUT/PRAC sections combine into a
valid package" (see CLAUDE.md for why: package construction is where a
subtle divergence would be most dangerous).
"""
from __future__ import annotations

import re

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


# Batch names follow one convention throughout the source: letters (dept),
# one digit (year), then the batch number - "CSD31" is CSD, year 3, batch 1;
# "CSD312" is CSD, year 3, batch 12. A "range" token spells out two of these
# and means every batch numbered in between, within the same dept+year (e.g.
# "CSD37 to CSD312" = CSD37, CSD38, ... CSD312). The naive read - treating the
# whole range string as one opaque token - was measured to break this: eight
# real courses (CSD304, CSD311, CSD319, ECE2001-2003, ECE301, ECE302) came
# back with zero valid packages, because every one of their range-tokens is a
# distinct literal string ("CSD31 to CSD36" vs "CSD31 to CSD33" vs "CSD37 to
# CSD312" ...) that never textually equals another, so nothing was ever seen
# as sharing a batch and every combination was rejected as incoherent.
_RANGE_TOKEN = re.compile(r"^([A-Za-z]+)(\d)(\d*)\s+to\s+([A-Za-z]+)(\d)(\d*)$", re.IGNORECASE)


def _expand_block_token(token: str) -> frozenset[str]:
    token = token.strip()
    if not token:
        return frozenset()
    m = _RANGE_TOKEN.match(token)
    if m:
        dept_a, year_a, num_a, dept_b, year_b, num_b = m.groups()
        if dept_a.upper() == dept_b.upper() and year_a == year_b:
            lo, hi = int(num_a or 0), int(num_b or 0)
            if 0 <= lo <= hi:
                return frozenset(f"{dept_a}{year_a}{n}" for n in range(lo, hi + 1))
        # Dept or year differ, or the range is inverted: not a range this
        # convention can safely expand. Falls through to the opaque-token
        # case below rather than guessing.
    return frozenset({token})


def _block_set(raw: str) -> frozenset[str]:
    """A row's 'block' field names which student batch(es) that specific
    section belongs to: a literal comma-separated list ("CSD21,CSD22, CSD23,
    CSD24"), a range ("CSD31 to CSD312"), a single batch ("CSD21"), or a
    whole-year label with no further split ("BIO4YR"). Empty means the source
    publishes no batch restriction for that section - open to whichever batch
    is asking, not "belongs to no one"."""
    result: frozenset[str] = frozenset()
    for part in raw.split(","):
        result |= _expand_block_token(part)
    return result


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
    meeting sets.

    A combination must also be BATCH-COHERENT, not just non-conflicting in
    time. The source tags many sections with which student batch(es) they
    belong to (e.g. CSD211's PRAC1 is "CSD21" only, its TUT2 is "CSD23,
    CSD24") - two sections restricted to disjoint batches can never be
    attended by the same real student, however far apart their meeting times
    sit. Cross-producting on time alone was measured to manufacture exactly
    this: CSD211's PRAC1 (CSD21-only) has no time conflict with TUT2 (CSD23/24
    -only), so the old logic offered "PRAC1 + TUT2" as a valid package - a
    combination no student who has ever existed could actually be enrolled in.
    A section with no batch tag at all is open to any batch (the source is
    simply not restricting it), so it is compatible with everything.
    """
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
    sec_batches: dict[tuple[str, str], frozenset[str]] = {}
    for comp, secs in by_comp.items():
        for sec, sec_rows in secs.items():
            sec_term[(comp, sec)] = sec_rows[0].get("term") or "Full semester"
            # Union across this section's own rows (its several meetings should
            # all name the same batch set, but union is the safe read if a
            # source row is ever inconsistent rather than silently picking one).
            batches: frozenset[str] = frozenset()
            for row in sec_rows:
                batches |= _block_set(row.get("block", ""))
            sec_batches[(comp, sec)] = batches

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
    incoherent_skipped = 0
    for combo in cross(0, []):
        # Batch coherence: every pair of *restricted* (non-empty) batch sets
        # in this combination must share at least one batch. An unrestricted
        # section imposes no constraint and is compatible with anything.
        restricted = [sec_batches[key] for key in combo if sec_batches[key]]
        common = restricted[0] if restricted else frozenset()
        incoherent = False
        for s in restricted[1:]:
            common &= s
            if not common:
                incoherent = True
                break
        if incoherent:
            incoherent_skipped += 1
            continue

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
        packages.append({"t": pkg_term, "l": label, "m": [list(m) for m in sorted(all_meetings)],
                         "batches": sorted(common)})
    if not packages:
        stats.error("NO_VALID_PACKAGE", f"every component combination for {code} internally clashes", code)
    if incoherent_skipped:
        stats.warn("BATCH_INCOHERENT_COMBOS_SKIPPED",
                  f"{code}: {incoherent_skipped} section combination(s) skipped because they mixed "
                  f"sections restricted to different, non-overlapping student batches", code)
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
