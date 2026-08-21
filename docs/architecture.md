# Architecture

## The shape of the problem

Devbox is developed on Linux and targets Windows 11.  It must run every service a
project needs, with pinned runtime versions, on a machine where the developer
cannot install anything system-wide.  Three constraints follow, and they explain
almost every decision below:

1. **Nothing global.** No system `PATH`, no registry key, no Windows service, no
   `Program Files`.  Everything lives under `%LOCALAPPDATA%\Devbox`.
2. **Detached lifetimes.** `devbox up` returns once the project is healthy; a
   later `devbox ps`, `devbox logs` or `devbox down` is a *different OS process*
   and must be able to find and control what the first one started.
3. **Testable without the target.** The Windows-specific half is written, typed
   and documented but cannot execute in the development container, so it sits
   behind a protocol with a real POSIX sibling and a deterministic fake.

## Layers

Imports go strictly upwards.  The direction is enforced by the import graph
itself — an earlier version had a cycle, and the fix was to move things to where
they belonged rather than to defer imports.

```
errors · host · catalogs                leaf: no Devbox dependencies
   ↑
config · security · lockfile            depend only on the leaves
   ↑
runtime                                 registry, download, extraction, install, PATH
   ↑
process · state · logs · health · graph
   ↑
infra · database
   ↑
services (orchestrator)
   ↑
build · validation · cli
```

Two consequences worth stating:

* the **catalogs** — which runtime kinds and which infrastructure providers
  exist — are their own leaf package, because configuration validation needs
  them and the config layer must not depend on the layers it validates;
* **resolving** a lockfile queries a registry, so `resolve_lockfile` lives in
  `runtime/`, not in `lockfile/`.  `lockfile/` only models, reads and writes the
  file.

## The five mechanisms that matter

### 1. `PATH` is built per child process

`RuntimeEnvironment` composes a complete environment for **one** child:
the runtime `bin` directories first, then the inherited `PATH` so `git` and the
system tools stay reachable.  It is handed to `subprocess.Popen(env=...)`.

Nothing global arbitrates, so two services can run two different Node versions in
the same `devbox up`.  That is the central use case and non-regression test #1.

*Rejected*: writing `node.cmd` shim files per service.  It works, but it puts a
mutable generated file on disk per service and breaks the moment two runs
overlap.

### 2. Process trees die together

Windows has no `killpg`.  `npm run dev` → `node` → bundler: stopping the `npm`
PID orphans everything below it, still holding port 3000.

Devbox creates one **Job Object per service**, named
`Local\Devbox-<project-hash>-<service>`:

