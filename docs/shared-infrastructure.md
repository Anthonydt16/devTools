# Shared infrastructure

## The rule

**One PostgreSQL on the machine, not one per project.**

```
PostgreSQL 17 (127.0.0.1:5432)     MySQL 8.4 (127.0.0.1:3306)
├── clients                        ├── legacy
├── auth                           └── reporting
├── facturation
└── documents

customer-portal  → clients, auth
billing          → clients, facturation
archive          → documents, auth
```

Three projects, one server, four databases.  Starting the fourth project does not
start a fourth PostgreSQL, and shutting down the first does not take the other
three with it.

This is not an optimisation.  It is how a company environment actually works, and
building it any other way makes local development diverge from every other
environment.

## Who owns what

| Started by | Ownership | Stopped by |
|---|---|---|
| `devbox infra up` | **global** — the machine's | `devbox infra down` only |
| `devbox up` (on demand) | the project, as a **claim** | the last project to release it |

```powershell
devbox infra up                  # the machine's servers, for the day
devbox infra status
devbox infra logs company-postgres --follow
devbox infra down                # the only thing that stops a global instance
```

`devbox down` on a project:

1. stops that project's services;
2. releases its claim on each shared server;
3. stops a server **only** if nothing else claims it **and** it was not started
   globally.

```
  ✓ backend              stopped
  ✓ frontend             stopped

Left running (shared):
    company-postgres: started by 'devbox infra up'; stop it with 'devbox infra down'
    company-mysql: still used by 1 other project
```

Devbox says what it left running and why, rather than silently doing less than
asked.

## Data

```
%LOCALAPPDATA%\Devbox\infra\postgres\company-postgres\
├── data\      the cluster — every project's databases live here
└── logs\
```

**Stopping never deletes data.**  `devbox down`, `devbox infra down` and a
crashed machine all leave the data directory exactly where it was.  Removing it
is a deliberate act you perform yourself:

```powershell
devbox infra down
Remove-Item -Recurse "$env:LOCALAPPDATA\Devbox\infra\postgres\company-postgres\data"
```

## No privileges

The servers run as **you**:

* binaries come from a runtime Devbox downloaded and SHA256-verified;
* the data directory is under your profile;
* PostgreSQL is started as a foreground `postgres` process supervised by Devbox
  — not `pg_ctl start`, which daemonises and hands back a PID Devbox does not
  own, breaking both tree shutdown and the ownership check;
* it is stopped with `pg_ctl stop -m fast`, because a signalled PostgreSQL
  spends its next start-up in recovery and the developer assumes Devbox broke
  something;
* it listens on `127.0.0.1` only.

No Windows service is registered, no installer runs, and no UAC prompt appears.

## Logical databases

```yaml
databases:
  clients:
    provider: postgres
    infrastructure: company-postgres
    database: clients
    init:
      if_missing:
        command: npm run db:init
```

On `devbox up`, for each database: is the server up? does the database exist?
If it exists — **stop**.  Not recreated, not migrated, not re-initialised.  It may
hold another project's data, or three weeks of yours.

If it does not exist, Devbox creates it and runs `init.if_missing` **once**, in
that run.  The command receives `DATABASE_URL` and `DEVBOX_DATABASE_NAME`.

A failed init reports the command's own output and leaves the database in place:

```
✗ initialisation of database 'clients' failed with exit code 1
  command: npm run db:init
  output:  migration 004_add_index.sql failed: relation "clients" does not exist

  The database 'clients' was created and is left in place. Fix the command and
  run 'devbox up' again, or drop the database to re-run initialisation.
```

## `managed` vs `external`

```yaml
infrastructure:
  company-postgres:
    provider: postgres
    mode: managed          # Devbox starts it, stops it, owns its data

  prod-postgres:
    provider: postgres
    mode: external         # somebody else's; Devbox never touches it
    url_env: PROD_DATABASE_URL
```

For an `external` server Devbox will health-check it and inject its URL, and
**that is all**: never started, never stopped, no database created, no init run.

That is why a production environment cannot boot a local database by accident,
and why a named non-local environment that still declares `managed`
infrastructure is a configuration error.

## Adding a provider

Redis is out of scope for the MVP, but nothing in the orchestrator names
PostgreSQL or MySQL.  Adding `company-redis` is:

```python
from devbox.catalogs.providers import ProviderDescriptor, register_provider_descriptor
from devbox.infra.provider import register_provider

REDIS = register_provider_descriptor(
    ProviderDescriptor(
        name="redis",
        default_port=6379,
        default_version="7.4",
        url_scheme="redis",
        runtime_kind="redis",
    )
)


class RedisProvider:
    @property
    def descriptor(self):
        return REDIS

    def needs_initialisation(self, context):
        return False

    def initialise(self, context):
        pass

    def start_command(self, context):
        return (context.executable("redis-server"), "--port", str(context.port))

    def stop_command(self, context):
        return None

    def healthcheck(self, context):
        from devbox.infra.provider import default_healthcheck

        return default_healthcheck(context)

    def connection_url(self, context, database, *, username="", password=""):
        return f"redis://{context.host}:{context.port}/{database}"

    def database_client(self, context): ...


register_provider(RedisProvider())
```

`tests/unit/test_infra_manager.py` drives a full lifecycle for a provider the
core has never heard of, and `devbox/testing/fake_provider.py` is a complete
working one.

## Two machines, same project

`project_id` is derived from the **resolved absolute path** of the project
directory, so two clones of the same repository in two folders are two projects
that can run side by side against the same shared servers.
