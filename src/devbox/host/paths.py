"""Filesystem layout.

Devbox owns exactly one directory tree and never writes outside it (plus the
project's own ``.devbox/`` folder).  On Windows that root is
``%LOCALAPPDATA%\\Devbox`` — a per-user location that requires no elevation.

The root can be overridden with ``DEVBOX_HOME``.  Tests rely on this; so does
anyone who wants runtimes on another drive.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devbox.errors import StateError

__all__ = ["DEVBOX_HOME_ENV", "DevboxPaths", "ProjectPaths", "resolve_devbox_home"]

DEVBOX_HOME_ENV = "DEVBOX_HOME"
"""Environment variable overriding the Devbox root directory."""

_APP_DIR_NAME = "Devbox"


def resolve_devbox_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the Devbox root directory for this user.

    Resolution order:

    1. ``DEVBOX_HOME`` if set (used by tests and by power users);
    2. ``%LOCALAPPDATA%\\Devbox`` on Windows;
    3. ``$XDG_DATA_HOME/devbox`` or ``~/.local/share/devbox`` elsewhere.

    The path is returned absolute but is *not* created; call
    :meth:`DevboxPaths.ensure` for that.
    """
    env = os.environ if environ is None else environ

    override = env.get(DEVBOX_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()

    local_appdata = env.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata).expanduser().resolve() / _APP_DIR_NAME

    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "devbox"

    home = env.get("USERPROFILE") or env.get("HOME")
    base = Path(home).expanduser() if home else Path.home()
    return (base / ".local" / "share" / "devbox").resolve()


@dataclass(frozen=True, slots=True)
class DevboxPaths:
    """The user-level Devbox tree.

    ``root`` is the only mutable location Devbox uses outside of a project.  No
    part of Devbox writes to ``Program Files``, the machine registry, or the
    system ``PATH``.
    """

    root: Path

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> DevboxPaths:
        return cls(root=resolve_devbox_home(environ))

    # -- top-level directories ------------------------------------------------
    @property
    def runtimes(self) -> Path:
        """Installed runtimes: ``runtimes/<name>/<version>/``."""
        return self.root / "runtimes"

    @property
    def cache(self) -> Path:
        """Verified artifacts, keyed by content hash."""
        return self.root / "cache"

    @property
    def downloads(self) -> Path:
        """Partial downloads.  Everything here is disposable."""
        return self.root / "downloads"

    @property
    def infra(self) -> Path:
        """Shared infrastructure data and logs — never deleted by ``devbox down``."""
        return self.root / "infra"

    @property
    def logs(self) -> Path:
        """User-level logs (infrastructure, Devbox itself)."""
        return self.root / "logs"

    @property
    def state(self) -> Path:
        """Process ownership records."""
        return self.root / "state"

    @property
    def tmp(self) -> Path:
        """Scratch space for atomic moves; always on the same volume as ``root``."""
        return self.root / "tmp"

    ALL_DIR_NAMES = ("runtimes", "cache", "downloads", "infra", "logs", "state", "tmp")

    def all_directories(self) -> tuple[Path, ...]:
        return tuple(getattr(self, name) for name in self.ALL_DIR_NAMES)

    # -- derived locations ----------------------------------------------------
    def runtime_dir(self, name: str, version: str) -> Path:
        """Install location for one runtime version."""
        _reject_path_component(name, "runtime name")
        _reject_path_component(version, "runtime version")
        return self.runtimes / name / version

    def infra_dir(self, provider: str, instance: str) -> Path:
        """Root of one shared infrastructure instance."""
        _reject_path_component(provider, "provider")
        _reject_path_component(instance, "infrastructure name")
        return self.infra / provider / instance

    def infra_data_dir(self, provider: str, instance: str) -> Path:
        return self.infra_dir(provider, instance) / "data"

    def infra_logs_dir(self, provider: str, instance: str) -> Path:
        return self.infra_dir(provider, instance) / "logs"

    def ensure(self) -> DevboxPaths:
        """Create the whole tree if needed.  Idempotent; safe to call on every run."""
        try:
            for directory in (self.root, *self.all_directories()):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"cannot create the Devbox home directory at {self.root}: {exc}"
            raise StateError(
                msg,
                hint=(
                    f"Set {DEVBOX_HOME_ENV} to a writable location, "
                    "e.g. $env:DEVBOX_HOME='D:\\devbox'."
                ),
            ) from exc
        return self

    def missing_directories(self) -> tuple[Path, ...]:
        """Directories that should exist but do not — used by ``devbox status``."""
        return tuple(d for d in self.all_directories() if not d.is_dir())


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Per-project locations, all under ``<project>/.devbox``.

    Kept separate from :class:`DevboxPaths` on purpose: deleting ``.devbox`` must
    never take shared infrastructure data with it.
    """

    project_root: Path

    @property
    def devbox_dir(self) -> Path:
        return self.project_root / ".devbox"

    @property
    def logs(self) -> Path:
        return self.devbox_dir / "logs"

    @property
    def run(self) -> Path:
        """Ephemeral per-run scratch (pid files, resolved env dumps)."""
        return self.devbox_dir / "run"

    @property
    def dist(self) -> Path:
        return self.project_root / "dist"

    @property
    def config_file(self) -> Path:
        return self.project_root / "devbox.yml"

    @property
    def lock_file(self) -> Path:
        return self.project_root / "devbox.lock"

    def service_stdout_log(self, service: str) -> Path:
        _reject_path_component(service, "service name")
        return self.logs / f"{service}.out.log"

    def service_stderr_log(self, service: str) -> Path:
        _reject_path_component(service, "service name")
        return self.logs / f"{service}.err.log"

    def ensure(self) -> ProjectPaths:
        for directory in (self.devbox_dir, self.logs, self.run):
            directory.mkdir(parents=True, exist_ok=True)
        return self


_FORBIDDEN_COMPONENT_CHARS = frozenset('<>:"/\\|?*')


def _reject_path_component(value: str, label: str) -> None:
    """Refuse anything that would escape the directory it is joined into.

    This is a defence-in-depth check: names also go through config validation,
    but they can reach here from a lockfile or a registry manifest too.
    """
    from devbox.errors import SecurityError  # local import: keeps errors.py leaf-free

    if not value or value in {".", ".."}:
        msg = f"invalid {label}: {value!r}"
        raise SecurityError(msg)
    if any(char in _FORBIDDEN_COMPONENT_CHARS for char in value):
        msg = (
            f"invalid {label}: {value!r} contains a path separator or a character "
            "that is illegal in a Windows filename"
        )
        raise SecurityError(msg)
