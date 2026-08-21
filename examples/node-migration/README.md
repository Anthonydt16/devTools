# Example — migrating from Node 18 to Node 24

This is the case Devbox exists for.  `legacy-api` cannot move off Node 18 yet;
`modern-api` is being written on Node 24.  Both have to run **at the same time**,
on a corporate Windows 11 laptop, with no Docker, no WSL and no admin rights.

```
examples/node-migration/
├── devbox.yml
├── legacy-api/   Node 18.19.0, port 3001
└── modern-api/   Node 24.6.0,  port 3002
```

## Run it

```powershell
cd examples\node-migration
devbox install          # resolves devbox.lock, downloads and verifies both runtimes
devbox up               # starts both, waits until both answer their healthcheck
```

Expected output:

```
Project: node-migration (local)
  ✓ node 18.19.0         cached
  ✓ node 24.6.0          cached
  ✓ legacy-api           running (pid 8132) on port 3001
  ✓ modern-api           running (pid 9164) on port 3002

Services
  ✓  legacy-api  running  PID 8132  :3001
  ✓  modern-api  running  PID 9164  :3002

All services healthy
```

## Verify the thing that matters

Two Node versions, at the same time, in the same project:

```powershell
devbox exec legacy-api node --version   # v18.19.0
devbox exec modern-api node --version   # v24.6.0
```

Neither is on your machine `PATH`; both come from
`%LOCALAPPDATA%\Devbox\runtimes\node\`.  Check with:

```powershell
devbox ps
devbox status
curl http://127.0.0.1:3001/health
curl http://127.0.0.1:3002/health
devbox logs legacy-api
devbox logs modern-api --follow
```

Test the same code on both versions without switching anything:

```powershell
devbox run --node 18.19.0 node --version
devbox run --node 24.6.0  node --version
```

## Stop it

```powershell
devbox down
devbox ps        # both exited
```

`devbox down` stops these two services and their child processes.  It does not
touch shared infrastructure — see `docs/shared-infrastructure.md`.

## What failure looks like

Break something on purpose — change `legacy-api`'s port to one that is already
taken, or make `server.js` throw — and `devbox up` reports it instead of hanging:

```
✗ legacy-api failed to start
  Command:   node server.js
  Exit code: 1
  stderr:
    Error: listen EADDRINUSE: address already in use 127.0.0.1:3001
  Logs:      .devbox\logs\legacy-api.err.log
```

## Running this example without Node installed

`server.js.fake`, next to each `server.js`, tells Devbox's fake runtime how to
stand in for that server: which port to bind and which path answers the
healthcheck.  Real Node ignores the file completely.

That is how `tests/integration/test_example_node_migration.py` runs this exact
example in CI — the same `devbox.yml`, the same commands, the same healthchecks
— on a Linux container with neither Node version installed and no network
access.  See `docs/testing.md`.
