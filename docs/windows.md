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
CreateProcess(...)                      # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
AssignProcessToJobObject(job, process)  # descendants inherit the job
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

**Graceful before forceful.**  `CREATE_NEW_PROCESS_GROUP` lets Devbox send
`CTRL_BREAK_EVENT` so a well-behaved server can flush and close its listeners.
`TerminateJobObject` follows after `stop_timeout`.

Since Windows 8, jobs nest, so assigning a process that already belongs to
another job (a CI agent's, for instance) succeeds instead of failing with
`ERROR_ACCESS_DENIED`.

### The assignment race

`subprocess.Popen` does not expose the primary thread handle, so a process cannot
be created suspended and resumed after being assigned to the job.  A descendant
spawned between `CreateProcess` and `AssignProcessToJobObject` would escape it.

The window is microseconds and no interpreter or package manager has finished
start-up in that time.  Closing it properly would mean reimplementing
`CreateProcess` with `CREATE_SUSPENDED` via `ctypes`.  Recorded in
[limitations.md](limitations.md); measured cost so far: zero observed escapes.

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
| `winapi.py` — `GetProcessTimes`, `CreateJobObjectW`, `OpenJobObjectW`, `TerminateJobObject` | ❌ cannot execute | ✅ real |
| `.cmd` resolution, `CTRL_BREAK_EVENT`, cp1252 console, `%LOCALAPPDATA%` | ❌ cannot execute | ✅ real |

Nothing in the right-hand column is claimed as verified from a Linux run.  The
`windows-latest` job is what makes those rows true, and
[limitations.md](limitations.md) lists what remains unproven even there.
