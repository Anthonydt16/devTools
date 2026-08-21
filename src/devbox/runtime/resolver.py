"""Resolving ``devbox.yml`` into ``devbox.lock``.

Resolution is **deterministic and sticky**: an entry that already exists in the
lockfile for the requested name, version and platform is kept untouched, so
running ``devbox install`` twice produces the same file and re-resolving does not
silently swap an artifact under a project.

Only what the configuration actually requires is resolved; a stale entry for a
runtime nobody uses any more is pruned, and the pruning is reported so the user
sees what changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from devbox.config.models import ProjectConfig, RuntimeRequirement
from devbox.errors import LockfileError
from devbox.host.platform_info import Platform
from devbox.lockfile.models import LockedRuntime, Lockfile
from devbox.runtime.registry import RuntimeRegistry

__all__ = ["ResolutionResult", "resolve_lockfile", "verify_lockfile_covers"]


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """What a resolution changed, so the CLI can report it honestly."""

    lockfile: Lockfile
    added: tuple[LockedRuntime, ...] = ()
    kept: tuple[LockedRuntime, ...] = ()
    removed: tuple[LockedRuntime, ...] = ()
    replaced: tuple[LockedRuntime, ...] = field(default=())
    """Entries whose platform no longer matched and had to be re-resolved."""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.replaced)

    def summary(self) -> str:
        if not self.changed:
            return f"devbox.lock is up to date ({len(self.kept)} runtime(s))"
        parts: list[str] = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.replaced:
            parts.append(f"{len(self.replaced)} re-resolved")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.kept:
            parts.append(f"{len(self.kept)} unchanged")
        return "devbox.lock: " + ", ".join(parts)


def resolve_lockfile(
    config: ProjectConfig,
    *,
    registry: RuntimeRegistry,
    platform: Platform,
    existing: Lockfile | None = None,
    force: bool = False,
) -> ResolutionResult:
    """Produce a lockfile covering every runtime *config* requires.

    *force* re-queries the registry for every runtime, which is how a user picks
    up a republished artifact on purpose.  Without it, existing entries win.
    """
    current = existing or Lockfile()
    current.validate()

    required = config.required_runtimes()
    added: list[LockedRuntime] = []
    kept: list[LockedRuntime] = []
    replaced: list[LockedRuntime] = []

    resolved = Lockfile()
    for requirement in required:
        previous = current.entry(requirement.name, requirement.version)
        if previous is not None and previous.platform == platform.tag and not force:
            kept.append(previous)
            resolved = resolved.with_entry(previous)
            continue
        locked = _resolve_one(requirement, registry=registry, platform=platform)
        resolved = resolved.with_entry(locked)
        if previous is None:
            added.append(locked)
        else:
            replaced.append(locked)

    required_keys = {(r.name, r.version) for r in required}
    removed = tuple(
        entry for entry in current.entries() if (entry.name, entry.version) not in required_keys
    )

    return ResolutionResult(
        lockfile=resolved,
        added=tuple(added),
        kept=tuple(kept),
        removed=removed,
        replaced=tuple(replaced),
    )


def _resolve_one(
    requirement: RuntimeRequirement,
    *,
    registry: RuntimeRegistry,
    platform: Platform,
) -> LockedRuntime:
    manifest = registry.resolve(requirement.name, requirement.version, platform)
    if manifest.platform != platform.tag:
        msg = (
            f"registry returned {requirement.name} {requirement.version} for platform "
            f"{manifest.platform!r}, but {platform.tag!r} was requested"
        )
        raise LockfileError(msg)
    return manifest.to_locked()


def verify_lockfile_covers(
    lock: Lockfile,
    requirements: Sequence[RuntimeRequirement],
    *,
    platform: Platform,
) -> None:
    """Raise unless *lock* pins every requirement for *platform*.

    Called before ``devbox up`` touches anything, so a project whose lockfile is
    out of date fails immediately with an actionable message instead of half
    starting.
    """
    missing: list[str] = []
    wrong_platform: list[str] = []
    for requirement in requirements:
        entry = lock.entry(requirement.name, requirement.version)
        if entry is None:
            missing.append(str(requirement))
        elif entry.platform != platform.tag:
            wrong_platform.append(f"{requirement} (locked for {entry.platform})")

    if not missing and not wrong_platform:
        return

    lines: list[str] = ["devbox.lock does not cover this project"]
    if missing:
        lines.append("  missing: " + ", ".join(sorted(missing)))
    if wrong_platform:
        lines.append(f"  resolved for another platform (this machine is {platform.tag}): ")
        lines.append("    " + ", ".join(sorted(wrong_platform)))
    raise LockfileError(
        "\n".join(lines),
        hint="Run 'devbox install' to resolve and write devbox.lock, then commit it.",
    )
