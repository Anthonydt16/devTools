# Fake runtimes

These fixtures let the whole orchestrator be tested on a machine with **no Node,
no Python runtime install, no PostgreSQL and no network**.

A fake runtime is a real directory tree that looks like a real runtime:

```
<install root>/
├── node            # POSIX launcher (shell script)
├── node.cmd        # Windows launcher (batch file)
└── runtime_main.py # the payload
```

The launcher bakes the version in, so the executable found through the child's
`PATH` decides what `node --version` prints.  That is what makes non-regression
test #1 — two services on two Node versions at the same time — a real test
rather than an assertion about a mock.

`runtime_main.py` can:

| Sub-command | Behaviour |
|---|---|
| `--version` | print `v<version>` |
| `serve --port N` | bind the port, answer HTTP `/health`, stay alive, log to stdout *and* stderr |
| `sleep --seconds N` | stay alive, do nothing |
| `fail --code N --message M` | write `M` to stderr, exit `N` |
| `crash` | exit 1 immediately |
| `spawn-child --port A --child-port B` | start a grandchild that only a process-tree kill can stop |
| `echo-args ...` | print every argument verbatim (argument-injection tests) |
| `print-env NAME` / `print-path` | prove which environment the child received |

Build one from Python:

```python
from devbox.testing.fake_runtime import build_fake_runtime, build_fake_registry

install = build_fake_runtime(tmp_path / "node-18", name="node", version="18.19.0")
registry_root = build_fake_registry(tmp_path / "registry", [("node", "18.19.0")])
```

`build_fake_registry` also produces the `index.json` and the SHA256 digests, so
the download → verify → extract → install pipeline runs for real against a
local directory with no network involved.
