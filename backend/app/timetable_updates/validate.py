"""Structural validation for raw timetable rows and normalized courses."""
from __future__ import annotations
from datetime import datetime

from app.timetable_updates.models import ImportStats

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
REQUIRED_ROW_FIELDS = ("code", "comp", "sec", "term", "day", "start", "end")


def to_minutes(t: str) -> int:
    dt = datetime.strptime(t.strip(), "%I:%M %p")
    return dt.hour * 60 + dt.minute


def validate_raw_row(r: dict, stats: ImportStats) -> bool:
    missing = [f for f in REQUIRED_ROW_FIELDS if not r.get(f)]
    if missing:
        stats.error("MISSING_FIELD", f"row rowid={r.get('rowid')} missing {missing}", r.get("code"))
        return False
    if r["day"].strip() not in DAYS:
        stats.error("INVALID_DAY", f"unrecognized day {r['day']!r}", r["code"])
        return False
    try:
        s, e = to_minutes(r["start"]), to_minutes(r["end"])
    except ValueError:
        stats.error("INVALID_TIME", f"unparseable time {r['start']!r}/{r['end']!r}", r["code"])
        return False
    if e <= s:
        stats.error("END_BEFORE_START", f"end {r['end']} is not after start {r['start']}", r["code"])
        return False
    cap = r.get("cap")
    if cap not in (None, ""):
        try:
            capf = float(cap)
            if capf < 0:
                stats.error("NEGATIVE_SEATS", f"seat count {cap} is negative", r["code"])
                return False
            if capf > 2000:
                stats.warn("SUSPICIOUS_SEATS", f"seat count {cap} is implausibly large", r["code"])
        except ValueError:
            stats.warn("INVALID_SEATS", f"seat count {cap!r} is not numeric", r["code"])
    return True


def validate_normalized(courses: list[dict], stats: ImportStats) -> None:
    """Post-normalization checks: duplicate course codes, zero-package
    courses already flagged during package construction (see normalize.py)."""
    seen: dict[str, int] = {}
    for c in courses:
        seen[c["code"]] = seen.get(c["code"], 0) + 1
    for code, n in seen.items():
        if n > 1:
            stats.error("DUPLICATE_COURSE_CODE", f"{code} appears more than once in the normalized output", code)
