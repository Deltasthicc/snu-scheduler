"""Desktop entry point: serves the API and the built frontend from one process
on one port, and opens it in a native window (falls back to the default
browser if pywebview or the system WebView2 runtime isn't available).

This is the file PyInstaller freezes into the distributable .exe. It is not
used by the Docker image or the local dev workflow (those keep the frontend
and backend on separate ports, per scripts/start-local.sh) - it exists only
to make "double-click one file, the app opens" possible for someone who has
neither Python nor Node installed.
"""
from __future__ import annotations
import json
import multiprocessing
import os
import sys
import threading
import time
import urllib.request
import webbrowser


class _NullStream:
    """A --windowed PyInstaller build has no console, so sys.stdout/sys.stderr
    are None, not a dummy stream. Anything that touches them at all - uvicorn's
    own logging setup calls sys.stdout.isatty() while configuring its default
    formatter, before this app's code ever runs - crashes with
    AttributeError: 'NoneType' object has no attribute 'isatty'. Give both a
    real (if inert) file-like object instead of leaving them None."""
    def write(self, *a, **kw): pass
    def flush(self, *a, **kw): pass
    def isatty(self): return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

HOST = "127.0.0.1"
PORT = 8000  # same port the frontend's own default API base already assumes


def _frontend_index_path() -> str:
    """Location of the single self-contained frontend bundle. In the frozen
    exe it's a PyInstaller data file under sys._MEIPASS; in a normal source
    checkout it's the file build_frontend.py already produces."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "frontend_dist", "index.html")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "frontend", "dist", "index.html"))


def _wait_until_ready(url: str, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _appdata_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SNU Scheduler")
    os.makedirs(d, exist_ok=True)
    return d


def _selftest_log_path() -> str:
    return os.path.join(_appdata_dir(), "worker_selftest.log")


def _log(path: str, msg: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{time.time():.3f} {msg}\n")


def _selftest_child_hello(log_path: str) -> None:
    """Trivial spawned-process smoke test with no OR-Tools import at all -
    isolates whether spawning itself still works in a frozen exe once
    OR-Tools is bundled in, separate from whether OR-Tools' own import
    survives being spawned."""
    _log(log_path, f"CHILD(hello): alive. frozen={getattr(sys, 'frozen', False)} "
                   f"meipass={getattr(sys, '_MEIPASS', None)} exe={sys.executable} pid={os.getpid()}")


def _selftest_child_ortools(log_path: str) -> None:
    try:
        _log(log_path, "CHILD(ortools): importing ortools ...")
        import ortools
        _log(log_path, f"CHILD(ortools): imported, version={getattr(ortools, '__version__', '?')}")
        from ortools.sat.python import cp_model
        _log(log_path, "CHILD(ortools): cp_model imported")
        m = cp_model.CpModel()
        x = m.NewBoolVar("x")
        m.Add(x == 1)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        status = solver.Solve(m)
        _log(log_path, f"CHILD(ortools): solve status={status} value={solver.Value(x)}")
        _log(log_path, "CHILD(ortools): SUCCESS")
    except BaseException:
        import traceback
        _log(log_path, "CHILD(ortools): FAILED:\n" + traceback.format_exc())


def run_selftest() -> int:
    """`--selftest-worker`: diagnoses exactly where the frozen wishlist-search
    worker dies, without needing the full FastAPI/job-manager stack. Writes to
    %LOCALAPPDATA%\\SNU Scheduler\\worker_selftest.log since a --windowed
    build's own stdout is unreliable (see the _NullStream note above) and a
    SECOND-level spawned child's stdout is even less trustworthy to capture."""
    log_path = _selftest_log_path()
    open(log_path, "w", encoding="utf-8").close()
    _log(log_path, f"MAIN: frozen={getattr(sys, 'frozen', False)} meipass={getattr(sys, '_MEIPASS', None)} "
                   f"exe={sys.executable} argv={sys.argv} pid={os.getpid()}")

    _log(log_path, "MAIN: running ortools import directly, unspawned ...")
    _selftest_child_ortools(log_path)

    ctx = multiprocessing.get_context("spawn")
    _log(log_path, "MAIN: spawning trivial hello-world child (no ortools import) ...")
    p1 = ctx.Process(target=_selftest_child_hello, args=(log_path,))
    p1.start()
    p1.join(timeout=20)
    _log(log_path, f"MAIN: hello child exitcode={p1.exitcode} alive={p1.is_alive()}")

    _log(log_path, "MAIN: spawning ortools-import child ...")
    p2 = ctx.Process(target=_selftest_child_ortools, args=(log_path,))
    p2.start()
    p2.join(timeout=30)
    _log(log_path, f"MAIN: ortools child exitcode={p2.exitcode} alive={p2.is_alive()}")

    print(f"selftest complete; see {log_path}")
    return 0


PROFILE_SCHEMA_VERSION = 1


