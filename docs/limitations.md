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
| `host/winapi.py` | `GetProcessTimes`, `CreateJobObjectW`, `OpenJobObjectW`, `AssignProcessToJobObject`, `SetInformationJobObject`, `TerminateJobObject`, `GetExitCodeProcess`, `OpenProcess` |
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

### The Job Object assignment race

`subprocess.Popen` does not expose the primary thread handle, so a process cannot
be created suspended and resumed after being assigned to its job.  A descendant
spawned between `CreateProcess` and `AssignProcessToJobObject` would escape the
job and survive `devbox down`.

The window is microseconds; no interpreter or package manager has finished
start-up in that time.  Closing it properly means reimplementing `CreateProcess`
with `CREATE_SUSPENDED` via `ctypes`.  Not done; the trade-off is recorded here
rather than hidden.

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

### Coverage of `winapi.py`

`process/windows.py` and `host/winapi.py` are excluded from the Linux coverage
report — they cannot execute there, and including them would make the number
meaningless in the wrong direction.  They are covered by the Windows job.

## What would be done next

1. Run the real PostgreSQL and MySQL providers against real binaries on a
   Windows machine and correct the flags they need.
2. Close the Job Object assignment race with `CREATE_SUSPENDED`.
3. Ship a concrete Redis provider, since the abstraction is already proven.
4. Stream `devbox exec` output instead of buffering it.
5. Publish a signed reference registry so the example can commit a lockfile.
