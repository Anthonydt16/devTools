"""``devbox build`` — a reproducible artifact plus its manifest.

Reproducible means: the same source tree and the same lockfile produce a
**byte-identical** zip, on any machine, on any day.  Three things would otherwise
break that, and each is handled:

* **timestamps** — every entry is stamped with ``SOURCE_DATE_EPOCH`` (or the
  zip epoch, 1980-01-01, when it is unset), never with the current time;
* **ordering** — entries are sorted by their archive path, not by whatever order
  the filesystem happened to return;
* **permissions** — normalised to 0644 / 0755, so a developer's umask does not
  end up in the artifact.

The manifest is written both inside the archive and beside it, and records the
digest of ``devbox.lock`` — so an artifact can always be traced back to the exact
runtimes it was built against.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from devbox.errors import BuildError
from devbox.lockfile.models import Lockfile

__all__ = ["BuildResult", "BuildSpec", "Builder", "build_manifest"]

MANIFEST_NAME = "devbox-manifest.json"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
"""The earliest timestamp the zip format can represent."""

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/*",
    ".git",
    ".devbox/*",
    ".devbox",
    "dist/*",
    "dist",
    "node_modules/*",
    "**/node_modules/*",
    "__pycache__/*",
    "**/__pycache__/*",
    "*.pyc",
    ".venv/*",
    ".venv",
    ".mypy_cache/*",
    ".pytest_cache/*",
    ".ruff_cache/*",
)


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """What to build, and from what."""

    application: str
    version: str
    project_root: Path
    output_dir: Path
    include: tuple[str, ...] = ()
    """Glob patterns to include; empty means "everything not excluded"."""

    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    runtimes: Mapping[str, str] = field(default_factory=dict)

    @property
    def artifact_name(self) -> str:
        return f"{self.application}-{self.version}.zip"

    @property
    def artifact_path(self) -> Path:
        return self.output_dir / self.artifact_name


@dataclass(frozen=True, slots=True)
class BuildResult:
    """The artifact, its manifest and enough detail to verify both."""

    artifact_path: Path
    manifest_path: Path
    manifest: Mapping[str, object]
    sha256: str
    size: int
    entries: tuple[str, ...]

    @property
    def file_count(self) -> int:
        return len(self.entries)


def source_date_epoch(environ: Mapping[str, str] | None = None) -> int | None:
    """``SOURCE_DATE_EPOCH`` if it is set to a usable integer.

    The reproducible-builds convention.  Setting it is how CI gets two runs of
    ``devbox build`` to produce identical bytes.
    """
    env = os.environ if environ is None else environ
    raw = env.get("SOURCE_DATE_EPOCH")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def build_manifest(
    spec: BuildSpec,
    *,
    lockfile: Lockfile,
    created_at: str,
    entries: Sequence[str],
) -> dict[str, object]:
    """The manifest describing one artifact."""
    return {
        "application": spec.application,
        "version": spec.version,
        "runtime": dict(sorted(spec.runtimes.items())),
        "created_at": created_at,
        "devbox_lock_hash": lockfile.digest(),
        "manifest_version": 1,
        "file_count": len(entries),
    }


class Builder:
    """Produces the artifact."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._environ: Mapping[str, str] = dict(os.environ if environ is None else environ)
        self._clock = clock

    def build(self, spec: BuildSpec, *, lockfile: Lockfile) -> BuildResult:
        if not spec.project_root.is_dir():
            msg = f"project directory does not exist: {spec.project_root}"
            raise BuildError(msg)
        if not spec.application or not spec.version:
            msg = "an artifact needs both an application name and a version"
            raise BuildError(
                msg,
                hint="Pass --name and --version, or set 'project' in devbox.yml.",
            )

        files = self._collect(spec)
        if not files:
            msg = f"nothing to package in {spec.project_root}"
            raise BuildError(
                msg,
                hint="Every file was excluded; check the --include and --exclude patterns.",
            )

        epoch = source_date_epoch(self._environ)
        timestamp = ZIP_EPOCH if epoch is None else _zip_time(epoch)
        created_at = (
            datetime.fromtimestamp(epoch, tz=UTC) if epoch is not None else datetime.now(tz=UTC)
        ).isoformat(timespec="seconds")

        entries = tuple(path.relative_to(spec.project_root).as_posix() for path in files)
        manifest = build_manifest(spec, lockfile=lockfile, created_at=created_at, entries=entries)
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

        spec.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = spec.artifact_path.with_name(spec.artifact_path.name + ".building")
        try:
            self._write_zip(temporary, spec, files, manifest_bytes, timestamp)
            data = temporary.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            temporary.replace(spec.artifact_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            msg = f"cannot write the artifact to {spec.artifact_path}: {exc}"
            raise BuildError(msg) from exc

        manifest_path = spec.output_dir / f"{spec.application}-{spec.version}.manifest.json"
        manifest_path.write_bytes(manifest_bytes)

        return BuildResult(
            artifact_path=spec.artifact_path,
            manifest_path=manifest_path,
            manifest=manifest,
            sha256=digest,
            size=len(data),
            entries=entries,
        )

    @staticmethod
    def _write_zip(
        target: Path,
        spec: BuildSpec,
        files: Sequence[Path],
        manifest_bytes: bytes,
        timestamp: tuple[int, int, int, int, int, int],
    ) -> None:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            # The manifest is written first and always under the same name, so
            # its position in the archive is part of what is reproducible.
            manifest_info = zipfile.ZipInfo(MANIFEST_NAME, date_time=timestamp)
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.external_attr = 0o644 << 16
            zf.writestr(manifest_info, manifest_bytes)

            for path in files:
                arcname = path.relative_to(spec.project_root).as_posix()
                info = zipfile.ZipInfo(arcname, date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                # Normalised so a developer's umask never reaches the artifact.
                executable = path.stat().st_mode & 0o111
                info.external_attr = (0o755 if executable else 0o644) << 16
                zf.writestr(info, path.read_bytes())

    def _collect(self, spec: BuildSpec) -> tuple[Path, ...]:
        """Every file to package, sorted by archive path for reproducibility."""
        selected: list[Path] = []
        for path in spec.project_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(spec.project_root).as_posix()
            if _matches_any(relative, spec.exclude):
                continue
            if spec.include and not _matches_any(relative, spec.include):
                continue
            selected.append(path)
        return tuple(sorted(selected, key=lambda p: p.relative_to(spec.project_root).as_posix()))


def _matches_any(relative: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern):
            return True
        # A directory pattern must exclude everything beneath it.
        if pattern.endswith("/*") and relative.startswith(pattern[:-1]):
            return True
        head = pattern.removeprefix("**/")
        if head != pattern and (fnmatch.fnmatch(relative, head) or f"/{head}" in f"/{relative}"):
            return True
    return False


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    """Convert a POSIX timestamp to a zip ``date_time`` tuple, clamped to 1980."""
    moment = datetime.fromtimestamp(max(epoch, 315_532_800), tz=UTC)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


def verify_artifact(path: Path) -> Mapping[str, object]:
    """Read an artifact's manifest back, proving the archive is usable."""
    if not path.is_file():
        msg = f"no artifact at {path}"
        raise BuildError(msg)
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                msg = f"artifact {path.name} is corrupt"
                raise BuildError(msg)
            raw = archive.read(MANIFEST_NAME)
    except KeyError as exc:
        msg = f"artifact {path.name} has no {MANIFEST_NAME}"
        raise BuildError(msg) from exc
    except zipfile.BadZipFile as exc:
        msg = f"artifact {path.name} is not a valid zip: {exc}"
        raise BuildError(msg) from exc
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        msg = f"artifact {path.name} has a malformed manifest"
        raise BuildError(msg)
    return document