def run_import_profile(path: str) -> int:
    """`--import-profile <path>`: validates a user_profile.local.json bootstrap
    file and copies it into this user's per-user app-data directory (never
    beside the exe in Program Files - that location isn't guaranteed writable
    and personal data has no business sitting next to the installed binary).
    The app itself (on next normal launch) is what actually offers to load it
    into a plan; this command only validates and stages the file."""
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        print(f"ERROR: could not read {path}: {e}")
        return 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON: {e}")
        return 1
    if not isinstance(data, dict):
        print("ERROR: profile bootstrap must be a JSON object at the top level")
        return 1
    sv = data.get("schemaVersion")
    if not isinstance(sv, int):
        print("ERROR: profile bootstrap is missing an integer 'schemaVersion' field")
        return 1
    if sv > PROFILE_SCHEMA_VERSION:
        print(f"ERROR: profile bootstrap schema {sv} is newer than this app supports "
             f"({PROFILE_SCHEMA_VERSION}); update the app first")
        return 1
    if "profile" not in data:
        print("ERROR: profile bootstrap is missing the top-level 'profile' object")
        return 1

    dest_dir = _appdata_dir()
    dest = os.path.join(dest_dir, "user_profile.local.json")
    if os.path.exists(dest):
        backup = dest + f".backup-{int(time.time())}.json"
        os.replace(dest, backup)
        print(f"Existing profile at {dest} backed up to {backup}")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2))
    print(f"Validated and staged profile bootstrap at: {dest}")
    print("Launch the app normally; first run will offer to load it into a plan.")
    return 0


def main() -> int:
    if getattr(sys, "frozen", False):
        # catalog.py's default path is relative to its own __file__, which
        # is not a reliable real filesystem path once PyInstaller freezes
        # the package; point it at the extracted data file explicitly.
        # app/timetable_updates/apply.py derives ITS paths from these same
        # two env vars via catalog._DATA_PATH/_MANIFEST_PATH (not from
        # __file__ independently - a real bug, found and fixed 2026-08-04,
        # came from exactly that kind of duplication), so both must be set
        # for the timetable-update service to find its own active version
        # inside a frozen build.
        os.environ.setdefault(
            "SNU_CATALOG_PATH", os.path.join(sys._MEIPASS, "app", "data", "courses.json"))
        os.environ.setdefault(
            "SNU_DATASET_MANIFEST_PATH", os.path.join(sys._MEIPASS, "app", "data", "dataset_manifest.json"))

    from fastapi.responses import HTMLResponse
    from app.main import app

    index_path = _frontend_index_path()
    if not os.path.isfile(index_path):
        print(f"ERROR: frontend bundle not found at {index_path}")
        print("Run 'python build_frontend.py' in frontend/ before launching the desktop app.")
        return 1
    index_html = open(index_path, encoding="utf-8").read()

    @app.get("/", include_in_schema=False)
    async def _frontend_index():
        return HTMLResponse(index_html)

    import uvicorn
    # log_config=None: skip uvicorn's own logging.dictConfig entirely. app.main
    # already installs its own JSON log handler; uvicorn's default formatter
    # setup is also what crashes when sys.stdout has no real console behind it.
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning",
                                           log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://{HOST}:{PORT}/"
    if not _wait_until_ready(base_url + "health/ready"):
        print("ERROR: backend did not become ready in time.")
        return 1

    try:
        import webview
        window = webview.create_window(
            "SNU Bid & Schedule Simulator — Monsoon 2026",
            base_url, width=1360, height=900, min_size=(1000, 700))
        webview.start()
    except Exception as e:
        print(f"Native window unavailable ({e}); opening in your default browser instead.")
        webbrowser.open(base_url)
        # a --windowed build has no console/stdin to read a keypress from, so
        # block on the server thread itself instead of input(); closing the
        # browser tab does not stop this process (there's no window to detect
        # that from without a console) - the user quits via the console/Task
        # Manager in that fallback case, same as any other background server.
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    server.should_exit = True
    return 0


if __name__ == "__main__":
    # required for a frozen (PyInstaller) exe: simulation/schedule-search jobs
    # spawn worker processes, and a frozen exe re-executes itself as the
    # interpreter for those workers. freeze_support() detects that case (via
    # the args multiprocessing's spawn passes) and runs the worker bootstrap
    # instead of main() -- without it, each worker relaunches this whole app
    # and crashes trying to rebind the same port instead of doing any work.
    multiprocessing.freeze_support()
    if "--selftest-worker" in sys.argv:
        sys.exit(run_selftest())
    if "--import-profile" in sys.argv:
        _i = sys.argv.index("--import-profile")
        if _i + 1 >= len(sys.argv):
            print("ERROR: --import-profile requires a file path argument")
            sys.exit(1)
        sys.exit(run_import_profile(sys.argv[_i + 1]))
    sys.exit(main())
