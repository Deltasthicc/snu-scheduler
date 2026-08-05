"""Safe extraction of the `const DATA = {...};` literal from the timetable
site's HTML. Never executes JavaScript - isolates the literal by brace
matching (string-escape aware), scans it for executable-looking tokens, then
parses with `json.loads` (the literal is already JSON-compatible: quoted
string keys, no function values - confirmed by round-tripping it during
initial investigation, see CLAUDE.md).
"""
from __future__ import annotations
import json
import re

from app.timetable_updates.models import ExtractResult
from app.timetable_updates.source import sha256_text

FORBIDDEN_TOKENS = ("function", "=>", "eval(", "new Function", "require(", "import(")


class ParseError(RuntimeError):
    pass


def extract_data_literal(html: str) -> str:
    m = re.search(r"\bconst\s+DATA\s*=", html)
    if not m:
        raise ParseError(
            "upstream structure changed: no 'const DATA =' assignment found in the page. "
            "The site's data-embedding approach has changed and this parser needs updating "
            "before it can be trusted again.")
    brace_start = html.find("{", m.end())
    if brace_start == -1:
        raise ParseError("found 'const DATA =' but no opening brace followed it")
    depth = 0
    in_str = False
    esc = False
    str_char = ""
    i = brace_start
    n = len(html)
    while i < n:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == str_char:
                in_str = False
        else:
            if c in ("'", '"'):
                in_str = True
                str_char = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    else:
        raise ParseError("unterminated DATA literal - brace matching never closed")
    literal = html[brace_start:i + 1]
    lowered = literal.lower()
    for tok in FORBIDDEN_TOKENS:
        if tok.lower() in lowered:
            raise ParseError(
                f"the isolated DATA literal contains a forbidden executable-looking token "
                f"({tok!r}); refusing to parse it. This parser never evaluates JavaScript.")
    return literal


def parse(html: str) -> ExtractResult:
    literal = extract_data_literal(html)
    try:
        parsed = json.loads(literal)
    except json.JSONDecodeError as e:
        raise ParseError(f"DATA literal is not valid JSON once isolated: {e}") from e
    return ExtractResult(raw_literal=literal, extracted_hash=sha256_text(literal), parsed=parsed)
