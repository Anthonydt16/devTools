"""Which runtime *kinds* exist (``node``, ``python``, ...).

A "kind" is only a namespace in the registry plus the rules for turning an
extracted directory into a usable ``PATH`` entry.  Adding a kind must not
require touching the orchestrator, so kinds live in a registry that anything can
extend at import time — see ``tests/unit/test_extensibility.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = ["RuntimeKind", "known_runtime_kinds", "register_runtime_kind", "runtime_kind"]


@dataclass(frozen=True, slots=True)
class RuntimeKind:
    """Describes how one family of runtimes lays itself out on disk."""

    name: str

    bin_subdirs: tuple[str, ...] = (".",)
    """Directories, relative to the install root, appended to ``PATH`` in order.

    Node ships its executables at the archive root on Windows (``node.exe``) but
    under ``bin/`` on POSIX; listing both is harmless and keeps one description
    valid on every platform.
    """

    probe_executable: str = ""
    """Executable used by ``devbox runtime list`` to prove the install works."""

    probe_args: tuple[str, ...] = ("--version",)

    env: Mapping[str, str] = field(default_factory=dict)
    """Extra environment variables exported for children using this runtime.

    Values may contain ``{root}``, replaced by the install directory.
    """

    def bin_paths(self, root: str, separator: str = "/") -> tuple[str, ...]:
        out: list[str] = []
        for subdir in self.bin_subdirs:
            out.append(root if subdir == "." else f"{root}{separator}{subdir}")
        return tuple(out)

    def resolved_env(self, root: str) -> dict[str, str]:
        return {key: value.replace("{root}", root) for key, value in self.env.items()}


_KINDS: dict[str, RuntimeKind] = {}


def register_runtime_kind(kind: RuntimeKind) -> RuntimeKind:
    """Register (or replace) a runtime kind.  Returns it for use as a constant."""
    _KINDS[kind.name] = kind
    return kind


def runtime_kind(name: str) -> RuntimeKind | None:
    return _KINDS.get(name)


def known_runtime_kinds() -> frozenset[str]:
    return frozenset(_KINDS)


def unregister_runtime_kind(name: str) -> None:
    """Test helper: remove a kind registered by a test so state does not leak."""
    _KINDS.pop(name, None)


def _install_defaults(kinds: Iterable[RuntimeKind]) -> None:
    for kind in kinds:
        register_runtime_kind(kind)


NODE = RuntimeKind(
    name="node",
    # Windows Node archives put node.exe/npm.cmd at the root; POSIX builds use bin/.
    bin_subdirs=(".", "bin"),
    probe_executable="node",
    env={"NODE_PATH": "{root}/lib/node_modules"},
)

PYTHON = RuntimeKind(
    name="python",
    bin_subdirs=(".", "Scripts", "bin"),
    probe_executable="python",
)

POSTGRES = RuntimeKind(
    name="postgres",
    bin_subdirs=("bin", "."),
    probe_executable="postgres",
)

MYSQL = RuntimeKind(
    name="mysql",
    bin_subdirs=("bin", "."),
    probe_executable="mysqld",
)

_install_defaults((NODE, PYTHON, POSTGRES, MYSQL))


def default_kind_names() -> Sequence[str]:
    return ("node", "python", "postgres", "mysql")
