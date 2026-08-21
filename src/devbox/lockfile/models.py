"""``devbox.lock``: the exact, reproducible resolution of ``devbox.yml``.

``devbox.yml`` states *intent* ("node 18.19.0").  ``devbox.lock`` states the
*resolution*: which artifact, from which source, with which SHA256, for which
platform.  It is committed to Git, so every machine and every CI run installs
byte-identical runtimes.

Nothing is ever installed without a matching lockfile entry, and no artifact is
ever executed before its SHA256 has been verified against the one recorded here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from devbox.errors import LockfileError

__all__ = ["LOCK_VERSION", "LockedRuntime", "Lockfile", "is_valid_sha256"]

LOCK_VERSION: Final = 1

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


def is_valid_sha256(value: str) -> bool:
    """True for a lowercase 64-character hex digest."""
    return bool(_SHA256_RE.match(value))


@dataclass(frozen=True, slots=True, order=True)
class LockedRuntime:
    """One pinned runtime artifact."""

    name: str
    version: str
    platform: str
    sha256: str
    source: str
    """Where the artifact came from, e.g. ``registry/company/node/18.19.0``.
    Informational and auditable; the URL is resolved through the registry."""

    url: str = ""
    """Resolved download URL, when the registry provided one."""

    size: int = 0
    archive: str = ""
    """File name of the artifact, used to pick the extractor."""

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def validate(self) -> list[str]:
        """Return a list of problems; empty means the entry is usable."""
        problems: list[str] = []
        if not self.name:
            problems.append("missing runtime name")
        if not self.version:
            problems.append("missing version")
        if not self.platform:
            problems.append("missing platform")
        if not self.sha256:
            problems.append("missing sha256")
        elif not is_valid_sha256(self.sha256):
            problems.append(
                f"invalid sha256 {self.sha256!r}: expected 64 lowercase hexadecimal characters"
            )
        if self.size < 0:
            problems.append(f"invalid size {self.size}")
        return problems

    def to_document(self) -> dict[str, object]:
        """Serialisable form, without the redundant name/version keys."""
        document: dict[str, object] = {
            "platform": self.platform,
            "sha256": self.sha256,
            "source": self.source,
        }
        if self.url:
            document["url"] = self.url
        if self.size:
            document["size"] = self.size
        if self.archive:
            document["archive"] = self.archive
        return document


@dataclass(frozen=True, slots=True)
class Lockfile:
    """The whole ``devbox.lock`` document.

    Entries are keyed ``runtimes[name][version]``.  One version resolves to one
    artifact *per lockfile*; a project needing the same runtime on two platforms
    keeps one lockfile per platform, which keeps ``devbox up`` unambiguous.
    """

    lock_version: int = LOCK_VERSION
    runtimes: Mapping[str, Mapping[str, LockedRuntime]] = field(default_factory=dict)

    # -- lookups --------------------------------------------------------------
    def entry(self, name: str, version: str) -> LockedRuntime | None:
        return self.runtimes.get(name, {}).get(version)

    def require(self, name: str, version: str, *, platform: str | None = None) -> LockedRuntime:
        """Fetch an entry or explain precisely what is missing."""
        found = self.entry(name, version)
        if found is None:
            msg = f"devbox.lock has no entry for {name} {version}"
            raise LockfileError(
                msg,
                hint="Run 'devbox install' to resolve and write the lockfile.",
            )
        if platform is not None and found.platform != platform:
            msg = (
                f"devbox.lock pins {name} {version} for platform {found.platform!r}, "
                f"but this machine is {platform!r}"
            )
            raise LockfileError(
                msg,
                hint="Re-resolve on the target platform with 'devbox install'.",
            )
        return found

    def entries(self) -> Iterator[LockedRuntime]:
        for name in sorted(self.runtimes):
            versions = self.runtimes[name]
            for version in sorted(versions):
                yield versions[version]

    def __len__(self) -> int:
        return sum(len(versions) for versions in self.runtimes.values())

    def __contains__(self, item: tuple[str, str]) -> bool:
        name, version = item
        return self.entry(name, version) is not None

    # -- mutation (returns a new lockfile; the type stays immutable) ----------
    def with_entry(self, locked: LockedRuntime) -> Lockfile:
        runtimes: dict[str, dict[str, LockedRuntime]] = {
            name: dict(versions) for name, versions in self.runtimes.items()
        }
        runtimes.setdefault(locked.name, {})[locked.version] = locked
        return replace(self, runtimes=runtimes)

    def without_entry(self, name: str, version: str) -> Lockfile:
        runtimes: dict[str, dict[str, LockedRuntime]] = {
            key: dict(versions) for key, versions in self.runtimes.items()
        }
        versions = runtimes.get(name)
        if versions is not None:
            versions.pop(version, None)
            if not versions:
                runtimes.pop(name)
        return replace(self, runtimes=runtimes)

    # -- integrity ------------------------------------------------------------
    def validate(self) -> None:
        """Raise if the document is structurally unusable."""
        if self.lock_version != LOCK_VERSION:
            msg = (
                f"unsupported lock_version {self.lock_version}; this build of Devbox "
                f"writes and reads version {LOCK_VERSION}"
            )
            raise LockfileError(msg, hint="Delete devbox.lock and run 'devbox install'.")
        problems: list[str] = []
        for locked in self.entries():
            problems.extend(
                f"runtimes.{locked.name}.{locked.version}: {p}" for p in locked.validate()
            )
        if problems:
            raise LockfileError(
                "devbox.lock is inconsistent:\n  - " + "\n  - ".join(problems),
                hint="Delete devbox.lock and run 'devbox install' to regenerate it.",
            )

    def digest(self) -> str:
        """Stable SHA256 of the lockfile *content*, independent of formatting.

        Used by ``devbox build`` to stamp an artifact with the exact runtime set
        it was produced against.
        """
        hasher = hashlib.sha256()
        hasher.update(f"lock_version={self.lock_version}\n".encode())
        for locked in self.entries():
            hasher.update(
                f"{locked.name}\0{locked.version}\0{locked.platform}\0"
                f"{locked.sha256}\0{locked.source}\0{locked.archive}\n".encode()
            )
        return hasher.hexdigest()

    def to_document(self) -> dict[str, object]:
        runtimes: dict[str, dict[str, object]] = {}
        for locked in self.entries():
            runtimes.setdefault(locked.name, {})[locked.version] = locked.to_document()
        return {"lock_version": self.lock_version, "runtimes": runtimes}
