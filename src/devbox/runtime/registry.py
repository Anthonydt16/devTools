"""Runtime registries: where artifacts come from.

Two implementations ship in the MVP and both satisfy the same protocol:

* :class:`FilesystemRegistry` — an ``index.json`` next to the artifacts, on a
  local disk or a mounted network share.  This is what a company would use
  behind a file server, and it is what the test suite uses (no network).
* :class:`HttpRegistry` — the same layout served over HTTP(S).

The protocol is deliberately narrow — resolve a manifest, stream bytes — so a
company registry with authentication, mirroring or signing can be added without
touching the download pipeline, the cache or the orchestrator.

Index layout::

    index.json
    node/24.6.0/windows-x64.zip

    {
      "schema": 1,
      "runtimes": {
        "node": {
          "24.6.0": {
            "windows-x64": {"url": "node/24.6.0/windows-x64.zip", "sha256": "...", "size": 0}
          }
        }
      }
    }

``url`` may be relative to the index; it is resolved against the registry root.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Final, Protocol, cast, runtime_checkable

from devbox.errors import RegistryError
from devbox.host.platform_info import Platform
from devbox.runtime.manifest import RuntimeManifest, manifest_from_document

__all__ = [
    "INDEX_FILENAME",
    "FilesystemRegistry",
    "HttpRegistry",
    "RuntimeRegistry",
    "registry_from_uri",
]

INDEX_FILENAME: Final = "index.json"
SUPPORTED_INDEX_SCHEMA: Final = 1
_CHUNK_SIZE: Final = 256 * 1024


@runtime_checkable
class RuntimeRegistry(Protocol):
    """Source of runtime artifacts."""

    @property
    def location(self) -> str:
        """Human-readable origin, shown in errors and written to the lockfile."""
        ...

    @property
    def supports_resume(self) -> bool:
        """True when :meth:`open_stream` honours ``offset``."""
        ...

    def resolve(self, name: str, version: str, platform: Platform) -> RuntimeManifest:
        """Return the manifest for one artifact, or raise :class:`RegistryError`."""
        ...

    def available_versions(self, name: str, platform: Platform | None = None) -> tuple[str, ...]:
        """Versions this registry can serve, sorted."""
        ...

    def open_stream(
        self, manifest: RuntimeManifest, *, offset: int = 0
    ) -> AbstractContextManager[BinaryIO]:
        """Context manager yielding a readable byte stream for *manifest*.

        *offset* resumes an interrupted download when
        :attr:`supports_resume` is true.
        """
        ...


class _IndexBackedRegistry:
    """Shared index parsing for the filesystem and HTTP registries."""

    __slots__ = ("_index_cache",)

    def __init__(self) -> None:
        self._index_cache: Mapping[str, object] | None = None

    # -- to implement in subclasses ------------------------------------------
    def _read_index_bytes(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def location(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _index(self) -> Mapping[str, object]:
        if self._index_cache is None:
            raw = self._read_index_bytes()
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                msg = f"registry index at {self.location} is not valid JSON: {exc}"
                raise RegistryError(msg) from exc
            if not isinstance(document, Mapping):
                msg = f"registry index at {self.location} must be a JSON object"
                raise RegistryError(msg)
            schema = document.get("schema", SUPPORTED_INDEX_SCHEMA)
            if schema != SUPPORTED_INDEX_SCHEMA:
                msg = (
                    f"registry index at {self.location} uses schema {schema!r}; this build of "
                    f"Devbox understands schema {SUPPORTED_INDEX_SCHEMA}"
                )
                raise RegistryError(msg)
            self._index_cache = document
        return self._index_cache

    def _runtimes(self) -> Mapping[str, object]:
        runtimes = self._index().get("runtimes", {})
        if not isinstance(runtimes, Mapping):
            msg = f"registry index at {self.location}: 'runtimes' must be an object"
            raise RegistryError(msg)
        return runtimes

    def resolve(self, name: str, version: str, platform: Platform) -> RuntimeManifest:
        runtimes = self._runtimes()
        versions = runtimes.get(name)
        if not isinstance(versions, Mapping):
            known = ", ".join(sorted(str(k) for k in runtimes)) or "<none>"
            msg = f"registry {self.location} does not publish a runtime named {name!r}"
            raise RegistryError(msg, hint=f"Available runtimes: {known}")
        platforms = versions.get(version)
        if not isinstance(platforms, Mapping):
            known = ", ".join(sorted(str(k) for k in versions)) or "<none>"
            msg = f"registry {self.location} does not publish {name} {version}"
            raise RegistryError(msg, hint=f"Available versions of {name}: {known}")
        entry = platforms.get(platform.tag)
        if not isinstance(entry, Mapping):
            known = ", ".join(sorted(str(k) for k in platforms)) or "<none>"
            msg = f"{name} {version} is not published for platform {platform.tag!r}"
            raise RegistryError(
                msg,
                hint=f"Published platforms for {name} {version}: {known}",
            )
        return manifest_from_document(
            {str(k): v for k, v in entry.items()},
            name=name,
            version=version,
            platform=platform.tag,
            source=f"{self.location}/{name}/{version}",
        )

    def available_versions(self, name: str, platform: Platform | None = None) -> tuple[str, ...]:
        versions = self._runtimes().get(name)
        if not isinstance(versions, Mapping):
            return ()
        if platform is None:
            return tuple(sorted(str(key) for key in versions))
        return tuple(
            sorted(
                str(version)
                for version, platforms in versions.items()
                if isinstance(platforms, Mapping) and platform.tag in platforms
            )
        )

    def available_runtimes(self) -> tuple[str, ...]:
        return tuple(sorted(str(key) for key in self._runtimes()))


class FilesystemRegistry(_IndexBackedRegistry):
    """A registry rooted at a local directory (or a mounted share).

    Artifact URLs are resolved *inside* the root: an index that points at
    ``../../etc/passwd`` is refused, so an untrusted index cannot make Devbox
    read outside the registry it was pointed at.
    """

    __slots__ = ("root",)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root.resolve()

    @property
    def location(self) -> str:
        return self.root.as_posix()

    @property
    def supports_resume(self) -> bool:
        return True

    def _read_index_bytes(self) -> bytes:
        index_path = self.root / INDEX_FILENAME
        try:
            return index_path.read_bytes()
        except FileNotFoundError as exc:
            msg = f"no registry index at {index_path}"
            raise RegistryError(
                msg,
                hint="Point --registry at a directory containing index.json, "
                "or see docs/registry.md to build one.",
            ) from exc
        except OSError as exc:
            msg = f"cannot read the registry index at {index_path}: {exc}"
            raise RegistryError(msg) from exc

    def artifact_path(self, manifest: RuntimeManifest) -> Path:
        from devbox.security.paths import resolve_within

        if manifest.url.startswith(("http://", "https://", "file://")):
            msg = (
                f"filesystem registry {self.location} advertises an absolute URL "
                f"({manifest.url}); use --registry with an http(s) URL instead"
            )
            raise RegistryError(msg)
        return resolve_within(self.root, manifest.url, what="registry artifact")

    @contextmanager
    def open_stream(self, manifest: RuntimeManifest, *, offset: int = 0) -> Iterator[BinaryIO]:
        path = self.artifact_path(manifest)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            msg = f"registry artifact is missing: {path}"
            raise RegistryError(msg) from exc
        except OSError as exc:
            msg = f"cannot open registry artifact {path}: {exc}"
            raise RegistryError(msg) from exc
        try:
            if offset:
                handle.seek(offset)
            yield handle
        finally:
            handle.close()


class HttpRegistry(_IndexBackedRegistry):
    """A registry served over HTTP(S).

    TLS verification uses the system trust store and is **never** disabled: an
    unverifiable registry is a refused registry.  ``http://`` is accepted for
    ``127.0.0.1``/``localhost`` only, which is what the test fixtures use; any
    other plaintext URL is refused so a corporate laptop cannot be talked into
    fetching executables over a downgraded connection.
    """

    __slots__ = ("_opener", "_timeout", "base_url")

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        _reject_insecure_url(self.base_url)
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    @property
    def location(self) -> str:
        return self.base_url

    @property
    def supports_resume(self) -> bool:
        return True

    def _absolute(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            _reject_insecure_url(url)
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    def _request(self, url: str, headers: Mapping[str, str] | None = None) -> BinaryIO:
        request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")  # noqa: S310 - scheme validated by _reject_insecure_url
        try:
            return cast(BinaryIO, self._opener.open(request, timeout=self._timeout))
        except urllib.error.HTTPError as exc:
            msg = f"registry request failed: HTTP {exc.code} for {url}"
            raise RegistryError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"registry is unreachable: {url} ({exc.reason})"
            raise RegistryError(
                msg,
                hint="Check the URL, your proxy settings, and that the registry is running.",
            ) from exc
        except TimeoutError as exc:
            msg = f"registry timed out after {self._timeout:g}s: {url}"
            raise RegistryError(msg) from exc

    def _read_index_bytes(self) -> bytes:
        stream = self._request(f"{self.base_url}/{INDEX_FILENAME}")
        try:
            return stream.read()
        finally:
            stream.close()

    @contextmanager
    def open_stream(self, manifest: RuntimeManifest, *, offset: int = 0) -> Iterator[BinaryIO]:
        url = self._absolute(manifest.url)
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        stream = self._request(url, headers)
        try:
            yield stream
        finally:
            stream.close()


def _reject_insecure_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "") in {"127.0.0.1", "::1", "localhost"}:
        return
    msg = f"refusing to use registry URL {url!r}"
    raise RegistryError(
        msg,
        hint=(
            "Runtime artifacts are executable code: only https:// is accepted "
            "(plain http:// is allowed for 127.0.0.1 in tests)."
        ),
    )


def registry_from_uri(uri: str) -> RuntimeRegistry:
    """Build a registry from a CLI/config value: a URL or a filesystem path."""
    if uri.startswith(("http://", "https://")):
        return HttpRegistry(uri)
    if uri.startswith("file://"):
        return FilesystemRegistry(Path(urllib.parse.urlparse(uri).path))
    return FilesystemRegistry(Path(uri).expanduser())


def stream_chunks(stream: BinaryIO, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    """Iterate a stream in fixed-size chunks."""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        yield chunk
