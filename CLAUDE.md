# CLAUDE.md — working rules for this repository

Devbox is a **user-space runtime and process orchestrator for Windows 11**.  It
gives `docker compose` ergonomics with **no Docker, no WSL, no Hyper-V, no
containers and no administrator rights**.

## Commands

Everything below assumes an activated virtualenv with `pip install -e ".[dev]"`.

| Goal | Command |
|---|---|
| Full autonomous validation (what CI runs) | `devbox test` |
| Unit tests only | `python -m pytest -m unit` |
| Integration tests (spawn real processes) | `python -m pytest -m integration` |
| Everything, with coverage | `python -m pytest --cov=devbox --cov-report=term-missing` (83%) |
| Windows-only tests | `python -m pytest -m windows` (skips off Windows) |
| Lint | `ruff check .` |
| Format check / fix | `ruff format --check .` / `ruff format .` |
| Types | `mypy src` |
| Build a one-file exe | `python -m devbox.packaging.build_exe` (needs `pip install -e ".[package]"`) |

`devbox test` is the single command a developer runs to check the project: it
chains lint, types, unit, integration, Windows-when-available, config checks and
packaging checks, and prints a summary with an explicit count of skipped tests
and the reason they were skipped.

## Layout

Imports go **strictly upwards**.  An earlier version had a cycle; the fix was to
move things where they belonged, not to defer imports.

```
src/devbox/
  errors.py       business error hierarchy; every class carries an exit code
  host/           the ONLY place allowed to branch on the operating system
                  paths.py (%LOCALAPPDATA%\Devbox), platform_info.py,
                  procinfo.py (PID + creation time),
                  winapi.py (ctypes: GetProcessTimes, Job Objects)
  catalogs/       which runtime kinds and providers exist — dependency-free leaf,
                  because config validation needs it and must not depend on the
                  layers it validates
  config/         devbox.yml: models.py, reader.py (strict), loader.py,
                  interpolation.py (${database.x.url})
  lockfile/       devbox.lock: models.py, io.py (byte-stable)
  security/       paths (containment), hashing, archive (safe extraction),
                  command (argv construction, never a shell)
  runtime/        registry.py, download.py, manager.py, resolver.py,
                  environment.py (per-child PATH), manifest.py
  process/        supervisor.py (Protocol), base.py (shared), windows.py
                  (Job Objects), posix.py (dev/CI on Linux), fake.py, runner.py
  state/          process and infrastructure ownership records
  graph/          dependency graph, topological order, cycle detection
  health/         TCP and HTTP readiness, retry policy with injected clock
  infra/          shared PostgreSQL / MySQL / <any registered provider>
  database/       logical databases inside a shared server
  services/       the orchestrator: project.py, orchestrator.py, events.py
  build/          reproducible artifact + manifest
  validation/     `devbox test`: checks.py, suite.py
  cli/            Typer application; no business logic lives here
  testing/        reusable fakes and fixtures (shipped, not test-only)
  packaging/      PyInstaller build for devbox.exe
```

Resolving a lockfile queries a registry, so `resolve_lockfile` lives in
`runtime/`, not in `lockfile/`.

## Conventions

- **Python 3.13+, fully typed.**  `mypy --strict` on `src/` must pass.  No
  `Any` used to dodge a type error; explicit `Any` is allowed only at a genuine
  boundary (ctypes, YAML) and must be narrowed immediately.
- **No OS branching outside `devbox/host/`.**  Everything else selects an
  implementation through a factory or receives one by injection.
- **Dependencies are injected.**  Every orchestration class takes its
  collaborators (supervisor, clock, registry, paths) as constructor arguments
  with a sensible default.  This is what makes the suite hermetic.
- **`subprocess` is always called with an argv list.**  `shell=True` is banned.
  Commands written by users in `devbox.yml` go through
  `devbox.security.command.split_command`, never through a shell.
- **Errors**: raise a `DevboxError` subclass with a human message and, when
  there is an obvious next step, a `hint=`.  The CLI never shows a traceback
  unless `--debug` is passed.
- **No secrets in the repository or in `devbox.yml`.**  Only `*_env` references
  naming an environment variable.
- **Paths**: `pathlib` everywhere (`ruff` rule `PTH` enforces it).  Never
  hardcode a Windows path; ask `devbox.host.paths`.
- **Docstrings** explain *why*, not *what*.  A module docstring states what the
  module is responsible for and what it deliberately does not do.

## Tests

- Markers (declared in `pyproject.toml`, `--strict-markers` is on):
  - `unit` — hermetic, no child process, no network, no clock dependence;
  - `integration` — spawns real child processes or builds a real directory tree;
  - `windows` — needs a real Windows host; skipped elsewhere with an explicit
    reason via `tests/conftest.py`;
  - `slow` — over ~2 seconds.
- **The suite must be load-bearing.**  `tests/unit/test_suite_is_load_bearing.py`
  sabotages mandatory behaviour and asserts the suite goes red.  A new
  non-regression rule should be accompanied by a sabotage that proves it.
- **Every test must clean up**: no leaked child process, no leaked temp file, no
  bound port left open.  Use the `devbox_home`, `project`, `supervisor` and
  `fake_runtime` fixtures from `tests/conftest.py`; they all tear down.
- **No network.**  A registry under test is a local directory or a
  `http.server` started by a fixture on port 0.
- **Determinism.**  No `time.sleep` in a unit test; inject `Clock`.  No reliance
  on dict ordering from the filesystem; sort.
- Windows-specific code that cannot run in the Linux container is still written,
  still typed, and still tested on the `windows-latest` CI job.

## Rules that must never be broken

They each have a dedicated non-regression test in
`tests/unit/test_non_regression.py` and `tests/integration/`:

1. Two services on two Node versions get two different `PATH` values, at once.
2. `devbox down` on a project never stops infrastructure started by `devbox infra up`.
3. An existing database is never re-initialised.
4. A SHA256 mismatch refuses the install; nothing is extracted or executed.
5. An archive containing `../../evil.exe` is rejected.
6. A dependency cycle produces a clear error naming the cycle.
7. After a hard kill, Devbox never kills a process it does not own (PID reuse).
8. Commands with spaces and special characters are passed through unaltered.
9. An `external` configuration never starts local infrastructure.
10. `devbox test` exits non-zero when a mandatory check fails.
