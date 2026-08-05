# Desktop packaging — root cause, fix, architecture decision, measurements

Session: 2026-08-04. This is the record of a real correctness bug found, diagnosed,
and fixed in the Windows desktop build — not a hypothetical writeup.

## The bug

The desktop `.exe` (built with OR-Tools added for the wishlist scheduler) returned
`"worker process exited without producing a result"` for every wishlist search,
silently. No Python traceback, because there wasn't one to catch.

## Reproducing it with real evidence, not guessing

Added a `--selftest-worker` diagnostic flag to `backend/desktop_launcher.py` that:

1. Runs a trivial OR-Tools CP-SAT solve (`CpModel` → `NewBoolVar` → `Add` → `CpSolver.Solve`)
   directly in the main process, unspawned.
2. Spawns a trivial no-op child process (no OR-Tools import at all).
3. Spawns a second child that repeats step 1's solve.

All output goes to `%LOCALAPPDATA%\SNU Scheduler\worker_selftest.log`, not stdout —
a `--windowed` PyInstaller build has no console, so stdout is unreliable even with the
existing `_NullStream` shim, and a spawned child's stdout is even less trustworthy to
capture.

Running the built exe with `--selftest-worker`:

```
1785851006.456 MAIN: frozen=True meipass=...\_internal exe=...\SNU-Bid-Scheduler-onedir.exe argv=[...] pid=12368
1785851006.458 MAIN: running ortools import directly, unspawned ...
1785851006.458 CHILD(ortools): importing ortools ...
1785851012.242 CHILD(ortools): imported, version=9.15.6755
1785851019.104 CHILD(ortools): cp_model imported
```

Process then **segfaults** (exit code 139 / SIGSEGV). No further log lines are written
— the crash happens between `cp_model` import succeeding and the solver actually
running (`CpModel()` / `NewBoolVar` / `Add` / `CpSolver().Solve()`).

**This is not a multiprocessing/spawn issue.** The crash reproduces in the *main*
process, unspawned, before any child is even created. It is also not a missing-file
issue: `find`-comparing the frozen `_internal/ortools/` tree against the unfrozen
`site-packages/ortools/` tree shows byte-identical native file layout
(`ortools/.libs/{abseil_dll,libprotobuf,ortools,...}.dll`, every `.pyd`) — every DLL
OR-Tools needs is present and gets found at import time (import succeeds fully). The
crash is OR-Tools' compiled CP-SAT core (a pybind11 extension) segfaulting when
actually invoked — not when imported — specifically inside PyInstaller's frozen
bootstrap environment. This was not root-caused further at the binary/DLL-loader
level (would need a Windows debugger/crash-dump attached to the frozen process,
tooling not available in this session) — see "Remaining uncertainty" below.

## The fix

**A segfault cannot be caught by `try`/`except`.** It is a hard OS-level process
termination; no Python exception is ever raised. Wrapping the `Solve()` call in a
`try` block, as a first instinct might suggest, would never actually run the `except`
branch — the process is just gone. The only correct fix is to never make the call at
all when running frozen.

`backend/app/services/cp_scheduler.py` now has a module-level flag:

```python
RUNNING_FROZEN = bool(getattr(sys, "frozen", False))
```

and `solve()` branches on it: when true, it calls `_solve_greedy_fallback()` instead of
`_build_and_solve()` (which is the one function that ever calls `cp_model.CpSolver().Solve()`).
The fallback is pure Python — forced/must-have items first, then remaining items by
intent and priority, greedily added if they fit without a conflict and within the
credit ceiling — reported honestly as `cp_status: "heuristic_fallback"`, never claimed
to be the exact CP-SAT result. It cannot segfault because it never touches the native
extension.

This means: **in the web app / Docker deployment (not frozen), nothing changed** —
CP-SAT runs exactly as before, all existing tests pass unchanged. **Only the frozen
desktop build** takes the fallback path. Verified directly:

```
$ curl -X POST http://127.0.0.1:8000/api/v1/schedules/search ... (through the rebuilt exe)
{"...", "cp_status": "heuristic_fallback", "included": ["CCC826/CCC2101"],
 "excluded": ["CCC2116"], "why_not": [{"code": "CCC2116",
 "blocker": "no_valid_combination", ...}], ...}
```
No crash. Correct must-have retention. Correct exclusion of the genuinely clashing
course, with a real reason.

### Regression test

`backend/tests/test_cp_scheduler.py` has 7 new tests using a monkeypatched
`RUNNING_FROZEN=True` plus a `cp_model.CpModel` replaced with a class that raises if
ever constructed — proving the fallback path genuinely never touches CP-SAT at the
Python level (a real segfault couldn't be caught by an automated test the way an
exception can, but this proves the call is never attempted, which is the actual fix).

### Full frozen smoke test (against the real packaged exe, not source)

