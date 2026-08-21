"""Extracting archives safely.

A runtime archive is executable code downloaded from somewhere else.  Extraction
is therefore treated as hostile input, and every entry is validated *before* a
single byte is written:

* **Zip Slip / path traversal** — ``../../evil.exe``, ``/etc/passwd``,
  ``C:\\Windows\\System32\\...``, ``\\\\server\\share\\...`` are all refused
  (:mod:`devbox.security.paths`), and the resolved destination is proven to be
  inside the target directory afterwards as well.
* **Symlinks, hardlinks and junctions** — refused outright.  On Windows a
  symlink in an archive is the classic way to make an extractor write outside
  its directory, and no legitimate runtime archive needs one.
* **Device files, FIFOs, sockets** — refused (tar only).
* **Archive bombs** — a total size limit, a per-entry size limit and an entry
  count limit, all checked against the *declared* sizes before extracting and
  against the *actual* bytes while extracting, because a header can lie.
* **Reserved Windows names** — ``CON``, ``NUL``, ``COM1`` and friends are
  refused; opening them has side effects.

Extraction is staged: everything lands in a temporary directory next to the
target and is moved into place only once the whole archive has been processed.
A failure therefore leaves no partial install behind.
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from devbox.errors import SecurityError
from devbox.security.paths import normalise_archive_member, resolve_within

__all__ = [
    "DEFAULT_LIMITS",
    "ExtractionLimits",
    "ExtractionResult",
    "extract_archive",
    "is_supported_archive",
]

_ZIP_SUFFIXES: Final = (".zip",)
_TAR_SUFFIXES: Final = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """Bounds that make an archive bomb a clean error instead of a full disk."""

    max_total_bytes: int = 4 * 1024**3
    """4 GiB uncompressed: comfortably above a JDK, far below a disk."""

    max_entry_bytes: int = 2 * 1024**3
    max_entries: int = 200_000

    def describe(self) -> str:
        return (
            f"max {self.max_entries} entries, {self.max_entry_bytes // 1024**2} MiB per entry, "
            f"{self.max_total_bytes // 1024**3} GiB total"
        )


DEFAULT_LIMITS = ExtractionLimits()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What was extracted, for reporting and for tests to assert on."""

    destination: Path
    entries: int
    total_bytes: int
    stripped_prefix: str = ""


def is_supported_archive(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_ZIP_SUFFIXES) or lowered.endswith(_TAR_SUFFIXES)


def extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    limits: ExtractionLimits = DEFAULT_LIMITS,
    strip_single_root: bool = True,
    staging_dir: Path | None = None,
) -> ExtractionResult:
    """Extract *archive_path* into *destination*, refusing anything unsafe.

    *strip_single_root* removes the redundant top-level folder that most vendors
    ship (``node-v18.19.0-win-x64/...``), so an install root always looks the
    same whatever the vendor decided.

    The destination is created atomically: work happens in a sibling temporary
    directory and is moved into place at the end, so an interrupted or refused
    extraction never leaves a half-installed runtime that a later run would
    mistake for a good one.
    """
    if not archive_path.is_file():
        msg = f"archive not found: {archive_path}"
        raise SecurityError(msg)
    if not is_supported_archive(archive_path.name):
        msg = (
            f"unsupported archive format: {archive_path.name} "
            f"(supported: {', '.join((*_ZIP_SUFFIXES, *_TAR_SUFFIXES))})"
        )
        raise SecurityError(msg)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = staging_dir or destination.parent
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        # A sibling of the destination guarantees the final move is on the same
        # volume, so it is atomic rather than a copy that can half-fail.
        staging_root / f".{destination.name}.extracting-{os.getpid()}"
    )
    _remove(staging)
    staging.mkdir(parents=True)

    try:
        if archive_path.name.lower().endswith(_ZIP_SUFFIXES):
            entries, total = _extract_zip(archive_path, staging, limits)
        else:
            entries, total = _extract_tar(archive_path, staging, limits)

        source = staging
        stripped = ""
        if strip_single_root:
            source, stripped = _strip_single_root(staging)

        _remove(destination)
        source.replace(destination)
        return ExtractionResult(
            destination=destination,
            entries=entries,
            total_bytes=total,
            stripped_prefix=stripped,
        )
    finally:
        _remove(staging)


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------
def _extract_zip(archive_path: Path, staging: Path, limits: ExtractionLimits) -> tuple[int, int]:
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        msg = f"corrupt archive {archive_path.name}: {exc}"
        raise SecurityError(
            msg,
            hint="Delete it from the download cache and run 'devbox install' again.",
        ) from exc

    with archive:
        infos = archive.infolist()
        _check_entry_count(len(infos), limits)
        declared = sum(info.file_size for info in infos)
        _check_total(declared, limits, declared=True)

        written = 0
        count = 0
        for info in infos:
            if _zip_entry_is_symlink(info):
                msg = f"refusing archive entry {info.filename!r}: it is a symbolic link"
                raise SecurityError(
                    msg,
                    hint="Runtime archives must not contain links; nothing was installed.",
                )
            name = normalise_archive_member(info.filename)
            target = resolve_within(staging, name, what="archive entry")
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.file_size > limits.max_entry_bytes:
                msg = (
                    f"refusing archive entry {info.filename!r}: it declares "
                    f"{info.file_size} bytes, over the {limits.max_entry_bytes} byte limit"
                )
                raise SecurityError(msg)

            target.parent.mkdir(parents=True, exist_ok=True)
            written += _copy_stream(archive.open(info), target, limits, written, info.filename)
            count += 1
            _apply_zip_mode(info, target)
        return count, written


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    """Unix mode is stored in the high 16 bits of ``external_attr``."""
    mode = info.external_attr >> 16
    return bool(mode) and stat.S_ISLNK(mode)


