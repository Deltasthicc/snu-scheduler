"""Diffing two normalized course datasets: added/removed/renamed/changed.

Uses code-keyed dict lookups throughout (O(old + new)), not nested course-to-
course comparison - rename detection uses a targeted secondary index over
'/'-separated code components, not an O(n^2) scan.
"""
from __future__ import annotations


def _code_parts(code: str) -> set[str]:
    return {p.strip() for p in code.split("/") if p.strip()}


def detect_renames(added: list[str], removed: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """O(added + removed) via a part->code index over the (usually small)
    removed list, not a full cross product."""
    removed_index: dict[str, str] = {}
    for r in removed:
        for part in _code_parts(r):
            removed_index[part] = r

    renames = []
    matched_added, matched_removed = set(), set()
    for a in added:
        for part in _code_parts(a):
            r = removed_index.get(part)
            if r is not None and r not in matched_removed:
                renames.append({"old_code": r, "new_code": a})
                matched_added.add(a)
                matched_removed.add(r)
                break
    remaining_added = [a for a in added if a not in matched_added]
    remaining_removed = [r for r in removed if r not in matched_removed]
    return renames, remaining_added, remaining_removed


def diff_datasets(old: list[dict], new: list[dict]) -> dict:
    old_by_code = {c["code"]: c for c in old}
    new_by_code = {c["code"]: c for c in new}
    old_codes, new_codes = set(old_by_code), set(new_by_code)

    added_raw = sorted(new_codes - old_codes)
    removed_raw = sorted(old_codes - new_codes)
    renames, added, removed = detect_renames(added_raw, removed_raw)
    changed = []

    for code in sorted(old_codes & new_codes):
        o, n = old_by_code[code], new_by_code[code]
        field_diffs = {}
        for f in ("title", "seats", "terms", "cat"):
            if o.get(f) != n.get(f):
                field_diffs[f] = {"old": o.get(f), "new": n.get(f)}
        old_labels = sorted(p["l"] for p in o.get("pk", []))
        new_labels = sorted(p["l"] for p in n.get("pk", []))
        if old_labels != new_labels:
            field_diffs["packages"] = {
                "removed_labels": sorted(set(old_labels) - set(new_labels)),
                "added_labels": sorted(set(new_labels) - set(old_labels)),
            }
        else:
            old_pk_by_label = {p["l"]: p for p in o.get("pk", [])}
            new_pk_by_label = {p["l"]: p for p in n.get("pk", [])}
            moved = [label for label in old_labels if old_pk_by_label[label]["m"] != new_pk_by_label[label]["m"]]
            if moved:
                field_diffs["packages_moved"] = moved
        if field_diffs:
            changed.append({"code": code, "diffs": field_diffs})

    return {
        "renamed_courses": renames, "added_courses": added, "removed_courses": removed,
        "changed_courses": changed,
        "summary": {"renamed": len(renames), "added": len(added), "removed": len(removed),
                   "changed": len(changed), "unchanged": len(old_codes & new_codes) - len(changed)},
    }


def affected_codes(diff: dict) -> set[str]:
    """Every course code touched by this diff in any way - used to scope
    plan-impact analysis to a small set instead of scanning every field of
    every saved plan (see app/timetable_updates/apply.py)."""
    codes: set[str] = set(diff["added_courses"]) | set(diff["removed_courses"])
    codes |= {r["old_code"] for r in diff["renamed_courses"]} | {r["new_code"] for r in diff["renamed_courses"]}
    codes |= {c["code"] for c in diff["changed_courses"]}
    return codes
