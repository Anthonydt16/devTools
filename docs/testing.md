# Testing

## One command

```powershell
devbox test
```

```
Devbox validation suite — C:\projects\devbox

  ✓ lint         All checks passed!
  ✓ format       134 files already formatted
  ✓ types        Success: no issues found in 88 source files
  ✓ unit         467 passed (5s)
  ✓ integration  146 passed (36s)
  ✓ windows      37 skipped
  ✓ config       1 configuration file(s) checked
  ✓ packaging    Devbox v0.1.0

Summary
  tests:  613 passed, 0 failed, 37 skipped
  checks: 8 run, 0 failed
  time:   43s

  Skipped:
    37 test(s) skipped: requires a real Windows host (running on linux); these run on the windows-latest CI job

✓ all mandatory checks passed
```

It exits non-zero if any **mandatory** check fails, and it always says what was
skipped and why.  A suite that silently skips the interesting half is worse than
one that fails.

```powershell
devbox test --only static     # lint, format, types
devbox test --only tests      # unit, integration, windows
devbox test --fast            # skip tests marked slow
```

On Windows the `windows` check is mandatory; elsewhere it is reported and does
not fail the run.

## Or directly

```powershell
python -m pytest                      # everything
python -m pytest -m unit              # hermetic
python -m pytest -m integration       # real processes
python -m pytest -m windows           # skipped off Windows, with a reason
python -m pytest --cov=devbox --cov-report=term-missing
ruff check . ; ruff format --check . ; mypy src
```

## Markers

| Marker | Meaning |
|---|---|
| `unit` | hermetic: no child process, no network, no dependence on wall-clock time |
| `integration` | spawns real child processes or builds a real directory tree |
| `windows` | needs a real Windows host; skipped elsewhere with an explicit reason |
| `slow` | over ~2 seconds |

`--strict-markers` is on, so a typo in a marker is an error.

## Testing without Node, PostgreSQL, MySQL or a network

This is the part worth understanding.

### Fake runtimes

`devbox.testing.build_fake_runtime` produces a **real** directory tree that looks
like a real runtime:

```
node/18.19.0/
├── node            POSIX launcher (shell script)
├── node.cmd        Windows launcher (batch file)
├── npm, npm.cmd
└── runtime_main.py the payload
```

The launcher bakes the version in, so the executable found through the child's
`PATH` decides what `node --version` prints.  That is what makes non-regression
test #1 — two Node versions at once — a real test rather than an assertion about
a mock.

The payload can bind a port, answer HTTP `/health`, log to stdout *and* stderr,
sleep, fail with a chosen exit code, spawn a grandchild that only a tree kill
stops, and print its `argv` back verbatim.

### Fake registries

`build_fake_registry` zips those trees **deterministically**, computes real
SHA256 digests, and writes a real `index.json`.  So the download → verify →
extract → install pipeline runs for real against a local directory, with no
network.  `corrupt=[...]` makes the index advertise a wrong digest while the
artifact stays valid — exactly the situation integrity checking exists for.

### A fake infrastructure provider

`devbox.testing.fake_provider` registers a provider that starts a **real
process**, binds a **real port**, answers a **real healthcheck** and keeps its
database catalogue in a **real file** in the data directory.  So "an existing
database is never re-initialised" survives a stop, a restart and a second
project, for real.

It is also the worked example of provider extensibility: it adds a provider
without changing one line of the orchestrator.

### The example runs in CI

`examples/node-migration/` ships a real `server.js` for each service, plus a
`server.js.fake` sidecar telling the fake runtime which port to bind and which
path answers the healthcheck.  Real Node ignores that file.

`tests/integration/test_example_node_migration.py` therefore runs the **same**
`devbox.yml`, the same commands and the same healthchecks that the README shows,
on a Linux container with neither Node version installed.

## What is really tested

