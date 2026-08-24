# Limitations

The honest register.  Three categories: what is deliberately out of scope, what
is implemented but **cannot be executed** in the development container, and what
is known to be imperfect.

## 1. Out of scope for the MVP

Not started, and the architecture does not block any of them.

| | Note |
|---|---|
| Containerisation, kernel namespaces, overlay filesystems, virtual networking | Devbox is explicitly *not* a container runtime. |
| Docker image or daemon compatibility, Kubernetes | Out of scope by design. |
| A GUI, a registry web UI | — |
| A production Windows service | Devbox supervises development processes for the logged-in user. |
| Cloud orchestration, deployment | The pipeline shape (`install → test → build → scan → registry → DEV/RECETTE/PROD`) is not blocked; `devbox build` produces the artifact and the manifest. |
| Test matrices (`test.matrix.node: [...]`) | `devbox run --node <version>` already runs one command under one version; a matrix is a loop over it. |
| Redis and other providers | The provider registry is in place and a fictional provider is driven end to end by the tests. Only the concrete Redis class is missing. |
| Log rotation | Logs are appended and never rotated. `devbox logs --tail` reads backwards in bounded memory, so a large file is not a problem to *read*; it will grow until deleted. |

## 2. Implemented but not executable in this environment

Written, fully typed, documented and covered by `@pytest.mark.windows` tests that
really run on the `windows-latest` CI job — but **never executed** in the Linux
development container.  Nothing here is reported as verified from a Linux run.

| Component | What is unverifiable locally |
|---|---|
| `host/winapi.py` | `GetProcessTimes`, `CreateJobObjectW`, `OpenJobObjectW`, `AssignProcessToJobObject`, `SetInformationJobObject`, `TerminateJobObject`, `GetExitCodeProcess`, `OpenProcess`, `CreateToolhelp32Snapshot`, `Thread32First`/`Thread32Next`, `Process32FirstW`/`Process32NextW`, `OpenThread`, `ResumeThread`, `TerminateProcess` |
| `process/windows.py` | job creation and assignment, `CTRL_BREAK_EVENT`, reopening a named job from a later invocation, tree termination |
| `.cmd` shim resolution | the *rules* are unit-tested with an injected predicate; actually executing `npm.cmd` through `CreateProcess` is not |
| `%LOCALAPPDATA%` resolution | the *logic* is unit-tested with a fake environment; the real variable is not |
| cp1252 console rendering | the fallback is unit-tested; a real legacy console is not |
| Long-path behaviour | the test reports and skips when the host has not enabled long paths |
| PyInstaller build | `devbox.exe` is built and executed only on the Windows CI job |

**Real PostgreSQL and MySQL are never started anywhere in the test suite.**  What
is tested is every argument list Devbox constructs for `initdb`, `postgres`,
`pg_ctl`, `psql`, `createdb`, `mysqld`, `mysql` and `mysqladmin`, and everything
Devbox does with their output.  Whether PostgreSQL 17 accepts those exact flags
on a given Windows build has not been verified by this repository.  It is the
first thing to check on a real machine — see the README procedure.

## 3. Known imperfections

### What review of the Windows paths turned up

An audit of the Windows-specific code — conducted *after* the first "it is
finished" report, and worth recording rather than quietly fixing — found seven
real defects. All are fixed and each now has a test that runs on Linux:

1. no `argtypes`/`restype` on any `kernel32` call, truncating 64-bit `HANDLE`
   values;
2. `security/archive.py` branching on `os.name`, violating the project's own
   rule and leaving the Windows branch unexercised;
3. `shutil.rmtree` on read-only files, which fails on Windows and which Node and
   PostgreSQL archives both produce;
4. a locked destination directory turning into a bare `FileExistsError` instead
   of "a service is still running from this runtime";
5. `os.killpg` and `signal.SIGKILL` in a test the `windows-latest` job runs —
   an `AttributeError` there, not a failure of Devbox but a false green;
