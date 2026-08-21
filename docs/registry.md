# Runtime registry

A registry is where runtime artifacts come from.  Devbox ships two
implementations behind one narrow protocol, so a company registry with
authentication, mirroring or signing can be added without touching the download
pipeline, the cache or the orchestrator.

## Layout

```
registry/
├── index.json
├── node/
│   ├── 18.19.0/windows-x64.zip
│   └── 24.6.0/windows-x64.zip
└── postgres/
    └── 17/windows-x64.zip
```

```json
{
  "schema": 1,
  "runtimes": {
    "node": {
      "24.6.0": {
        "windows-x64": {
          "url": "node/24.6.0/windows-x64.zip",
          "sha256": "9f2c...64 hex characters...",
          "size": 31457280,
          "archive": "node-24.6.0-windows-x64.zip"
        }
      }
    }
  }
}
```

`url` may be relative to the index (resolved inside the registry root) or an
absolute `https://` URL.

**`sha256` is mandatory.** A registry that cannot state the digest of what it
serves cannot be used, because Devbox refuses to execute an artifact whose
integrity was never established.

## Pointing Devbox at one

```powershell
devbox --registry \\fileserver\devbox-runtimes install
devbox --registry https://runtimes.example.com/devbox install
$env:DEVBOX_REGISTRY = 'https://runtimes.example.com/devbox'
```

A path, a UNC share or an `https://` URL all work.

## Transport rules

| Scheme | Accepted |
|---|---|
| `https://` | yes — TLS verified against the system trust store, never disabled |
| `http://127.0.0.1`, `http://localhost` | yes — this is what the test fixtures use |
| `http://` anywhere else | **refused** |
| filesystem path, UNC | yes |

Runtime artifacts are executable code.  A corporate laptop must not be talked
into fetching them over a downgraded connection, so plaintext HTTP to a remote
host is a hard error rather than a warning.

Filesystem registries resolve artifact paths **inside** the registry root: an
index pointing at `../../etc/passwd` is refused.

## Building one

```python
import hashlib, json, shutil
from pathlib import Path

root = Path("registry")
index = {"schema": 1, "runtimes": {}}

for name, version, source in [
    ("node", "18.19.0", Path("downloads/node-v18.19.0-win-x64.zip")),
    ("node", "24.6.0", Path("downloads/node-v24.6.0-win-x64.zip")),
]:
    target = root / name / version / "windows-x64.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    data = target.read_bytes()
    index["runtimes"].setdefault(name, {})[version] = {
        "windows-x64": {
            "url": f"{name}/{version}/windows-x64.zip",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "archive": source.name,
        }
    }

(root / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True))
```

Serve it with any static file server, or put it on a share.  Nothing dynamic is
required.

## A registry for tests

`devbox.testing` builds a complete one — real archives, real digests, real
executables — with no network:

```python
from devbox.testing import build_fake_registry

registry = build_fake_registry(
    tmp_path / "registry",
    [("node", "18.19.0"), ("node", "24.6.0")],
    platform="windows-x64",
    extra_executables=("npm",),
)
```

`corrupt=[("node", "18.19.0")]` makes the index advertise a wrong digest while
the artifact stays valid — exactly the situation integrity checking exists for.

## Adding a registry implementation

Implement four members:

```python
class RuntimeRegistry(Protocol):
    @property
    def location(self) -> str: ...
    @property
    def supports_resume(self) -> bool: ...
    def resolve(self, name: str, version: str, platform: Platform) -> RuntimeManifest: ...
    def open_stream(
        self, manifest: RuntimeManifest, *, offset: int = 0
    ) -> AbstractContextManager[BinaryIO]: ...
```

`open_stream` yields bytes; everything else — the temp file, the resume, the
hashing, the refusal, the atomic move — is handled by `Downloader` and is the
same for every registry.  `supports_resume` says whether `offset` is honoured.

## Publishing checklist

- one artifact per `(name, version, platform)`;
- SHA256 recorded in `index.json` and never changed for a published version —
  republish under a new version instead.  Devbox refuses an artifact whose digest
  no longer matches the lockfile, which is what a lockfile is for;
- `size` is advisory (progress display), not trusted;
- the archive should extract to a directory containing the executables, with or
  without a single vendor top-level folder (Devbox strips it).
