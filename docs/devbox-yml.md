# `devbox.yml`

The whole schema, with what is validated and what the error looks like.

Every problem is reported **at once**, located by a dotted path, **before**
anything starts:

```
devbox.yml: invalid configuration
  - services.backend.command: expected a string, got an integer (3)
  - services.backend.depend_on: unknown key 'depend_on', did you mean 'depends_on'? (allowed: command, depends_on, env, healthcheck, port, runtime, stop_timeout, working_dir)
  - services.frontend.healthcheck.tcp.port: is required
```

## Top level

| Key | Type | Required | Meaning |
|---|---|---|---|
| `version` | integer | **yes** | Schema version. Only `1` is understood. |
| `project` | string | no | Project name. Defaults to the directory name. |
| `infrastructure` | mapping | no | Shared servers this project uses. |
| `databases` | mapping | no | Logical databases inside those servers. |
| `services` | mapping | no | Processes Devbox supervises. |
| `environments` | mapping | no | Per-environment overrides. |

Unknown keys are refused, with a suggestion when one is close.

Names — of services, databases and infrastructure — may contain letters, digits,
`-`, `_` and `.` only. They become directory and log-file components.

## `services`

```yaml
services:
  backend:
    runtime: { node: "18.19.0" }
    working_dir: ./backend
    command: npm run dev
    env:
      DATABASE_URL: ${database.clients.url}
      PORT: "3000"
    depends_on: [company-postgres, clients]
    port: 3000
    stop_timeout: 10
    healthcheck:
      http:
        url: "http://127.0.0.1:3000/health"
        interval: 1
        timeout: 30
        retries: 10
        start_period: 5
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `command` | string or list | **required** | Split with POSIX quoting rules. **Never** run through a shell. |
| `runtime` | `{name: version}` | `{}` | Every name must be a known runtime kind. |
| `working_dir` | string | `.` | Relative to the project root. |
| `env` | mapping | `{}` | Values are stringified; `${...}` is resolved at start-up. |
| `depends_on` | list of strings | `[]` | Services, databases or infrastructure. |
| `healthcheck` | mapping | none | Exactly one of `http` or `tcp`. |
| `port` | integer | inferred | Display only. Inferred from the healthcheck. |
| `stop_timeout` | number | `10` | Seconds to wait for a graceful stop. |

`command` accepts a YAML list when you need arguments passed verbatim:

```yaml
command: ["node", "--max-old-space-size=4096", "server.js"]
```

Everything is passed as an argv list to `CreateProcess`. A command containing
`&& rm -rf /` produces four literal arguments; nothing is re-interpreted.

## `healthcheck`

Started is not ready. A service with no healthcheck is only checked for having
survived its first moment — Devbox does not claim health it cannot observe.

```yaml
healthcheck:
  http:
    url: "http://127.0.0.1:3000/health"   # required, http:// or https://
    method: GET                           # GET | HEAD | POST
    expect_status: [200, 204]             # default: any 2xx or 3xx
    interval: 1                           # seconds between attempts
    timeout: 30                           # seconds for one attempt
    retries: 10                           # attempts before failing
    start_period: 5                       # grace period before the first attempt
```

```yaml
healthcheck:
  tcp:
    host: 127.0.0.1
    port: 5173
    interval: 0.5
    timeout: 2
    retries: 30
```

Waiting **aborts the moment the process exits** — waiting 30 seconds for a
service that died two seconds ago hides the real error, which is in its stderr.

## `infrastructure`

```yaml
infrastructure:
  company-postgres:
    provider: postgres      # postgres | mysql | any registered provider
    version: "17"
    mode: managed           # managed | external
    host: 127.0.0.1
    port: 5432
    username: devbox
    password_env: PG_PASSWORD    # the *name* of a variable, never a password
    options: {}
```

| `mode` | Meaning |
|---|---|
| `managed` | Devbox starts, stops and owns its data directory. |
| `external` | It lives elsewhere. Devbox **never** starts, stops, creates or initialises anything in it. `url_env` is required. |

```yaml
  prod-postgres:
    provider: postgres
    mode: external
    url_env: PROD_DATABASE_URL
```

Two managed servers cannot share a host and port; that is a configuration error,
not a runtime surprise.

The runtime backing a **managed** server is part of what `devbox install`
fetches: a project declaring managed PostgreSQL 17 needs the PostgreSQL 17
binaries, exactly as it needs Node. An `external` server needs nothing installed.

## `databases`

```yaml
databases:
  clients:
    provider: postgres
    infrastructure: company-postgres
    database: clients          # defaults to the key
    username: app
    password_env: APP_DB_PASSWORD
    init:
      if_missing:
        command: npm run db:init
        working_dir: backend
        runtime: { node: "18.19.0" }
```

Three rules, each with a test:

* an **existing** database is never recreated and never re-initialised;
* `init.if_missing` runs **exactly once** — in the run that created the database;
* nothing is created inside an `external` server.

A failed `init` reports the command's own output and **leaves the database in
place**: deleting it would destroy whatever the half-finished initialisation did
manage to write, and hide why it failed.

## `${...}` interpolation

Resolved at start-up in `env` values, in `command` and in `working_dir`.
Validated at **load** time: a reference to something the project does not declare
is a configuration error, not a service that starts and then cannot connect.

| Reference | Yields |
|---|---|
| `${database.<name>.url}` | full connection URL |
| `${database.<name>.host\|port\|user\|password\|name}` | its parts |
| `${infrastructure.<name>.url\|host\|port}` | the server itself |
| `${env.VARIABLE}` | an environment variable (error if unset) |
| `${project.name\|root}` | the project |
| `$$` | a literal `$` |

Substitution happens **before** the command is split, so a substituted value
containing a space becomes one argument only if it is quoted in `devbox.yml` —
the same rule as any other argument.

## `environments`

Overlays are deep-merged into the root document **before** validation, so an
overlay can change any field and the result is validated exactly once.

```yaml
environments:
  local:
    infrastructure:
      company-postgres: { mode: managed }
  production:
    infrastructure:
      company-postgres: { mode: external, url_env: PROD_DATABASE_URL }
      company-mysql:    { mode: external, url_env: PROD_MYSQL_URL }
```

```powershell
devbox up --environment production
```

A named non-local environment that still declares **managed** infrastructure is
refused. That is the mistake that would make `devbox up --environment production`
boot a local PostgreSQL, so it is a configuration error rather than a runtime
one.

## Secrets

None, ever, in this file. Only `*_env` keys naming an environment variable:

```yaml
password_env: APP_DB_PASSWORD     # correct
password: hunter2                 # refused: unknown key
```

Passwords reach child processes through the environment, never through a command
line — a command line is visible to every other process on the machine.

## Minimal valid file

```yaml
version: 1
```

## Full example

See [`examples/node-migration/devbox.yml`](../examples/node-migration/devbox.yml),
which is loaded and started by the test suite on every run.
