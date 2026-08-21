# Security

Devbox downloads executable code from a network location, extracts it, and runs
it with the developer's own privileges.  That is the threat model, and it is
taken seriously.  Every item below has an automated test; the file and class are
named.

## Integrity — nothing runs unverified

**The rule: Devbox never executes an artifact whose integrity has not been
established.**

| Step | Behaviour |
|---|---|
| Cache lookup | keyed by SHA256, so a hit *is* proof of integrity |
| Download | streamed to a temp file in `downloads\`, hashed on the way through |
| Resume | allowed, then the **whole** file is re-hashed — a partial file is only trustworthy if the complete digest matches |
| Verification | compared against the digest `devbox.lock` pins |
| Mismatch | the file is **deleted**; nothing is extracted, nothing is executed |
| Install | atomic rename into place, completion marker written last |

If the registry now advertises a different digest than the lockfile pins, the
**lockfile wins** and the install is refused.  A republished artifact under an
existing version is exactly what a lockfile exists to catch.

*Tested*: `tests/integration/test_runtime_install.py::TestIntegrity`,
`tests/unit/test_non_regression.py::TestRule04_IntegrityMismatchRefusesInstall`.

## Archive extraction — treated as hostile input

Every entry is validated **before** a byte is written:

| Attack | Result |
|---|---|
| `../../evil.exe`, `..\..\evil.exe` | refused, named in the error |
| `/etc/passwd`, `C:\Windows\System32\...` | refused (absolute) |
| `\\server\share\...` | refused (UNC) |
| `CON`, `NUL`, `COM1`, `LPT1` | refused (opening them has side effects) |
| `evil.exe.` (trailing dot or space) | refused — Win32 silently rewrites the name |
| symlink / hardlink / junction | refused; no legitimate runtime archive needs one |
| character device, FIFO | refused |
| declared size vs actual size | checked against **real** bytes, because a header can lie |
| entry count, per-entry size, total size | bounded (200 000 / 2 GiB / 4 GiB) |

The destination is proven to be inside the target directory **after** resolution
too, so a symlink or junction already inside the staging tree cannot be used to
escape.

Extraction is staged in a sibling directory and moved into place in one atomic
rename.  A refused archive leaves **nothing** behind — not a partial install, not
a staging directory.

*Tested*: `tests/unit/test_archive_security.py` (30 cases),
`tests/unit/test_non_regression.py::TestRule05_ZipSlipIsRejected`.

## Command execution — no shell, ever

`subprocess` is always called with an **argv list**.  `shell=True` appears
nowhere in the package, and a test parses the whole source tree with `ast` to
prove it — grepping would have been fooled by the docstrings that mention the
ban.

A command written in `devbox.yml` goes through `split_command`, which applies
POSIX quoting rules and produces a tuple:

```yaml
command: npm run build -- --out "dist folder" && rm -rf /
```
```
("npm", "run", "build", "--", "--out", "dist folder", "&&", "rm", "-rf", "/")
```

`&&` is a literal argument to `npm`.  Nothing is re-interpreted, on either
platform.

Executables are resolved through the **child's own `PATH`**, never the machine's.

*Tested*: `tests/unit/test_non_regression.py::TestRule08_ArgumentsAreNotReinterpreted`,
and by running a real process with those arguments in
`tests/integration/test_process_supervisor.py` and `tests/windows/`.

## SQL identifiers — allowlisted, not escaped

SQL identifiers cannot be parameterised, so the only safe rule is refusal:

```
clients; DROP DATABASE production; --     refused
clients'--                                refused
clients`                                  refused
customer_portal-v2                        accepted
```

Letters, digits, `_`, `-` and `$`; not starting with a digit; at most 63
characters.  Anything else never reaches a server.

*Tested*: `tests/unit/test_database_manager.py::TestSqlIdentifierSafety`.

## Transport

| Scheme | Accepted |
|---|---|
| `https://` | yes — system trust store, verification never disabled |
| `http://127.0.0.1`, `localhost` | yes — test fixtures only |
| `http://` remote | **refused** |

## Process ownership — never kill somebody else's program

A PID means nothing after a reboot.  Every state record stores the process
**creation time** (`GetProcessTimes` on Windows, `/proc/<pid>/stat` field 22 on
Linux).  Before signalling anything, Devbox re-reads it:

| Situation | Action |
|---|---|
| alive, creation time matches | stop it |
| PID gone | already stopped; not an error |
| alive, creation time differs | **refused** and reported: `pid 8132 is alive but belongs to another program now` |
| no creation time recorded | **refused** — ownership cannot be proven |

Devbox never kills a process because it "looks like node".

*Tested*: `tests/unit/test_non_regression.py::TestRule07_PidReuseIsRefused`,
`tests/integration/test_process_supervisor.py::TestOwnership`,
`tests/windows/test_process_tree.py::TestPidReuse`.

## Secrets

* no secret in `devbox.yml` — only `*_env` keys naming an environment variable;
* passwords are passed through the **environment**, never on a command line
  (a command line is visible to every other process on the machine);
* `environment_for_display` masks anything whose name contains `PASSWORD`,
  `SECRET`, `TOKEN` or `API_KEY` before it reaches the terminal or a log.

## Filesystem

Everything Devbox writes is under `%LOCALAPPDATA%\Devbox` (per-user, no
elevation) or the project's own `.devbox\`.  Never `Program Files`, never the
machine registry, never a Windows service, never the system `PATH`, never a UAC
prompt.

State and lockfile writes are atomic (temp file then `replace`), so a machine
that loses power finds either the old file or the new one — never half of either.
Damaged state is **quarantined**, not deleted: losing the ability to stop a
service is bad, silently destroying the evidence of why is worse.

*Tested*: `tests/integration/test_autonomy.py::TestIsolationFromTheMachine`,
`tests/windows/test_windows_environment.py::TestPaths`.

## Reporting a problem

Open an issue with the `devbox.yml`, the command, and `devbox status --debug`.
Do not include the contents of any variable named in a `*_env` key.
