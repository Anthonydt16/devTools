"""Path containment.

Anything that turns *external input* — a registry index, an archive member, a
service name — into a filesystem path goes through here.  The rule is always the
same: the result must stay inside a directory we chose.

Windows makes this harder than POSIX and every difference is handled:

* both ``/`` and ``\\`` separate components;
* a leading ``C:``, ``\\\\server\\share`` or ``/`` makes a path absolute;
* ``..`` may appear anywhere, including after a legitimate prefix;
* reserved device names (``CON``, ``NUL``, ``COM1``...) are refused because
  opening them for writing has side effects;
* a trailing dot or space is stripped by the Win32 layer, so ``evil.exe.``
  and ``evil.exe`` name the same file and must be treated as suspicious.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from devbox.errors import SecurityError

__all__ = [
    "RESERVED_WINDOWS_NAMES",
    "is_safe_relative_path",
    "normalise_archive_member",
    "resolve_within",
]

RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def is_safe_relative_path(candidate: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a path that must stay inside a base directory.

    Rejects absolute paths, drive letters, UNC prefixes, ``..`` traversal,
    reserved Windows device names, NUL bytes and names that Win32 would silently
    rewrite.
    """
    if not candidate or candidate in {".", "./"}:
        return False, "empty path"
    if "\x00" in candidate:
        return False, "path contains a NUL byte"

    unified = candidate.replace("\\", "/")

    if unified.startswith("//"):
        return False, "UNC path"
    if unified.startswith("/"):
        return False, "absolute path"
    if _DRIVE_RE.match(candidate):
        return False, "absolute path with a drive letter"

    parts = [part for part in unified.split("/") if part not in ("", ".")]
    if not parts:
        return False, "empty path"
    for part in parts:
        if part == "..":
            return False, "path traversal via '..'"
        stem = part.split(".", 1)[0].upper()
        if stem in RESERVED_WINDOWS_NAMES:
            return False, f"reserved Windows device name {part!r}"
        if part != part.rstrip(". "):
            return False, f"component {part!r} ends with a dot or a space"
        if any(char in part for char in '<>:"|?*'):
            return False, f"component {part!r} contains a character illegal on Windows"
    return True, ""


def normalise_archive_member(name: str) -> str:
    """Validate an archive member name and return it in POSIX form.

    Raises :class:`SecurityError` with the exact reason — the message ends up in
    front of the user, who needs to know *which* entry was refused.
    """
    ok, reason = is_safe_relative_path(name)
    if not ok:
        msg = f"refusing archive entry {name!r}: {reason}"
        raise SecurityError(
            msg,
            hint="The archive is not trustworthy; nothing was extracted.",
        )
    unified = name.replace("\\", "/")
    normalised = posixpath.normpath(unified)
    if normalised.startswith(("/", "../")) or normalised == "..":
        msg = f"refusing archive entry {name!r}: path traversal after normalisation"
        raise SecurityError(msg)
    return normalised


def resolve_within(base: Path, relative: str, *, what: str = "path") -> Path:
    """Join *relative* onto *base* and prove the result is still inside *base*.

    The check is done on the fully resolved paths, so a symlink or junction
    inside *base* that points outside it is caught too.
    """
    ok, reason = is_safe_relative_path(relative)
    if not ok:
        msg = f"refusing {what} {relative!r}: {reason}"
        raise SecurityError(msg)

    base_resolved = base.resolve()
    candidate = (base_resolved / relative.replace("\\", "/")).resolve()
    if not _is_within(candidate, base_resolved):
        msg = f"refusing {what} {relative!r}: it resolves outside {base_resolved}"
        raise SecurityError(
            msg,
            hint="This is what a Zip Slip / path traversal attempt looks like.",
        )
    return candidate


def _is_within(candidate: Path, base: Path) -> bool:
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def contains_traversal(name: str) -> bool:
    """True when *name* would escape its directory.  Convenience for tests/logs."""
    return not is_safe_relative_path(name)[0]


def as_windows_path(path: str) -> PureWindowsPath:
    """Parse *path* with Windows semantics, whatever the host OS."""
    return PureWindowsPath(path)


def as_posix_path(path: str) -> PurePosixPath:
    return PurePosixPath(path)
