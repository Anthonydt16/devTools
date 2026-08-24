# Windows

What is genuinely different on the target platform, and how each difference is
handled.  This is also the honest register of what the Linux development
container cannot execute — see the last section.

## Job Objects, because there is no `killpg`

`npm run dev` starts `node`, which starts a bundler and a watcher.  Terminating
the `npm` PID leaves all of them running and holding port 3000.  Windows offers
no way to signal a process group, so Devbox uses the mechanism the OS does
provide: a **Job Object** per service.

```
CreateJobObjectW(NULL, "Local\\Devbox-<project-hash>-<service>")
SetInformationJobObject(...)            # KILL_ON_JOB_CLOSE, foreground mode only
CreateProcess(...)                      # CREATE_SUSPENDED + console/group flags
AssignProcessToJobObject(job, process)  # root is assigned before user code runs
CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)
ResumeThread(primary_thread)            # descendants now inherit the job
...
GenerateConsoleCtrlEvent(CTRL_BREAK)    # graceful first
TerminateJobObject(job, 1)              # escalation after stop_timeout
```

Three decisions worth explaining:

**The job is named.**  `devbox up` starts services detached and exits; `devbox
down` is a *different process* and has to reach the job again, which it does with
`OpenJobObjectW` on the name recorded in state.  The `Local\` namespace scopes
the name to the current logon session, so nothing on another user's desktop can
open it.

**`KILL_ON_JOB_CLOSE` is off by default.**  With it on, the tree would die the
moment `devbox up` exits — precisely what detached services must not do.  It is
switched on only in foreground mode, where Ctrl-C should take everything down.

**Graceful stop is a trade-off, not a feature that always applies.**
`GenerateConsoleCtrlEvent` — what `os.kill(pid, CTRL_BREAK_EVENT)` calls —
signals process groups attached to *the caller's* console.  A detached service is
created with `CREATE_NO_WINDOW` precisely so that it has **no** console shared
with Devbox and survives the terminal window being closed.  Those two facts are
in tension, and Devbox resolves it explicitly:

| Mode | Flags | Stop |
|---|---|---|
| detached (default) | `CREATE_SUSPENDED` + `CREATE_NO_WINDOW` + `CREATE_NEW_PROCESS_GROUP` | `TerminateJobObject`, immediately |
| foreground | `CREATE_SUSPENDED` + `CREATE_NEW_PROCESS_GROUP` | `CTRL_BREAK_EVENT`, then `TerminateJobObject` after `stop_timeout` |

Sending an event that cannot be delivered would be worse than not sending one:
if `GenerateConsoleCtrlEvent` returned success without reaching anybody, every
`devbox down` would wait the full `stop_timeout` per service for nothing.  So
`WindowsSupervisor` only attempts it for processes it started with a shared
console.

Graceful shutdown where it actually matters — a database mid-checkpoint — does
not go through a console event at all: PostgreSQL is stopped with
`pg_ctl stop -m fast` and MySQL with `mysqladmin shutdown`, which is both
available and more correct.

Since Windows 8, jobs nest, so assigning a process that already belongs to
another job (a CI agent's, for instance) succeeds instead of failing with
`ERROR_ACCESS_DENIED`.

The Job Object remains the primary stop mechanism.  As a defensive fallback for
hosts whose outer job changes descendant-inheritance behaviour, Devbox also
captures the verified root's descendant chain before calling
`TerminateJobObject`.  It opens and pins only descendants that remain below that
root in a second Toolhelp snapshot and whose creation time does not predate the
root.  After the Job Object stop, any captured process still alive is terminated
through its handle; PID reuse cannot redirect that handle to an unrelated
process.

### Race-free assignment

Every supervised Windows process is created with `CREATE_SUSPENDED`.  Devbox
assigns that process to its named Job Object before resuming the initial thread,
so user code cannot run and cannot spawn a descendant outside the job during
launch.

`subprocess.Popen` closes the primary thread handle before returning.  Devbox
therefore uses the documented Toolhelp APIs to enumerate the newly created
process's thread, opens it with `THREAD_SUSPEND_RESUME`, and calls
`ResumeThread` only after `AssignProcessToJobObject` succeeds.  If enumeration,
assignment, or resumption fails, startup fails closed and the suspended process
is terminated; an unsupervised service is never left behind.

## `ctypes`, and the bug that only appears on 64-bit Windows

Every `kernel32` function Devbox calls has an explicit `argtypes` and `restype`
(`devbox.host.winapi.SIGNATURES`).  This is not tidiness.  Without them `ctypes`
assumes `restype = c_int` — 32 bits — while `HANDLE` is a 64-bit pointer, so
every handle returned by `OpenProcess`, `CreateJobObjectW` and `OpenJobObjectW`
would be truncated and sign-extended, and passed back to `CloseHandle` in that
state.

Handle values are usually small enough to survive the round trip by luck, which
is exactly what makes the mistake expensive: it works on the machine where it was
written and misbehaves somewhere else.

`tests/unit/test_windows_contract.py` parses this module **on Linux** and fails
if a `kernel32` call has no declared signature, if a `HANDLE` is declared as an
`int`, if an argument count disagrees with the Win32 declaration, or if an ANSI
(`…A`) variant is used where the wide one is needed.

## PID reuse

Windows recycles PIDs aggressively.  Devbox stores the process **creation time**
next to every PID, read with `GetProcessTimes` on a handle opened with
`PROCESS_QUERY_LIMITED_INFORMATION` — a right a standard user has for their own
processes, with no elevation and no `SeDebugPrivilege`.

Before stopping anything, the recorded creation time is compared with the live
one.  A mismatch means the number now belongs to somebody else, and Devbox
refuses and says so.

## Quoting

Everything is an argv list; `shell=True` appears nowhere.  Arguments containing
spaces, `&&`, `|`, `^`, `%PATH%`, quotes and accents reach the child byte for
byte.  Verified by running a real process that prints its `argv` back, on Windows
CI.

Project paths with spaces — `C:\Users\Jean Dupont\Mes Projets\portail café` — are
used deliberately throughout the suite, because that is where quoting bugs live.

## `.cmd` shims

`npm`, `yarn` and `pnpm` are `.cmd` files.  `CreateProcess` needs the extension,
so executable resolution tries `.exe`, `.cmd`, `.bat`, `.com`, `.ps1` — searching
the **child's own `PATH`**.

## Console encoding

A `cp1252` console cannot print `✓`.  Devbox probes what the output stream can
encode once, at start-up, and falls back to `[OK]` / `[!!]` — rather than dying
with `UnicodeEncodeError` halfway through a status table, which is what happens
to tools that assume UTF-8.  Writing is also guarded, so a service name with an
accent cannot crash the renderer on a legacy code page.

## Log encodings

Children write raw bytes.  Reading them back handles four cases, in order:

1. a BOM (UTF-8, UTF-16 LE/BE, UTF-32) — authoritative, and stripped;
2. strict UTF-8;
3. the ANSI code page (`cp1252` by default);
4. UTF-8 with replacement characters.

A line is **always** returned: an unreadable byte must never be why a developer
cannot see their service's error.

PowerShell's `>` redirection writes UTF-16 LE with a BOM, which is case 1.

## Filesystem

| | |
|---|---|
| Root | `%LOCALAPPDATA%\Devbox` — per-user, no elevation |
| Never | `Program Files`, `HKLM`, a Windows service, the system `PATH`, UAC |
| Reserved names | `CON`, `NUL`, `COM1`… refused in any path Devbox builds |
| Read-only files | cleared before deleting — `shutil.rmtree` raises `PermissionError` on them, and Node and PostgreSQL archives both contain some |
| Locked directories | a runtime cannot be replaced while a service runs from it; Devbox says so instead of raising `FileExistsError` |
| Name lengths | service, database and infrastructure names are capped at 64 characters, because they become directory names *and* Win32 object names |
| Trailing dot/space | refused — Win32 silently rewrites `evil.exe.` to `evil.exe` |
| Illegal characters | `< > : " \| ? *` refused in names Devbox composes |
| Long paths | tested; the test reports and skips if the host has not enabled them |

