"""Installing, listing and removing runtimes.

The install is transactional in the way that matters: an artifact is only ever
extracted after its SHA256 has been verified, extraction happens in a staging
directory, and the final ``replace`` into
``%LOCALAPPDATA%\\Devbox\\runtimes\\<name>\\<version>`` is a single atomic move.
A failure at any step leaves the previous state untouched — there is no such
thing as a half-installed runtime that a later run mistakes for a good one.

Nothing here writes outside the Devbox home directory: no ``Program Files``, no
registry keys, no system ``PATH``, no elevation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from devbox.catalogs.runtime_kinds import runtime_kind
from devbox.config.models import RuntimeRequirement
from devbox.errors import RuntimeInstallError
from devbox.host.files import remove_tree
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import Platform, current_platform
from devbox.lockfile.models import LockedRuntime, Lockfile
from devbox.runtime.download import Downloader, ProgressCallback
from devbox.runtime.manifest import RuntimeManifest
from devbox.runtime.registry import RuntimeRegistry
from devbox.security.archive import ExtractionLimits, extract_archive

__all__ = ["InstallOutcome", "InstalledRuntime", "RuntimeManager"]

_MARKER_NAME = ".devbox-runtime.json"


@dataclass(frozen=True, slots=True)
class InstalledRuntime:
    """A runtime present on disk."""

    name: str
    version: str
    root: Path
    sha256: str = ""
    complete: bool = True
    """False when the install directory exists but its marker does not: an
    interrupted or externally damaged install, which must be reinstalled rather
    than used."""

    def __str__(self) -> str:
        return f"{self.name} {self.version}"

    @property
    def bin_paths(self) -> tuple[Path, ...]:
        kind = runtime_kind(self.name)
        subdirs = kind.bin_subdirs if kind is not None else (".",)
        return tuple(self.root if s == "." else self.root / s for s in subdirs)


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What ``install`` did, so the CLI can report the truth rather than a guess."""

    runtime: InstalledRuntime
    action: str
    """``installed`` | ``already-present`` | ``repaired``"""

    from_cache: bool = False
    downloaded_bytes: int = 0

    @property
    def changed(self) -> bool:
        return self.action != "already-present"


