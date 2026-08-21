"""The download pipeline.

Every runtime artifact goes through exactly this sequence, with no shortcut:

1. **check the cache** — a verified artifact is keyed by its SHA256, so a hit is
   proof of integrity, not a guess based on a file name;
2. **stream to a temporary file** in ``downloads/``, hashing as the bytes arrive;
3. **resume** an interrupted download when the registry supports ranges, and
   re-verify the whole file afterwards — a resumed download is only trustworthy
   if the *complete* digest matches;
4. **compare the digest** with the one the lockfile pins;
5. **refuse and delete** on a mismatch — nothing is extracted, nothing is
   executed, and the error names both digests;
6. **move atomically** into the cache.

The rule this module exists to enforce: *Devbox never executes an artifact whose
integrity has not been established.*
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devbox.errors import IntegrityError, RegistryError
from devbox.host.paths import DevboxPaths
from devbox.runtime.manifest import RuntimeManifest
from devbox.runtime.registry import RuntimeRegistry
from devbox.security.hashing import CHUNK_SIZE, StreamHasher, sha256_file, verify_sha256

__all__ = ["DownloadResult", "Downloader", "ProgressCallback"]

ProgressCallback = Callable[[int, int], None]
"""``(bytes_so_far, total_bytes)``; ``total_bytes`` is 0 when unknown."""

_PARTIAL_SUFFIX = ".part"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Where the verified artifact is, and how it got there."""

    path: Path
    sha256: str
    size: int
    from_cache: bool
    resumed: bool = False

    @property
    def source_description(self) -> str:
        if self.from_cache:
            return "cached"
        return "downloaded (resumed)" if self.resumed else "downloaded"


class Downloader:
    """Fetches and verifies runtime artifacts."""

    def __init__(
        self,
        paths: DevboxPaths,
        *,
        chunk_size: int = CHUNK_SIZE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._paths = paths
        self._chunk_size = chunk_size
        self._clock = clock

    # -- cache ---------------------------------------------------------------
    def cache_path(self, manifest: RuntimeManifest) -> Path:
        """Cache location, keyed by digest.

        Content-addressed on purpose: two registries publishing the same bytes
        share one cache entry, and a republished artifact with the same file name
        but different bytes can never be mistaken for the cached one.
        """
        name = f"{manifest.sha256}-{manifest.archive_name}"
        return self._paths.cache / manifest.sha256[:2] / name

    def is_cached(self, manifest: RuntimeManifest) -> bool:
        return self.cache_path(manifest).is_file()

    def partial_path(self, manifest: RuntimeManifest) -> Path:
        return self._paths.downloads / f"{manifest.sha256}{_PARTIAL_SUFFIX}"

    # -- fetch ---------------------------------------------------------------
    def fetch(
        self,
        manifest: RuntimeManifest,
        registry: RuntimeRegistry,
        *,
        expected_sha256: str | None = None,
        on_progress: ProgressCallback | None = None,
        allow_resume: bool = True,
    ) -> DownloadResult:
        """Return a verified artifact, downloading it only if necessary.

        *expected_sha256* comes from ``devbox.lock``.  When it differs from what
        the registry advertises, the **lockfile wins** and the download is
        refused: a registry that changed the digest of a pinned version is
        exactly the situation a lockfile exists to catch.
        """
        expected = (expected_sha256 or manifest.sha256).lower()
        if expected_sha256 and expected_sha256.lower() != manifest.sha256.lower():
            msg = (
                f"{manifest.name} {manifest.version}: the registry now advertises a different "
                f"artifact than devbox.lock pins\n"
                f"  devbox.lock: {expected_sha256}\n"
                f"  registry:    {manifest.sha256}"
            )
            raise IntegrityError(msg, expected=expected_sha256, actual=manifest.sha256)

        cached = self.cache_path(manifest)
        if cached.is_file():
            actual = sha256_file(cached, chunk_size=self._chunk_size)
            if actual == expected:
                return DownloadResult(
                    path=cached,
                    sha256=actual,
                    size=cached.stat().st_size,
                    from_cache=True,
                )
            # A damaged cache entry is not a reason to fail: drop it and refetch.
            cached.unlink(missing_ok=True)

        self._paths.downloads.mkdir(parents=True, exist_ok=True)
        partial = self.partial_path(manifest)
        offset = 0
        resumed = False
        if allow_resume and partial.is_file() and registry.supports_resume:
            offset = partial.stat().st_size
            resumed = offset > 0
        elif partial.is_file():
            partial.unlink(missing_ok=True)

        try:
            self._stream(manifest, registry, partial, offset=offset, on_progress=on_progress)
        except RegistryError:
            # Keep the partial file: the next run resumes from where this stopped.
            raise

        actual = sha256_file(partial, chunk_size=self._chunk_size)
        try:
            verify_sha256(actual, expected, subject=f"{manifest.name} {manifest.version}")
        except IntegrityError:
            # A refused artifact is removed immediately.  It is never extracted,
            # never executed, and never left where a later run could pick it up.
            partial.unlink(missing_ok=True)
            raise

        cached.parent.mkdir(parents=True, exist_ok=True)
        size = partial.stat().st_size
        partial.replace(cached)
        return DownloadResult(
            path=cached, sha256=actual, size=size, from_cache=False, resumed=resumed
        )

    def _stream(
        self,
        manifest: RuntimeManifest,
        registry: RuntimeRegistry,
        target: Path,
        *,
        offset: int,
        on_progress: ProgressCallback | None,
    ) -> None:
        hasher = StreamHasher()
        mode = "ab" if offset else "wb"
        total = manifest.size
        written = offset
        with registry.open_stream(manifest, offset=offset) as stream, target.open(mode) as handle:
            while chunk := stream.read(self._chunk_size):
                hasher.update(chunk)
                handle.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(written, total)

    # -- maintenance ---------------------------------------------------------
    def clean_partials(self, *, older_than_seconds: float | None = None) -> int:
        """Delete interrupted downloads.  Returns how many were removed."""
        directory = self._paths.downloads
        if not directory.is_dir():
            return 0
        cutoff = None if older_than_seconds is None else self._clock() - older_than_seconds
        removed = 0
        for partial in directory.glob(f"*{_PARTIAL_SUFFIX}"):
            if cutoff is not None and partial.stat().st_mtime > cutoff:
                continue
            partial.unlink(missing_ok=True)
            removed += 1
        return removed

    def cache_entries(self) -> tuple[Path, ...]:
        if not self._paths.cache.is_dir():
            return ()
        return tuple(sorted(p for p in self._paths.cache.rglob("*") if p.is_file()))

    def cache_size(self) -> int:
        return sum(entry.stat().st_size for entry in self.cache_entries())
