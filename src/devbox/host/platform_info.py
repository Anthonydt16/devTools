"""Platform identification.

Devbox targets ``windows-x64``.  Other platforms are supported for *development*
of Devbox itself: business logic, fakes and the POSIX supervisor all run there,
but a runtime artifact published only for ``windows-x64`` will be refused with an
explicit error rather than silently installed.
"""

from __future__ import annotations

import platform as _stdlib_platform
import sys
from dataclasses import dataclass

__all__ = [
    "PLATFORM_LINUX_ARM64",
    "PLATFORM_LINUX_X64",
    "PLATFORM_MACOS_ARM64",
    "PLATFORM_WINDOWS_ARM64",
    "PLATFORM_WINDOWS_X64",
    "Platform",
    "current_platform",
    "is_windows",
]


@dataclass(frozen=True, slots=True, order=True)
class Platform:
    """A registry platform tag, e.g. ``windows-x64``."""

    os: str
    arch: str

    def __str__(self) -> str:
        return f"{self.os}-{self.arch}"

    @property
    def tag(self) -> str:
        return str(self)

    @property
    def is_windows(self) -> bool:
        return self.os == "windows"

    @property
    def executable_suffix(self) -> str:
        return ".exe" if self.is_windows else ""

    @property
    def path_separator(self) -> str:
        return ";" if self.is_windows else ":"

    @classmethod
    def parse(cls, tag: str) -> Platform:
        """Parse ``os-arch``.  Raises :class:`ValueError` on anything else."""
        parts = tag.strip().split("-")
        if len(parts) != 2 or not all(parts):
            msg = f"invalid platform tag {tag!r}, expected '<os>-<arch>' (e.g. 'windows-x64')"
            raise ValueError(msg)
        return cls(os=parts[0].lower(), arch=parts[1].lower())


PLATFORM_WINDOWS_X64 = Platform("windows", "x64")
PLATFORM_WINDOWS_ARM64 = Platform("windows", "arm64")
PLATFORM_LINUX_X64 = Platform("linux", "x64")
PLATFORM_LINUX_ARM64 = Platform("linux", "arm64")
PLATFORM_MACOS_ARM64 = Platform("macos", "arm64")

_OS_ALIASES = {"win32": "windows", "cygwin": "windows", "darwin": "macos"}
_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
}


def current_platform() -> Platform:
    """The registry platform tag for the machine Devbox is running on."""
    raw_os = _OS_ALIASES.get(sys.platform, sys.platform)
    raw_arch = _stdlib_platform.machine().lower()
    return Platform(os=raw_os, arch=_ARCH_ALIASES.get(raw_arch, raw_arch))


def is_windows() -> bool:
    """True on a real Windows host.

    This is the single place Devbox is allowed to ask the question; everything
    else selects an implementation through a factory that consults it once.
    """
    return sys.platform == "win32"