class RuntimeManager:
    """Owns ``%LOCALAPPDATA%\\Devbox\\runtimes``."""

    def __init__(
        self,
        paths: DevboxPaths,
        *,
        platform: Platform | None = None,
        downloader: Downloader | None = None,
        limits: ExtractionLimits | None = None,
    ) -> None:
        self.paths = paths
        self.platform = platform or current_platform()
        self._downloader = downloader or Downloader(paths)
        self._limits = limits

    # -- inspection ----------------------------------------------------------
    def install_root(self, name: str, version: str) -> Path:
        return self.paths.runtime_dir(name, version)

    def is_installed(self, name: str, version: str) -> bool:
        root = self.install_root(name, version)
        return root.is_dir() and (root / _MARKER_NAME).is_file()

    def installed(self, name: str, version: str) -> InstalledRuntime | None:
        root = self.install_root(name, version)
        if not root.is_dir():
            return None
        marker = root / _MARKER_NAME
        return InstalledRuntime(
            name=name,
            version=version,
            root=root,
            sha256=_read_marker_digest(marker),
            complete=marker.is_file(),
        )

    def list_installed(self) -> tuple[InstalledRuntime, ...]:
        """Every runtime on disk, including incomplete ones (reported as such)."""
        runtimes_dir = self.paths.runtimes
        if not runtimes_dir.is_dir():
            return ()
        found: list[InstalledRuntime] = []
        for name_dir in sorted(p for p in runtimes_dir.iterdir() if p.is_dir()):
            for version_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
                marker = version_dir / _MARKER_NAME
                found.append(
                    InstalledRuntime(
                        name=name_dir.name,
                        version=version_dir.name,
                        root=version_dir,
                        sha256=_read_marker_digest(marker),
                        complete=marker.is_file(),
                    )
                )
        return tuple(found)

    def missing(self, requirements: Sequence[RuntimeRequirement]) -> tuple[RuntimeRequirement, ...]:
        return tuple(r for r in requirements if not self.is_installed(r.name, r.version))

    # -- install -------------------------------------------------------------
    def install_locked(
        self,
        locked: LockedRuntime,
        registry: RuntimeRegistry,
        *,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> InstallOutcome:
        """Install exactly what ``devbox.lock`` pins.

        The lockfile's digest is authoritative: if the registry now serves
        different bytes for the same version, the install is refused rather than
        silently accepted.
        """
        if locked.platform != self.platform.tag:
            msg = (
                f"{locked.name} {locked.version} is locked for platform {locked.platform!r}, "
                f"but this machine is {self.platform.tag!r}"
            )
            raise RuntimeInstallError(
                msg,
                hint="Run 'devbox install' on the target platform to re-resolve devbox.lock.",
            )

        existing = self.installed(locked.name, locked.version)
        if existing is not None and existing.complete and not force:
            if existing.sha256 and existing.sha256 != locked.sha256:
                # Installed from a different artifact than the lockfile now pins.
                return self._perform_install(locked, registry, on_progress, action="repaired")
            return InstallOutcome(runtime=existing, action="already-present")
        action = "repaired" if existing is not None else "installed"
        return self._perform_install(locked, registry, on_progress, action=action)

    def _perform_install(
        self,
        locked: LockedRuntime,
        registry: RuntimeRegistry,
        on_progress: ProgressCallback | None,
        *,
        action: str,
    ) -> InstallOutcome:
        manifest = self._manifest_for(locked, registry)
        download = self._downloader.fetch(
            manifest,
            registry,
            expected_sha256=locked.sha256,
            on_progress=on_progress,
        )

        destination = self.install_root(locked.name, locked.version)
        extract_kwargs = {} if self._limits is None else {"limits": self._limits}
        extract_archive(
            download.path,
            destination,
            staging_dir=self.paths.tmp,
            **extract_kwargs,  # type: ignore[arg-type]
        )
        _write_marker(destination / _MARKER_NAME, locked)

        return InstallOutcome(
            runtime=InstalledRuntime(
                name=locked.name,
                version=locked.version,
                root=destination,
                sha256=locked.sha256,
            ),
            action=action,
            from_cache=download.from_cache,
            downloaded_bytes=0 if download.from_cache else download.size,
        )

    def _manifest_for(self, locked: LockedRuntime, registry: RuntimeRegistry) -> RuntimeManifest:
        """Resolve the artifact, preferring the registry's current answer.

        The lockfile records a URL, but registries move; asking the registry
        again and then checking the digest against the lockfile is both more
        robust and no less safe, because the digest is what is trusted.
        """
        return registry.resolve(locked.name, locked.version, self.platform)

    def ensure_installed(
        self,
        requirements: Sequence[RuntimeRequirement],
        lockfile: Lockfile,
        registry: RuntimeRegistry,
        *,
        on_progress: ProgressCallback | None = None,
        on_event: Callable[[InstallOutcome], None] | None = None,
    ) -> tuple[InstallOutcome, ...]:
        """Install everything *requirements* needs, in a deterministic order."""
        outcomes: list[InstallOutcome] = []
        for requirement in sorted(set(requirements)):
            locked = lockfile.require(
                requirement.name, requirement.version, platform=self.platform.tag
            )
            outcome = self.install_locked(locked, registry, on_progress=on_progress)
            outcomes.append(outcome)
            if on_event is not None:
                on_event(outcome)
        return tuple(outcomes)

    # -- removal -------------------------------------------------------------
    def remove(self, name: str, version: str) -> bool:
        """Delete an installed runtime.  Returns False when it was not there."""
        root = self.install_root(name, version)
        if not root.is_dir():
            return False
        # remove_tree clears the read-only attribute Node and PostgreSQL
        # archives set on some files, which plain rmtree cannot delete on
        # Windows.
        remove_tree(root)
        parent = root.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return True

    def prune_incomplete(self) -> tuple[str, ...]:
        """Remove install directories with no marker.  Returns what was removed."""
        removed: list[str] = []
        for runtime in self.list_installed():
            if runtime.complete:
                continue
            remove_tree(runtime.root, best_effort=True)
            removed.append(f"{runtime.name} {runtime.version}")
        return tuple(removed)

    def verify(self, name: str, version: str) -> tuple[bool, str]:
        """Check an install is usable: present, marked, and with its probe binary."""
        runtime = self.installed(name, version)
        if runtime is None:
            return False, "not installed"
        if not runtime.complete:
            return False, "installation is incomplete (interrupted or damaged)"
        kind = runtime_kind(name)
        if kind is None or not kind.probe_executable:
            return True, ""
        from devbox.security.command import resolve_executable

        found = resolve_executable(
            kind.probe_executable,
            path_entries=[str(p) for p in runtime.bin_paths],
            is_windows=self.platform.is_windows,
        )
        if found is None:
            return False, f"{kind.probe_executable} was not found under {runtime.root}"
        return True, ""


def _write_marker(path: Path, locked: LockedRuntime) -> None:
    """Write the completion marker.

    Its presence is what distinguishes a finished install from an interrupted
    one, so it is written **last**, after extraction has fully succeeded.
    """
    import json

    path.write_text(
        json.dumps(
            {
                "name": locked.name,
                "version": locked.version,
                "platform": locked.platform,
                "sha256": locked.sha256,
                "source": locked.source,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_marker_digest(marker: Path) -> str:
    import json

    if not marker.is_file():
        return ""
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    digest = document.get("sha256") if isinstance(document, dict) else None
    return str(digest) if isinstance(digest, str) else ""