* **named**, because `devbox down` runs in a different process and has to reopen
  it (`OpenJobObjectW`).  The `Local\` namespace scopes the name to the current
  logon session.
* **without `KILL_ON_JOB_CLOSE` by default**, because detached services must
  outlive `devbox up`.  It is switched on only in foreground mode, where Ctrl-C
  should take everything down.
* **graceful first**: `CREATE_NEW_PROCESS_GROUP` lets Devbox send
  `CTRL_BREAK_EVENT` and give a server time to close its listeners;
  `TerminateJobObject` is the escalation after `stop_timeout`.

On POSIX the same protocol is satisfied with process groups
(`start_new_session=True` + `killpg`), which is what makes the whole orchestrator
integration-testable on Linux.

*Rejected*: `taskkill /T /F`.  It walks the parent-PID tree, which is unreliable
once an intermediate process has exited, and it is exactly the "kill anything
that looks like node" behaviour Devbox must not have.

### 3. Ownership is PID **and** creation time

A PID alone means nothing after a reboot.  Every state record stores the process
creation time — `GetProcessTimes` on Windows, `/proc/<pid>/stat` field 22 on
Linux — and Devbox refuses to signal a PID whose creation time does not match.
Non-regression test #7.

A record with *no* creation time (an older state file) is also refused: unable to
prove ownership means unable to act.

### 4. Shared infrastructure is refcounted and explicitly owned

One PostgreSQL on the machine serves every project, as it would in a real
company environment.  `devbox infra up` marks an instance **globally owned**; a
project that starts one on demand records a **claim**.  `devbox down` releases
the project's claim and stops the instance only if nothing else holds it and it
was not started globally.  Data directories are never deleted.
Non-regression test #2.

### 5. Nothing runs unverified

`devbox.yml` says "node 18.19.0".  `devbox.lock` says "this artifact, this
SHA256, from this source, for this platform", and is committed.  The download
pipeline is: cache (keyed by digest) → stream to a temp file → resume if
interrupted → verify the **whole** digest → refuse and delete on mismatch →
atomic move.  Extraction refuses traversal, links, device nodes and archive
bombs, stages everything, and moves it into place in one atomic rename.

If the registry now advertises a different digest than the lockfile pins, the
**lockfile wins** and the install is refused — that is what a lockfile is for.

## Choices with alternatives

### Typer for the CLI

Typer builds the parser from type hints, which is the same source of truth the
rest of the codebase uses, and it is one pure-Python dependency — which matters
because `devbox.exe` is produced with PyInstaller.  `standalone_mode=False`
hands exit-code and error rendering to Devbox, which is what makes exit codes
reliable in CI and assertable in tests.

*Rejected*: `argparse` (no typed sub-commands, verbose declarations); a bespoke
parser (no value); Click alone would also have worked — Typer is Click-shaped
with the type hints already written.

### Hand-rolled configuration validation, not Pydantic

Three reasons, in order of weight:

1. **Error messages.** Devbox promises every problem located and reported *at
   once*, before anything starts:
   `services.backend.healthcheck.http.timeout: expected a number, got 'abc'`.
2. **Unknown keys.** A typo such as `depend_on` must fail loudly with a
   suggestion.
3. **Packaging.** Pydantic's compiled core adds weight and hidden imports to the
   frozen executable, for validation we would hand-write anyway to satisfy (1).

The cost is `config/reader.py` — about 300 lines, fully tested.

### Shelling out to `psql`/`mysql` instead of a driver

`psycopg` and `mysqlclient` need a compiler or bundled libraries on Windows.
Devbox has to be installable by a standard user with `pip install` and nothing
else.  The client binaries ship *inside* the runtime Devbox already downloaded
and verified, and every argument list Devbox builds is unit-tested without either
server installed.

Database names are **allowlisted, not escaped**: SQL identifiers cannot be
parameterised, so the only safe rule is to refuse anything that is not
`[A-Za-z0-9_$-]`.

### Three supervisors, one protocol

`windows.py` (Job Objects), `posix.py` (process groups) and `fake.py`
(deterministic, no OS process).  The POSIX implementation is not a compromise:
it is what lets `up / ps / logs / exec / down` be integration-tested end to end,
against fake runtimes, with no Node and no PostgreSQL installed.

The fake is a genuine implementation, not a recording of calls — it enforces the
same rules (a recycled PID is refused, a stopped process cannot be stopped
twice), so a test that passes against it is testing behaviour.

## Dependency injection everywhere

Every orchestration class takes its collaborators as constructor arguments with
working defaults: supervisor, state store, runtime manager, command runner,
clock, `sleep`.  Two payoffs:

* the retry policy of a healthcheck with 30 attempts one second apart is
  asserted **exactly**, in microseconds, because `sleep` and `monotonic` are
  arguments;
* `ProjectContext` is the composition root, so the orchestrator runs unchanged
  against fakes in unit tests and against the real machinery in production.

## Extensibility

Adding `company-redis` is a `ProviderDescriptor`, a class implementing
`InfraProvider`, and a `register_provider()` call.  Nothing in `services/`,
`infra/manager.py` or the CLI names PostgreSQL or MySQL.
`tests/unit/test_infra_manager.py` and `devbox/testing/fake_provider.py` prove
it by driving a full lifecycle for a provider the core has never heard of.
