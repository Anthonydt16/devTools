# Working on Devbox

## Set-up

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
devbox test
```

Python 3.13 or newer.  Everything else is in `pyproject.toml`.

## The loop

```bash
python -m pytest -m unit -q      # fast feedback
ruff check . && ruff format .
mypy src
devbox test                      # before pushing
```

`devbox test` is what CI runs.  If it is green locally it is green there, with
one exception: the Windows job, which is the point of the next section.

## Developing on Linux for Windows

The development container has no Windows API, no Node, no PostgreSQL and no
network access to a package registry.  Three rules follow:

1. **Every OS-specific capability sits behind a `Protocol`** with a real
   implementation and a deterministic fake.
2. **As much logic as possible is pure** — parsing, validation, graphs, hashing,
   PATH composition, state reconciliation, formatting — and is tested here.
3. **What genuinely needs Windows** is written, typed, documented, marked
   `@pytest.mark.windows`, skipped elsewhere *with a reason*, and really run on
   the `windows-latest` job.

A Windows behaviour verified only through a mock is **never** reported as
validated.  [limitations.md](limitations.md) is the register of everything in
that category.

## Layout

```
src/devbox/
  errors.py       one exception hierarchy; every class carries an exit code
  host/           the ONLY place allowed to branch on the operating system
  catalogs/       which runtime kinds and providers exist (dependency-free leaf)
  config/         devbox.yml: models, strict reader, loader, interpolation
  lockfile/       devbox.lock: models and byte-stable I/O
  security/       path containment, hashing, safe extraction, argv construction
  runtime/        registry, download, install, per-child PATH, resolution
  process/        supervisor protocol + windows / posix / fake, command runner
  state/          process and infrastructure ownership records
  graph/ health/  dependency ordering; TCP and HTTP readiness
  infra/ database/  shared servers; logical databases
  services/       the orchestrator
  build/ validation/ cli/   artifacts, `devbox test`, the Typer app
  testing/        reusable fakes, shipped (not test-only)
  packaging/      PyInstaller build
```

Imports go strictly upwards; see [architecture.md](architecture.md).

## Conventions

* **Fully typed.**  `mypy --strict` on `src/` must pass.  No `Any` to dodge a
  type error; explicit `Any` only at a genuine boundary (ctypes, YAML) and
  narrowed immediately.
* **No OS branching outside `host/`.**  Everything else selects an
  implementation through a factory or receives one by injection.
* **Dependencies are injected.**  Every orchestration class takes its
  collaborators — supervisor, clock, `sleep`, registry, paths — as constructor
  arguments with working defaults.  That is what makes the suite hermetic and
  fast.
* **`subprocess` always gets an argv list.**  `shell=True` is banned and a test
  parses the source tree to prove it.
* **Errors** are `DevboxError` subclasses with a human message and, when there is
  an obvious next step, a `hint=`.  The CLI never shows a traceback without
  `--debug`.
* **Paths** use `pathlib` (`ruff` rule `PTH`).  Never hardcode a Windows path;
  ask `devbox.host.paths`.
* **Docstrings explain *why*.**  A module docstring says what the module is
  responsible for and what it deliberately does not do.

## Adding things

**A CLI command** — a module in `cli/commands/` exposing `register(app)`, listed
in `cli/commands/__init__.py` and in `packaging/build_exe.py::hidden_imports`
(a test checks both).  No business logic: parse, call, render.

**A runtime kind** — `register_runtime_kind(RuntimeKind(...))`.  See
[runtimes.md](runtimes.md).

**An infrastructure provider** — a `ProviderDescriptor` and a class implementing
`InfraProvider`.  See [shared-infrastructure.md](shared-infrastructure.md).
Nothing in the core needs to change; a test proves it.

**A registry backend** — implement four members of `RuntimeRegistry`.  See
[registry.md](registry.md).

## Writing tests

Use the fixtures in `tests/conftest.py`: `devbox_home`, `project_dir`,
`project_with_spaces`, `fake_registry`, `registry_factory`,
`http_registry_server`, `free_port`, `spawned_processes`.  They all tear down.

* mark every test `unit`, `integration` or `windows`;
* no `time.sleep` in a unit test — inject the clock;
* no network — use `http_registry_server`, which binds `127.0.0.1:0`;
* clean up: an autouse fixture fails a test that leaks a running child;
* prefer asserting on the **message** of an error, not just its type.  An error
  nobody can act on is a bug.

## Commits

Conventional prefixes: `feat(runtime):`, `fix(process):`, `test(config):`,
`docs(windows):`, `refactor(cli):`.  Say what changed and why; the diff already
says how.

## Building the executable

```bash
pip install -e ".[package]"
python -m devbox.packaging.build_exe
```

Produces `dist/devbox.exe` (or `dist/devbox`).  `tests/unit/test_packaging.py`
checks the constraints that keep the frozen build working — no `__file__` at
import time, no dynamic import of a computed name, every hidden import
resolvable, only pure-Python runtime dependencies.