1. Submit a real wishlist search → `202`, then poll to `completed`. ✅
2. Fetch results → correct `included`/`excluded`/`why_not`, `cp_status: heuristic_fallback`. ✅
3. Submit a second search, cancel it immediately → `state: cancelled`, `ack_ms: 0.18`. ✅
4. Fetch results for the cancelled job → `409` (never a stale/partial result). ✅
5. Submit a third, fresh search → correct fresh result, unaffected by the cancelled one. ✅
6. `taskkill` the process → clean exit, port released. ✅

## Architecture decision: one-folder, not one-file

Measured directly, same machine, same source, after the fix above:

| | onefile | onedir |
|---|---:|---:|
| Distributable size | 89.7 MB (single .exe) | 214.6 MB (4,115 files) / zipped for distribution |
| Cold start (process launch → `/health/ready` responds) | **12,735 ms** | **1,370 ms** |
| Debuggability | Opaque — everything extracted to a temp dir per launch | Every file visible on disk; `--selftest-worker` log inspection was done against this build |
| Build reliability | Same PyInstaller invocation, same dependencies | Same |

The ~9x startup difference is because onefile re-extracts its entire compressed
archive to a fresh temp directory on **every single launch**; onedir just runs the
already-unpacked files. For a desktop app a student launches repeatedly, a 12.7-second
wait every time is a materially worse experience than a larger on-disk folder — this
matches the request's own framing: *"A smaller single .exe is not automatically better
if it becomes slower, less reliable, or extracts a large temporary directory. Choose
the best total user experience."*

**Decision: `--onedir`, distributed as a zip.** `scripts/build-exe.sh` now builds this
by default; the old `--onefile` invocation is kept, commented, in the same script as a
documented fallback (it does still work correctly after the source fix, just slower to
start every time).

### Alternatives considered and rejected

- **Separate launcher + backend + worker executables** (request's Option C): would add
  real isolation but the actual root cause was a native-extension/frozen-bootstrap
  incompatibility that the pure-Python fallback already solves correctly; splitting
  into multiple processes/executables would add real packaging and IPC complexity for
  a problem that no longer exists once CP-SAT is never invoked when frozen.
- **Managed Python runtime / installer with a real Python environment** (Option D):
  would sidestep the freezing problem entirely (OR-Tools would run in an unfrozen
  interpreter) and is worth reconsidering if the heuristic fallback's quality ever
  becomes a real problem for desktop users — flagged as a real future option, not
  pursued this session because the simpler fix (never call the crashing path) was
  sufficient and is already shipped.
- **Tauri / Electron**: rejected outright — neither addresses a Python native-extension
  packaging problem, and swapping UI shells to solve a backend packaging bug would be
  solving the wrong layer.

## Size breakdown (onedir, largest contributors)

```
$ Get-ChildItem <dist>/_internal -Recurse | group by top-level dir | sum size
```

The dominant contributors are OR-Tools itself (native DLLs + all its solver
subpackages: knapsack, graph, linear_solver, math_opt, pdlp, scheduling, set_cover —
only `sat` is actually used) and the transitively-pulled `pandas`/`numpy`/`matplotlib`/
`tkinter` stack (pandas and numpy are OR-Tools' own dependencies for some of its
optional solvers; matplotlib/tkinter/PIL were already flagged as unnecessary bloat in
an earlier session, §11 of CLAUDE.md, and remain unaddressed this session — pulled in
transitively and not yet worth the risk of an aggressive PyInstaller exclude list this
late in a correctness-focused session).

**Not done this session** (explicitly, not silently): a `--exclude-module` pass to drop
OR-Tools' unused subpackages (`ortools.constraint_solver`, `ortools.graph`,
`ortools.math_opt`, `ortools.pdlp`, `ortools.linear_solver`'s heavier bits) and the
matplotlib/tkinter stack. This is a real, safe-if-done-carefully follow-up — each
exclusion needs an import-test rebuild to confirm CP-SAT (in `ortools.sat.python`
only) still imports cleanly, which is exactly the kind of "prove it before excluding"
work this document's own root-cause section shows is worth taking seriously rather
than guessing at.

## Remaining uncertainty

- The exact reason the CP-SAT native extension segfaults inside a frozen bootstrap
  (vs. a normal Python interpreter) was not identified at the binary level — plausible
  categories include a DLL-search-path difference for a dynamically-loaded dependency
  that isn't caught by import-time linking, a threading/TLS conflict with the
  PyInstaller bootloader, or an abseil-internal crash-handler/signal-handler
  installation conflicting with the bootloader's own signal handling. Confirming which
  would need a debugger (WinDbg/procdump) attached to the frozen process, which this
  session's tooling did not have.
- The pure-Python fallback is a real, tested, correct-but-heuristic scheduler — it does
  not prove optimality the way CP-SAT does, and does not model choice-group
  satisfiability up front (an unsatisfiable `at_least_one`/`min_credits` group is
  simply not enforced, rather than reported infeasible, in the fallback path only).
  This is stated in the fallback function's own docstring, not hidden.
- If a future session wants exact CP-SAT results in the desktop build, the two real
  paths are: root-cause and fix the native crash directly, or ship a bundled, unfrozen
  Python runtime for just the worker process (Option D above).
