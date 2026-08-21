# Runtimes

## The promise

Two services, two Node versions, one `devbox up`, nothing installed globally:

```yaml
services:
  legacy-api:
    runtime: { node: "18.19.0" }
  modern-api:
    runtime: { node: "24.6.0" }
```

```powershell
devbox exec legacy-api node --version   # v18.19.0
devbox exec modern-api node --version   # v24.6.0
```

Both are running at the same time. Neither is on the machine `PATH`.

## How it works

Devbox never modifies the machine or user `PATH`, and never writes a shim file.
For **each child process** it composes a complete environment:

```
PATH = <runtimes>\node\18.19.0 ; <runtimes>\node\18.19.0\bin ; <inherited PATH>
```

and passes it to `CreateProcess`. The inherited part is kept so `git`, `cmd` and
the system tools stay reachable; the runtime entries come first, so a globally
installed Node can never win over the pinned one.

Because nothing global arbitrates, there is nothing to switch and nothing to race
over. Two children simply receive two different environments.

A handful of inherited variables are **removed** from the child environment
unless the service sets them itself — `NODE_OPTIONS`, `NODE_PATH`, `PYTHONPATH`,
`PYTHONHOME`, `VIRTUAL_ENV`, `CONDA_PREFIX`. An inherited `NODE_OPTIONS` breaks a
service in a way that is very hard to diagnose.

## Layout on disk

```
%LOCALAPPDATA%\Devbox\
├── runtimes\
│   └── node\
│       ├── 18.19.0\
│       │   ├── node.exe
│       │   ├── npm.cmd
│       │   └── .devbox-runtime.json     completion marker
│       └── 24.6.0\
├── cache\        verified artifacts, keyed by SHA256
├── downloads\    partial downloads; everything here is disposable
├── infra\        shared server data and logs — never deleted by `devbox down`
├── logs\
├── state\        process ownership records
└── tmp\          staging for atomic moves, always on the same volume
```

Override the root with `DEVBOX_HOME` — useful to put runtimes on another drive:

```powershell
$env:DEVBOX_HOME = 'D:\devbox'
```

## Installing

```
devbox.yml  →  devbox.lock  →  cache  →  install
  intent        resolution     verified   extracted
```

`devbox install` resolves every runtime the project needs — including the one
backing a **managed** database server — writes `devbox.lock`, and installs.

Resolution is **sticky**: an entry that already exists for the requested name,
version and platform is kept untouched, so running `devbox install` twice
produces a byte-identical file. `--force` re-queries the registry on purpose.

```powershell
devbox install                 # resolve and install
devbox install --lock-only     # write devbox.lock, install nothing
devbox install --check         # fail if devbox.lock would change (for CI)
devbox install --force         # re-query the registry for every runtime
```

The install itself is transactional:

1. cache hit? the key **is** the SHA256, so a hit is proof of integrity;
2. otherwise stream to a temp file, hashing as the bytes arrive;
3. resume an interrupted download, then verify the **whole** digest;
4. mismatch → delete and refuse. Nothing is extracted, nothing is executed;
5. extract into a staging directory, refusing traversal, links and archive bombs;
6. one atomic rename into place;
7. write `.devbox-runtime.json` **last**.

That last point is what makes an interrupted install detectable rather than
usable: a directory with no marker is reported as incomplete and pruned, never
mistaken for a good install.

## Managing runtimes directly

```powershell
devbox runtime list
devbox runtime install node 24.6.0
devbox runtime remove node 18.19.0
```

`devbox runtime list` shows `ready` only when the install is complete **and** its
probe binary is where the runtime kind says it should be.

## Runtime kinds

A "kind" describes how one family lays itself out:

| Kind | `bin` directories | Probe | Extra environment |
|---|---|---|---|
| `node` | `.`, `bin` | `node` | `NODE_PATH=<root>/lib/node_modules` |
| `python` | `.`, `Scripts`, `bin` | `python` | |
| `postgres` | `bin`, `.` | `postgres` | |
| `mysql` | `bin`, `.` | `mysqld` | |

Node ships its executables at the archive root on Windows and under `bin/` on
POSIX; listing both keeps one description valid everywhere.

Adding a kind is a registration, not a change to the orchestrator:

```python
from devbox.catalogs.runtime_kinds import RuntimeKind, register_runtime_kind

register_runtime_kind(
    RuntimeKind(
        name="dotnet",
        bin_subdirs=("sdk", "."),
        probe_executable="dotnet",
        env={"DOTNET_ROOT": "{root}"},
    )
)
```

## `.cmd` shims on Windows

`npm`, `yarn` and `pnpm` are `.cmd` files. `CreateProcess` needs the extension,
so executable resolution tries `.exe`, `.cmd`, `.bat`, `.com` and `.ps1` in that
order — searching **the child's own `PATH`**, never the machine's. This is unit
tested on Linux (the predicate is injectable) and really executed on Windows.

## `devbox run`

Test the same code on two versions with nothing to switch:

```powershell
devbox run --node 18.19.0 npm test
devbox run --node 24.6.0  npm test
```

No service is involved; the environment is composed the same way. The
architecture does not stand in the way of a future
`test.matrix.node: ["18.19.0", "20", "22", "24"]` — that is out of scope for the
MVP, not designed out of it.
