salut 
# Devbox

**A user-space runtime and process orchestrator for Windows 11.**
`docker compose` ergonomics — with no Docker, no WSL, no Hyper-V, no containers
and **no administrator rights**.

```powershell
git clone <repo>; cd <repo>
devbox up
```

```
Project: customer-portal (local)
  ✓ node 18.19.0         cached
  ✓ node 24.6.0          cached
  ✓ company-postgres     running on 127.0.0.1:5432
  ✓ company-mysql        running on 127.0.0.1:3306
  ✓ clients              ready
  ✓ backend              running (pid 8132) on port 3000
  ✓ frontend             running (pid 9164) on port 5173

Services
  ✓  backend   running  PID 8132  :3000
  ✓  frontend  running  PID 9164  :5173

All services healthy
```

---

## Why

On a locked-down corporate Windows 11 laptop you often cannot install Docker
Desktop, cannot enable Hyper-V, cannot enable WSL, and cannot write to
`Program Files`. But you still have to run a project that needs **Node 18 and
Node 24 at the same time**, plus PostgreSQL and MySQL that four other projects
also use.

Devbox does that with what a standard user *does* have: their own profile
directory and the ability to start processes.

| | |
|---|---|
| Runtimes | downloaded to `%LOCALAPPDATA%\Devbox\runtimes`, SHA256-verified, never installed system-wide |
| `PATH` | built **per child process** — never the machine `PATH`, never a shim on disk |
| Process trees | Windows **Job Objects**, so `devbox down` takes the children with it |
| Databases | **one shared** PostgreSQL and MySQL per machine, serving every project |
| Privileges | none. No service, no registry key, no `Program Files`, no UAC prompt |

## Install

```powershell
git clone <repo>
cd <repo>
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
devbox test
```

`devbox test` is the single command that checks the whole project: lint, types,
unit tests, integration tests, Windows tests, configuration and packaging. It
exits non-zero if anything mandatory fails, and it tells you what it skipped and
why.

Prefer a single executable? `pip install -e ".[package]"` then
`python -m devbox.packaging.build_exe` produces `dist\devbox.exe`.

## Use

```powershell
devbox init                      # write a starter devbox.yml
devbox install                   # resolve devbox.lock, download + verify runtimes
devbox up                        # start everything, wait until healthy
devbox ps                        # what is running
devbox status                    # runtimes, infrastructure and services
devbox logs backend --follow     # stdout and stderr, separately
devbox exec backend node --version
devbox run --node 18.19.0 npm test
devbox down                      # stop this project only
```

Machine-wide shared servers:

```powershell
devbox infra up                  # start the shared PostgreSQL / MySQL
devbox infra status              # what is running, and who is using it
devbox infra logs company-postgres
devbox infra down                # only this stops a globally started server
```

Runtimes and artifacts:

```powershell
devbox runtime list
devbox runtime install node 24.6.0
devbox runtime remove node 18.19.0
devbox build --version 1.4.2     # reproducible dist\my-api-1.4.2.zip + manifest
```

## `devbox.yml`

```yaml
version: 1
project: customer-portal

infrastructure:
  company-postgres:
    provider: postgres
    version: "17"
    mode: managed          # managed | external
    port: 5432

databases:
  clients:
    provider: postgres
    infrastructure: company-postgres
    database: clients
    init:
      if_missing:
        command: npm run db:init   # runs once, only when Devbox creates it

services:
  backend:
    runtime: { node: "18.19.0" }
    working_dir: ./backend
    command: npm run dev
    env:
      DATABASE_URL: ${database.clients.url}
    depends_on: [company-postgres, clients]
    healthcheck:
      http: { url: "http://127.0.0.1:3000/health", timeout: 30, retries: 10 }

  frontend:
    runtime: { node: "24.6.0" }
    working_dir: ./frontend
    command: npm run dev
    healthcheck:
      tcp: { host: 127.0.0.1, port: 5173 }

environments:
  production:
    infrastructure:
      company-postgres: { mode: external, url_env: PROD_DATABASE_URL }
```

`devbox.yml` is *intent*. `devbox.lock` is the exact resolution — artifact,
source and SHA256 per runtime — and is committed to Git. Full schema in
[docs/devbox-yml.md](docs/devbox-yml.md).

**No secret ever goes in `devbox.yml`.** Only `*_env` references naming an
environment variable.

## The example

[`examples/node-migration/`](examples/node-migration/) is the case Devbox exists
for: `legacy-api` on Node 18.19.0 and `modern-api` on Node 24.6.0, running at the
same time.

```powershell
cd examples\node-migration
devbox install
devbox up
devbox exec legacy-api node --version   # v18.19.0
devbox exec modern-api node --version   # v24.6.0
devbox down
```

It is run automatically in CI against fake runtimes, on a machine with neither
Node version installed — see [docs/testing.md](docs/testing.md).

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | how it is put together, and what was rejected |
| [devbox-yml.md](docs/devbox-yml.md) | the full configuration schema |
| [runtimes.md](docs/runtimes.md) | how runtime isolation actually works |
| [registry.md](docs/registry.md) | building a company runtime registry |
| [shared-infrastructure.md](docs/shared-infrastructure.md) | one PostgreSQL for every project |
| [security.md](docs/security.md) | the threat model and what is checked |
| [windows.md](docs/windows.md) | Job Objects, PID reuse, quoting, code pages |
| [testing.md](docs/testing.md) | what is really tested, and how, without Node |
| [development.md](docs/development.md) | working on Devbox itself |
| [limitations.md](docs/limitations.md) | what is not done, and what is untested here |
| [plan.md](docs/plan.md) | the phases and the architecture decisions |

## Diagnosing a failure

```
✗ backend failed to start
  Command:   npm run dev
  Exit code: 1
  stderr:
    Error: connect ECONNREFUSED 127.0.0.1:5432
  Logs:      .devbox\logs\backend.err.log

  Full output: .devbox\logs\backend.err.log
```

`--debug` adds the traceback. `devbox status` shows what Devbox believes about
runtimes, infrastructure and services. `devbox logs <service>` has the rest.

## Licence

MIT.