| Area | How |
|---|---|
| Configuration | 56 cases: every documented failure mode, asserting on the *message* |
| Interpolation | 27 cases: validation at load time, resolution at start-up, non-recursion |
| Lockfile | round-trip, byte stability, ambiguous versions (`8.4` must not become a float), sticky resolution |
| Dependency graph | ordering, determinism, cycles named in order, a 5 000-node chain that must not hit the recursion limit |
| Archive extraction | 30 attacks: traversal, UNC, drive letters, reserved names, links, devices, bombs, lying headers |
| Download | cache, resume, digest mismatch, corrupt cache entry, unreachable registry, plaintext HTTP refused |
| Processes | start, stop, restart, immediate exit, timeout, stdout/stderr separation, exit codes, **process trees**, PID reuse, stale state |
| Healthchecks | retry policy asserted exactly with an injected clock; abort when the process dies |
| Infrastructure | ownership, claims, `external`, persistence across restart, a provider the core has never heard of |
| Databases | existence, run-once init, failure reporting, SQL identifier refusal |
| Orchestration | full `up`/`down`, dependency order, rollback on failure, idempotence |
| CLI | exit codes per error class, `--help`, bad arguments, missing config, `--debug`, quiet, never reads stdin, paths with spaces and accents |
| Build | reproducibility (byte-identical), manifest, lockfile digest, inclusion rules |
| Autonomy | clean machine, empty cache, full cache, interrupted download, incomplete install, corrupt state, crash recovery, awkward paths |
| Packaging | argv composition, every hidden import resolvable, no `__file__` at import time, no dynamic import of a computed name |
| Windows contract (**on Linux**) | every `kernel32` call has a signature, `HANDLE` is pointer-sized, argument counts match Win32, no ANSI variants, no OS branching outside `host/`, no POSIX-only API in code or tests the Windows job runs, no hardcoded POSIX child environment |

## The ten non-regression rules

`tests/unit/test_non_regression.py` states each rule from `CLAUDE.md` as a named
class, so a reviewer can read one file and see that all ten are enforced.  Most
are also covered end to end elsewhere; the file says which.

## The suite is load-bearing

`tests/unit/test_suite_is_load_bearing.py` deliberately breaks mandatory
behaviour and checks that the guarantee disappears:

* path containment is disabled → a real `pytest` run of rule 5 **fails**;
* the shared-ownership rule is disabled → a real `pytest` run of rule 2 **fails**;
* in-process mutations show that each guarantee is produced by the mechanism it
  claims, not by accident.

It also documents a finding: integrity has **two** independent guards (the
lockfile-versus-registry comparison and the digest of the bytes received), so
disabling one is not enough to let a bad artifact in.

## Hygiene, enforced

* `DEVBOX_HOME` points at a temporary directory for **every** test, so nothing
  can touch a developer's real installed runtimes;
* an autouse fixture **fails** any test that leaves a running child process
  behind (zombies excluded — a detached service Devbox deliberately did not reap
  is not a leak);
* no network: a "remote" registry is an `http.server` this process started on
  `127.0.0.1`, port 0, torn down at the end of the test;
* no `time.sleep` in a unit test — clocks and `sleep` are injected;
* `filterwarnings = ["error"]`, so a `ResourceWarning` fails the run.

## Coverage

83% overall, with nothing excluded from the report on purpose.  The modules
below the line are there for reasons worth stating rather than hiding:

| Module | Linux | Why |
|---|---|---|
| `testing/fake_runtime_main.py` | 16% | it runs in **child processes**, so the parent's coverage never sees it — it is one of the most heavily exercised files in the repository |
| `host/winapi.py` | 30% | Win32 `ctypes`; covered by the `windows-latest` job |
| `process/windows.py` | 40% | Job Objects; covered by the `windows-latest` job |
| `host/procinfo.py` | 58% | the Windows and macOS branches cannot run here |
| `__main__.py` | 60% | the `if __name__ == "__main__"` guard |

## CI

| Job | Runs |
|---|---|
| **linux** | lint, format, types, unit, integration, coverage, leak check |
| **windows** | the `windows` marker, plus unit and integration on the target OS, plus a CLI run from a path containing spaces, plus a leak check |
| **validation** | `devbox test` — the exact command the README tells a developer to run |
| **packaging** | builds `devbox.exe` with PyInstaller and runs it |

If `devbox test` passes in CI and fails for you, the promise is broken — which is
why that job exists separately from the one that runs pytest directly.
