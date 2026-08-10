#!/usr/bin/env python3
"""Assemble the thin API-driven frontend.

Deliberately does NOT bundle the heavy compute modules that used to run in the
browser (simulate / competition / robust / optimize) or the authoritative
domain engine. Only a clash-preview helper remains client-side.
"""
import os, re, shutil, sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("SNU_FRONTEND_OUT", os.path.join(BASE, "dist", "index.html"))

# client-side modules: adapter, plan store, clash preview, glue. No engines.
CLIENT = ["src/api.js", "src/plans.js", "src/clash.js"]
GLUE = "src/glue.js"
UI = ["src/ui/a_head.html", "src/ui/b_body.html", "src/ui/c_core.html", "src/ui/d_sched.html",
      "src/ui/i_build.html", "src/ui/e_tt.html", "src/ui/h_spec.html", "src/ui/k_learn.html",
      "src/ui/j_minor.html", "src/ui/g_two.html"]
DATA = os.path.join(BASE, "src", "data.json")
DOCS = os.path.join(BASE, "src", "docs")
STATIC = os.path.join(BASE, "src", "static")

# every browser-side computation path that must NOT survive the migration
# Any REFERENCE to a removed engine is a bug, not just a definition. An earlier
# version only checked for definitions and shipped a dangling `ROBUST.PRIORITY`
# call that threw at runtime on the Course-picker tab.
#
# enumerateSchedules/scheduleStats: the schedule-search backtracking search
# that used to run synchronously on the browser main thread (measured to block
# a real tab for ~100s at its old default budget) now runs server-side only,
# see backend/app/services/scheduler.py. If either of these is ever redefined
# client-side again, that heavy search is leaking back into the browser.
#
# nodes>400000: the literal bound of autoSolve()'s old in-browser branch-and-
# bound clash search (same category of bug, smaller scale). autoSolve() now
# calls the backend's search_with_fallback instead - this token would only
# reappear if that heavy search got reintroduced client-side.
FORBIDDEN_TOKENS = ["SIMULATE.", "OPTIMIZE.", "ROBUST.", "COMPETITION.", "ENGINE.",
                    "enumerateSchedules(", "scheduleStats(", "nodes>400000"]
FORBIDDEN_GLOBALS = ["g.SIMULATE", "g.OPTIMIZE", "g.ROBUST", "g.COMPETITION", "g.ENGINE",
                     "root.SIMULATE", "root.OPTIMIZE", "root.ROBUST", "root.COMPETITION"]

_auto = [0]

def read(p):
    with open(os.path.join(BASE, p), encoding="utf-8") as f:
        return f.read()

def a11y(html):
    """Associate every <label> with its control; make scroll regions keyboard-reachable."""
    pat = re.compile(r'<label([^>]*)>(.*?)</label>\s*(<(?:input|select|textarea)\b)([^>]*?)(/?>)', re.S)
    def fix(m):
        la, lt, tag, attrs, close = m.groups()
        if "for=" in la:
            return m.group(0)
        idm = re.search(r'\bid="([^"]+)"', attrs)
        if idm:
            cid = idm.group(1)
        else:
            _auto[0] += 1
            cid = f"a11y-{_auto[0]}"
            attrs = f' id="{cid}"' + attrs
        return f'<label{la} for="{cid}">{lt}</label>{tag}{attrs}{close}'
    html = pat.sub(fix, html)
    def bare(m):
        tag, attrs, close = m.group(1), m.group(2), m.group(3)
        if "aria-label" in attrs or 'type="hidden"' in attrs:
            return m.group(0)
        idm = re.search(r'\bid="([^"]+)"', attrs)
        if not idm or f'for="{idm.group(1)}"' in html:
            return m.group(0)
        tm = re.search(r'\btitle="([^"]+)"', attrs)
        name = tm.group(1) if tm else idm.group(1).replace("-", " ")
        return f'<{tag}{attrs} aria-label="{name}"{close}'
    html = re.sub(r'<(input|select|textarea)\b([^>]*?)(/?>)', bare, html)
    html = html.replace('<div class="scroll">',
                        '<div class="scroll" tabindex="0" role="region" aria-label="Scrollable table">')
    html = html.replace('<div class="ttwrap" id="ttGrid">',
                        '<div class="ttwrap" id="ttGrid" tabindex="0" role="region" aria-label="Weekly timetable grid">')
    return html

def main():
    parts = [read(p) for p in UI]
    html = "".join(parts)
    client_js = "\n".join(f"/* ===== {m} ===== */\n{read(m)}" for m in CLIENT)
    glue_js = f"/* ===== {GLUE} ===== */\n{read(GLUE)}"
    html = html.replace("</div><script>",
                        "</div>\n<script>\n/* client-injected */\n" + client_js + "\n", 1)
    # glue goes last so it can override the old inline handlers
    html = html.replace("</script></body></html>", "\n" + glue_js + "\n</script></body></html>")

    head, sep, tail = html.partition("<script>")
    html = a11y(head) + sep + tail

    with open(DATA, encoding="utf-8") as f:
        html = html.replace("__DATA__", f.read())

    if "__DATA__" in html:
        sys.exit("ERROR: course data placeholder not substituted")
    leaked = [t for t in FORBIDDEN_TOKENS if t in html] + [t for t in FORBIDDEN_GLOBALS if t in html]
    if leaked:
        sys.exit("ERROR: removed compute engine still referenced in the bundle: " + ", ".join(leaked))
    for need in ("g.API = factory", "g.PLANS = factory", "g.CLASH = factory"):
        if need not in html:
            sys.exit("ERROR: client module missing from bundle: " + need)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    docs_out = os.path.join(os.path.dirname(OUT), "docs")
    if os.path.isdir(DOCS):
        shutil.copytree(DOCS, docs_out, dirs_exist_ok=True)
    # robots.txt / 404.html etc: plain files served as-is at the site root by
    # both the FastAPI StaticFiles mount (app/main.py) and the nginx image
    # (frontend/Dockerfile), since both simply serve everything under dist/.
    if os.path.isdir(STATIC):
        for name in os.listdir(STATIC):
            shutil.copy2(os.path.join(STATIC, name), os.path.join(os.path.dirname(OUT), name))
    print(f"built {OUT}  ({len(html):,} bytes)")
    print(f"  client modules: {len(CLIENT)} + glue")
    print(f"  ui layers:      {len(UI)}")
    if os.path.isdir(DOCS):
        print(f"  source docs:    {len(os.listdir(DOCS))}")
    if os.path.isdir(STATIC):
        print(f"  static assets:  {len(os.listdir(STATIC))} ({', '.join(sorted(os.listdir(STATIC)))})")
    print("  verified: no simulation/optimisation engine defined OR referenced in the bundle")
    return 0

if __name__ == "__main__":
    sys.exit(main())