## Zero privileges — how to check

```powershell
whoami /priv                # no SeDebugPrivilege needed
devbox up                   # no UAC prompt
Get-Service devbox*         # nothing registered
Get-ChildItem "$env:LOCALAPPDATA\Devbox"
[Environment]::GetEnvironmentVariable('PATH', 'Machine')   # unchanged
```

`tests/windows/test_windows_environment.py::TestPaths` asserts the same things.

## What is verified where

| Behaviour | Linux container | Windows CI |
|---|---|---|
| Config, lockfile, graph, hashing, extraction, state, PATH composition | ✅ real | ✅ real |
| Process start/stop, trees, logs, exec, healthchecks | ✅ real (process groups) | ✅ real (Job Objects) |
| `winapi.py` — `GetProcessTimes`, `CreateJobObjectW`, `OpenJobObjectW`, `TerminateJobObject` | ❌ cannot execute (signatures and structures **are** checked) | ✅ real |
| `.cmd` resolution, `CTRL_BREAK_EVENT`, cp1252 console, `%LOCALAPPDATA%` | ❌ cannot execute | ✅ real |
| That no module outside `host/` branches on the OS, and that no POSIX-only API is used in code or tests the Windows job runs | ✅ real (AST) | ✅ real |

Nothing in the right-hand column is claimed as verified from a Linux run.  The
`windows-latest` job is what makes those rows true, and
[limitations.md](limitations.md) lists what remains unproven even there.