def _apply_zip_mode(info: zipfile.ZipInfo, target: Path) -> None:
    """Preserve the executable bit on POSIX; Windows ignores it.

    Without this, a runtime extracted on Linux (the test suite, and any CI job
    that builds an artifact) would have a ``node`` that cannot be executed.
    """
    mode = info.external_attr >> 16
    if not mode or os.name == "nt":
        return
    executable = stat.S_IMODE(mode) & 0o111
    if executable:
        current = target.stat().st_mode
        target.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# tar
# ---------------------------------------------------------------------------
def _extract_tar(archive_path: Path, staging: Path, limits: ExtractionLimits) -> tuple[int, int]:
    try:
        return _extract_tar_members(archive_path, staging, limits)
    except (tarfile.TarError, EOFError, OSError) as exc:
        # A truncated .tar.gz surfaces as EOFError from the gzip layer rather
        # than as a TarError; either way the user needs "this download is
        # damaged", not a stack trace from the compression module.
        msg = f"corrupt archive {archive_path.name}: {exc}"
        raise SecurityError(
            msg,
            hint="Delete it from the download cache and run 'devbox install' again.",
        ) from exc


def _extract_tar_members(
    archive_path: Path, staging: Path, limits: ExtractionLimits
) -> tuple[int, int]:
    with tarfile.open(archive_path) as archive:
        written = 0
        count = 0
        for member in archive:
            count += 1
            _check_entry_count(count, limits)
            _reject_special_tar_member(member)
            name = normalise_archive_member(member.name)
            target = resolve_within(staging, name, what="archive entry")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.size > limits.max_entry_bytes:
                msg = (
                    f"refusing archive entry {member.name!r}: it declares {member.size} bytes, "
                    f"over the {limits.max_entry_bytes} byte limit"
                )
                raise SecurityError(msg)
            member_stream = archive.extractfile(member)
            if member_stream is None:  # pragma: no cover - defensive
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # _copy_stream closes the stream; a `with` here would double-close.
            written += _copy_stream(member_stream, target, limits, written, member.name)
            if os.name != "nt" and member.mode & 0o111:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return count, written


def _reject_special_tar_member(member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        kind = "symbolic link" if member.issym() else "hard link"
        msg = f"refusing archive entry {member.name!r}: it is a {kind}"
        raise SecurityError(
            msg,
            hint="Runtime archives must not contain links; nothing was installed.",
        )
    if member.ischr() or member.isblk() or member.isfifo():
        msg = f"refusing archive entry {member.name!r}: it is a device or FIFO"
        raise SecurityError(msg)


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------
def _copy_stream(
    source: IO[bytes],
    target: Path,
    limits: ExtractionLimits,
    already_written: int,
    name: str,
) -> int:
    """Copy an archive member, enforcing the size limit against *real* bytes.

    A header can declare 1 KB and deliver 10 GB, so the running total is checked
    on the way through rather than trusted up front.
    """
    written = 0
    try:
        with target.open("wb") as handle:
            while chunk := source.read(256 * 1024):
                written += len(chunk)
                if written > limits.max_entry_bytes:
                    msg = (
                        f"refusing archive entry {name!r}: it expands past the "
                        f"{limits.max_entry_bytes} byte per-entry limit"
                    )
                    raise SecurityError(msg)
                if already_written + written > limits.max_total_bytes:
                    msg = (
                        f"refusing archive: it expands past the {limits.max_total_bytes} byte "
                        f"total limit ({limits.describe()})"
                    )
                    raise SecurityError(msg)
                handle.write(chunk)
    finally:
        source.close()
    return written


def _check_entry_count(count: int, limits: ExtractionLimits) -> None:
    if count > limits.max_entries:
        msg = f"refusing archive: it contains more than {limits.max_entries} entries"
        raise SecurityError(msg, hint=limits.describe())


def _check_total(total: int, limits: ExtractionLimits, *, declared: bool) -> None:
    if total > limits.max_total_bytes:
        what = "declares" if declared else "expands to"
        msg = (
            f"refusing archive: it {what} {total} bytes uncompressed, over the "
            f"{limits.max_total_bytes} byte limit"
        )
        raise SecurityError(msg, hint=limits.describe())


# Directories that are part of a runtime's own layout rather than a vendor
# wrapper.  An archive whose single top-level entry is one of these is already
# rooted correctly and must not be flattened.
_LAYOUT_DIRECTORIES = frozenset(
    {"bin", "lib", "libexec", "include", "share", "scripts", "sbin", "etc", "data"}
)


def _strip_single_root(staging: Path) -> tuple[Path, str]:
    """Remove the vendor's redundant top-level folder, if there is one.

    Most runtimes ship as ``node-v18.19.0-win-x64/...``; the install root should
    be flat so that ``<root>/node.exe`` is always the answer.  But an archive
    whose only top-level entry is ``bin/`` is already rooted correctly, and
    flattening it would move ``bin/node`` to ``node`` and break the layout the
    runtime kind describes.
    """
    children = list(staging.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return staging, ""
    if children[0].name.lower() in _LAYOUT_DIRECTORIES:
        return staging, ""
    return children[0], children[0].name


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def list_archive_members(archive_path: Path) -> Iterator[str]:
    """Member names, for diagnostics.  Does not extract anything."""
    if archive_path.name.lower().endswith(_ZIP_SUFFIXES):
        with zipfile.ZipFile(archive_path) as archive:
            yield from (info.filename for info in archive.infolist())
        return
    with tarfile.open(archive_path) as archive:
        yield from archive.getnames()


def supported_suffixes() -> Sequence[str]:
    return (*_ZIP_SUFFIXES, *_TAR_SUFFIXES)
