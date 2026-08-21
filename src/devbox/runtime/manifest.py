"""Registry manifests: what a registry says about one downloadable artifact."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from devbox.errors import RegistryError
from devbox.host.platform_info import Platform
from devbox.lockfile.models import LockedRuntime, is_valid_sha256

__all__ = ["RuntimeManifest", "manifest_from_document"]

_SUPPORTED_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """One artifact, as advertised by a registry.

    ``sha256`` is mandatory: a registry that cannot state the digest of what it
    serves cannot be used, because Devbox refuses to execute an artifact whose
    integrity was never established.
    """

    name: str
    version: str
    platform: str
    url: str
    sha256: str
    size: int = 0
    archive: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if not is_valid_sha256(self.sha256):
            msg = (
                f"registry manifest for {self.name} {self.version} ({self.platform}) has an "
                f"invalid sha256 {self.sha256!r}: expected 64 lowercase hexadecimal characters"
            )
            raise RegistryError(
                msg,
                hint="Devbox never installs an artifact it cannot verify.",
            )

    @property
    def archive_name(self) -> str:
        """File name to store the download under, inferred from the URL if needed."""
        if self.archive:
            return self.archive
        tail = self.url.rsplit("/", 1)[-1].split("?", 1)[0]
        if tail and tail.lower().endswith(_SUPPORTED_ARCHIVE_SUFFIXES):
            return tail
        return f"{self.name}-{self.version}-{self.platform}.zip"

    def matches(self, platform: Platform) -> bool:
        return self.platform == platform.tag

    def to_locked(self) -> LockedRuntime:
        return LockedRuntime(
            name=self.name,
            version=self.version,
            platform=self.platform,
            sha256=self.sha256,
            source=self.source or self.url,
            url=self.url,
            size=self.size,
            archive=self.archive_name,
        )


def manifest_from_document(
    document: Mapping[str, object],
    *,
    name: str,
    version: str,
    platform: str,
    source: str = "",
) -> RuntimeManifest:
    """Build a manifest from a registry index entry, rejecting bad shapes loudly."""

    def text(key: str, *, required: bool = True) -> str:
        value = document.get(key)
        if value is None:
            if required:
                msg = f"registry entry {name} {version} ({platform}) is missing {key!r}"
                raise RegistryError(msg)
            return ""
        if not isinstance(value, str):
            msg = f"registry entry {name} {version} ({platform}): {key!r} must be a string"
            raise RegistryError(msg)
        return value

    raw_size = document.get("size", 0)
    if isinstance(raw_size, bool) or not isinstance(raw_size, int):
        msg = f"registry entry {name} {version} ({platform}): 'size' must be an integer"
        raise RegistryError(msg)

    return RuntimeManifest(
        name=name,
        version=version,
        platform=platform,
        url=text("url"),
        sha256=text("sha256").lower(),
        size=raw_size,
        archive=text("archive", required=False),
        source=source,
    )
