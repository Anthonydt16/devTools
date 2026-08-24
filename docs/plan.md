# Implementation plan

## Constraint that shapes everything

Devbox is **developed on Linux and targets Windows 11**.  The development
container has no Windows API, no Node, no PostgreSQL and no network access to a
package registry.  Three rules follow, and they drive most of the architecture:

1. Every OS-specific capability sits behind a `Protocol` with at least a real
   implementation and a deterministic fake.
2. As much logic as possible is pure: parsing, validation, graphs, hashing,
   PATH construction, state reconciliation, formatting.  All of it is tested on
   Linux.
3. What genuinely needs Windows is written, typed and documented, marked
   `@pytest.mark.windows`, skipped with an explicit reason elsewhere, and run
   for real on a `windows-latest` GitHub Actions job.

A Windows behaviour verified only through a mock is **never** reported as
validated; `docs/limitations.md` is the register of everything in that category.

## Phases

| # | Phase | Delivers |
|---|---|---|
| 1 | Foundations | `pyproject.toml`, `CLAUDE.md`, error hierarchy with exit codes, `host/` (paths, PID identity, Win32 bindings), config models + strict reader + loader + interpolation, provider/runtime catalogs, test scaffolding |
| 2 | Config, lockfile, graph | environment overlays, `devbox.lock` read/generate/validate, dependency graph with topological order and cycle detection |
| 3 | Processes | `ProcessSupervisor` protocol, Windows Job Object implementation, POSIX implementation, deterministic fake, state store with PID-reuse detection, log capture and reading |
| 4 | Runtimes | registry abstraction (filesystem + HTTP), download pipeline with SHA256 gate, safe extraction, cache, atomic install, per-child `PATH` construction |
| 5 | Shared infrastructure | provider abstraction, PostgreSQL and MySQL providers, persistent data directories, `managed` vs `external`, logical databases with run-once init |
| 6 | Orchestration and build | `up`/`down`/`ps`/`logs`/`exec`/`run`, healthcheck waiting, reproducible `devbox build` |
| 7 | `devbox test` | autonomous validation suite: lint, types, unit, integration, Windows-when-available, config and packaging checks |
| 8 | Hardening | security tests, Windows CI, crash recovery, coverage, documentation, worked example |

## Architecture decisions

### D1 — Runtimes are per-process, never global

Devbox never touches the machine or user `PATH`.  `RuntimeEnvironment.apply()`
builds the environment **for one child process**: the runtime `bin` directories
are prepended to a copy of the parent environment, which is then handed to
`subprocess.Popen(env=...)`.

*Consequence*: Node 18 and Node 24 can serve two services in the same
`devbox up`, because nothing global arbitrates between them.  This is the
central use case (`examples/node-migration`) and non-regression test #1.

*Rejected*: shim directories with `node.cmd` written per service.  It works, but
it puts a mutable generated file on disk per service and breaks as soon as two
runs overlap.

### D2 — Process trees are killed with Windows Job Objects

Windows has no process group kill.  `npm run dev` → `node` → bundler: stopping
the `npm` PID orphans everything below it.  Devbox creates one Job Object per
service with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so the tree dies both on an
explicit `TerminateJobObject` and if Devbox itself is killed.

*Race-free launch*: the root process is created with `CREATE_SUSPENDED`, assigned
to the Job Object, then resumed through a Toolhelp thread snapshot.  Although
`subprocess.Popen` closes the primary thread handle, its thread ID can be
recovered with `Thread32First`/`Thread32Next` and opened for `ResumeThread`.
User code therefore cannot create a descendant before Job Object assignment.
Before stopping, a guarded Toolhelp process snapshot also pins the verified
descendant chain; this is a fallback for outer host jobs that alter inheritance,
while `TerminateJobObject` remains the primary mechanism.

*Rejected*: `taskkill /T /F`.  It walks the parent-PID tree, which is unreliable
once an intermediate process has exited, and it is exactly the "kill anything
that looks like node" behaviour Devbox must not have.

### D3 — Ownership is PID + creation time

A PID alone is meaningless after a reboot or a crash.  Every state record stores
the process creation time (`GetProcessTimes` on Windows, `/proc/<pid>/stat`
field 22 on Linux).  Devbox refuses to stop a process whose creation time does
not match the record — non-regression test #7.

### D4 — Infrastructure ownership is explicit and refcounted

`devbox infra up` marks an instance as **globally owned**.  A project that
starts it on demand marks itself as a user of it.  `devbox down` releases the
project's claim and only stops the instance if no other project holds it **and**
it was not started globally.  Data directories live under
`%LOCALAPPDATA%\Devbox\infra\<provider>\<instance>\data` and are never deleted
by `devbox down` — non-regression test #2.

### D5 — Providers are a registry, not an `if/elif`

`infra/catalog.py` maps a provider name to a descriptor; `infra/provider.py`
defines the lifecycle protocol.  The orchestrator never names PostgreSQL or
MySQL.  `tests/unit/test_extensibility.py` registers a fictional provider and
drives a full lifecycle through the public API with no change to the core.

### D6 — Typer for the CLI

Typer builds the parser from type hints, which is the same source of truth the
rest of the codebase uses, and it is a single pure-Python dependency —
important because `devbox.exe` is produced with PyInstaller.  Business logic
lives in the service layer; `cli/` only parses, calls, renders and maps
exceptions to exit codes, which is what makes the CLI testable without a
terminal.

*Rejected*: `argparse` (no typed sub-commands, verbose), and a bespoke parser
(no value).  Click alone would work too; Typer is Click-shaped with the type
hints already written.

### D7 — Hand-rolled validation instead of Pydantic

`devbox.yml` errors must be located precisely, reported all at once, and must
reject unknown keys with a suggestion.  That is a small amount of code
(`config/reader.py`) and it keeps the PyInstaller bundle free of a compiled
validation core.  Full rationale in `docs/architecture.md`.

### D8 — The lockfile is the boundary between intent and resolution

`devbox.yml` says "node 18.19.0".  `devbox.lock` says "this exact artifact, this
SHA256, from this source, for this platform".  Resolution is deterministic and
the lockfile is committed to Git.  Nothing is ever installed without a SHA256
that matches — non-regression test #4.

### D10 — One-way layering, enforced by the import graph

Packages import strictly upwards:

```
errors · host · catalogs          leaf, no Devbox dependencies
config · security · lockfile      depend only on the leaves
runtime                           depends on config.models, lockfile.models, security
process · state · logs · health · graph
infra · database
services · build · validation · cli
```

Two consequences of building it this way, both discovered by an import cycle
that this restructure removed:

* the *catalogs* (which runtime kinds and which infrastructure providers exist)
  live in their own leaf package, because configuration validation needs them
  and the config layer must not depend on the layers it validates;
* **resolving** a lockfile queries a registry, so `resolve_lockfile` lives in
  `runtime/`, not in `lockfile/`.  `lockfile/` only models, reads and writes the
  file, and depends on nothing above it.

### D9 — Three supervisors, one protocol

`windows.py` (Job Objects), `posix.py` (process groups — used to develop and to
run the integration suite on Linux CI) and `fake.py` (deterministic, no
process at all).  The POSIX implementation is not a compromise: it is what lets
`up / ps / logs / exec / down` be integration-tested end to end in the cloud
container, against fake runtimes, with no Node and no PostgreSQL installed.