6. integration tests handing children `PATH=/usr/bin:/bin` and no `SystemRoot`,
   which stops a Python child from starting at all on Windows;
7. a long service name producing a Job Object name past the Win32 limit.

Nothing in that list was found by the 613 tests that were passing; they were
found by reading the code with the target platform in mind. That is the honest
measure of how much a green Linux suite says about Windows.

### Graceful shutdown is unavailable for detached services

A detached service has no console shared with Devbox — that is what lets it
survive the terminal closing — and a console control event cannot reach a
process without one.  So `devbox down` terminates its job object rather than
asking politely first.  For a Node dev server that is harmless; for a database
it would not be, which is why PostgreSQL and MySQL are stopped with their own
commands instead.  Foreground mode does send Ctrl-Break.  Full reasoning in
[windows.md](windows.md).

### Job Object assignment race: closed

The original implementation launched the root process before assigning it to
its Job Object, leaving a small interval in which a rapidly spawned descendant
could escape.  Windows launches are now created with `CREATE_SUSPENDED`, assigned
to the named Job Object, and resumed only afterwards through the Toolhelp thread
APIs.  Because user code cannot execute while the initial thread is suspended,
there is no launch interval in which a descendant can escape.

Devbox also captures the verified root's descendant chain before stopping the
Job Object.  A double Toolhelp snapshot, creation-time check, and pinned process
handles provide a guarded fallback when an outer host job changes inheritance
semantics.  A descendant that explicitly requests `CREATE_BREAKAWAY_FROM_JOB`
can still leave the Job Object if policy permits it, but it is included in this
fallback while its verified parent chain remains connected to the live root.
Devbox never grants that flag to processes it starts.

### Exit codes are not available across invocations

`devbox up` starts a service detached and exits.  If the service later dies on
its own, a subsequent `devbox ps` can prove the process is gone but **cannot
report its exit code** — nobody was waiting on it.  The state shows `exited` with
no code; the reason is in `.devbox\logs\<service>.err.log`.

Fixing it properly needs a supervising process that outlives `devbox up`, which
is the Windows-service design the MVP deliberately excludes.

### Zombie processes inside a long-lived parent

On POSIX, a detached child Devbox stopped stays a zombie until its parent exits.
For the CLI this is invisible (the parent *is* `devbox`, and it exits).  Inside a
long-lived process — the test suite — the entry lingers, so
`process_is_alive` consults `/proc/<pid>/stat` and reports a zombie as dead.

### `devbox exec` and `devbox run` buffer output

Both capture the child's output and print it when the command finishes, rather
than streaming it.  Fine for `node --version`; not ideal for a long build.
`devbox logs --follow` streams properly.

### No lockfile is committed with the example

`examples/node-migration/devbox.lock` is absent because there is no public
Devbox runtime registry to resolve against — the digests would be invented.  Run
`devbox install --registry <your-registry>` once and commit the result.  The
example's *configuration* is validated on every CI run, and the example is
started end to end against a generated fake registry.

### Interpolation is not recursive

`${database.clients.url}` is resolved once.  A value that itself contains
`${...}` after substitution is left alone.  Deliberate: recursive expansion is a
good way to write a configuration file that loops.

### `--follow` polls

`devbox logs --follow` polls the file every 250 ms rather than using
`ReadDirectoryChangesW` / `inotify`.  Simple, portable, and imperceptible at
human latency; it does mean a busy log is delivered in bursts.

### Coverage of the Windows modules

Nothing is excluded from the coverage report.  `host/winapi.py` and
`process/windows.py` therefore score low on a Linux run — they cannot execute
there — and that is deliberate: omitting them would make the total look better
and mean less.  They are exercised by the `windows-latest` job.

## What would be done next

1. Run the real PostgreSQL and MySQL providers against real binaries on a
   Windows machine and correct the flags they need.
2. Ship a concrete Redis provider, since the abstraction is already proven.
3. Stream `devbox exec` output instead of buffering it.
4. Publish a signed reference registry so the example can commit a lockfile.
