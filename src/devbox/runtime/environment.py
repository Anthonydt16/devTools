"""Building the environment for one child process.

This is the mechanism that replaces "install Node globally and hope".  Devbox
never touches the machine or user ``PATH``; instead it composes a fresh
environment **per child**, prepends that service's runtime ``bin`` directories to
a copy of the parent ``PATH``, and hands the result to ``subprocess.Popen(env=)``.

Because nothing global arbitrates, two services can run two different Node
versions in the same ``devbox up``:

    legacy-api  ->  PATH = <runtimes>/node/18.19.0;<inherited PATH>
    new-api     ->  PATH = <runtimes>/node/24.6.0;<inherited PATH>

and ``devbox exec legacy-api node --version`` prints ``v18.19.0`` while
``devbox exec new-api node --version`` prints ``v24.6.0``.  That is
non-regression test #1, and it is tested with real processes.

The environment is built as pure data — no ``os.environ`` mutation, no global
state — which is why it can be asserted on directly in unit tests.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devbox.config.models import RuntimeRequirement
from devbox.host.platform_info import Platform, current_platform
from devbox.runtime.catalog import runtime_kind

__all__ = ["RuntimeEnvironment", "build_environment"]

_PATH_KEY = "PATH"

# Variables that would let an inherited setting break a service in a way that is
# very hard to diagnose.  They are dropped from the child environment unless the
# service sets them itself.
_STRIPPED_VARIABLES = frozenset(
    {
        "NODE_OPTIONS",
        "NODE_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """A fully resolved child environment."""

    variables: Mapping[str, str]
    path_entries: tuple[str, ...]
    runtime_roots: Mapping[str, str] = field(default_factory=dict)
    platform: Platform = field(default_factory=current_platform)

    @property
    def path(self) -> str:
        return self.platform.path_separator.join(self.path_entries)

    def as_dict(self) -> dict[str, str]:
        """The mapping to hand to ``subprocess.Popen(env=...)``."""
        return dict(self.variables)

    def resolve_executable(self, program: str) -> str | None:
        """Find *program* using **this** environment's PATH, not the machine's."""
        from devbox.security.command import resolve_executable

        return resolve_executable(
            program,
            path_entries=self.path_entries,
            is_windows=self.platform.is_windows,
        )

    def describe(self) -> str:
        roots = ", ".join(f"{name} -> {root}" for name, root in sorted(self.runtime_roots.items()))
        return roots or "no runtimes"


def build_environment(
    requirements: Sequence[RuntimeRequirement],
    *,
    runtime_root: Path,
    base_environment: Mapping[str, str] | None = None,
    extra_variables: Mapping[str, str] | None = None,
    platform: Platform | None = None,
    strip_inherited: bool = True,
) -> RuntimeEnvironment:
    """Compose the environment for a child that needs *requirements*.

    ``PATH`` is built as ``[runtime bins in declaration order] + [inherited]``.
    The inherited part is kept so that ``git``, ``cmd`` and the system tools stay
    reachable — a dev tool that hides the rest of the machine is unusable — but
    the runtime entries come first, so a globally installed Node can never win
    over the pinned one.

    *extra_variables* (a service's own ``env:`` block) is applied last and can
    override anything, including ``PATH``: the user is allowed the final word.
    """
    target = platform or current_platform()
    base = dict(os.environ if base_environment is None else base_environment)

    if strip_inherited:
        for name in _STRIPPED_VARIABLES:
            base.pop(name, None)

    inherited_path = _inherited_path_entries(base, target)

    runtime_entries: list[str] = []
    runtime_variables: dict[str, str] = {}
    roots: dict[str, str] = {}

    for requirement in requirements:
        root = (runtime_root / requirement.name / requirement.version).resolve()
        roots[requirement.name] = str(root)
        kind = runtime_kind(requirement.name)
        subdirs = kind.bin_subdirs if kind is not None else (".",)
        for subdir in subdirs:
            candidate = root if subdir == "." else root / subdir
            entry = str(candidate)
            if entry not in runtime_entries:
                runtime_entries.append(entry)
        if kind is not None:
            runtime_variables.update(kind.resolved_env(str(root)))

    path_entries = tuple(runtime_entries + [e for e in inherited_path if e not in runtime_entries])

    variables = {**base, **runtime_variables}
    variables[_PATH_KEY] = target.path_separator.join(path_entries)
    variables["DEVBOX_RUNTIME_ROOT"] = str(runtime_root)
    if extra_variables:
        variables.update(extra_variables)
        if _PATH_KEY in extra_variables:
            path_entries = tuple(extra_variables[_PATH_KEY].split(target.path_separator))

    return RuntimeEnvironment(
        variables=variables,
        path_entries=path_entries,
        runtime_roots=roots,
        platform=target,
    )


def _inherited_path_entries(base: Mapping[str, str], platform: Platform) -> list[str]:
    """Read PATH from the parent environment, case-insensitively on Windows.

    Windows environment variables are case-insensitive, so a parent may hand us
    ``Path`` rather than ``PATH``; missing that would silently drop the whole
    inherited search path.
    """
    if platform.is_windows:
        for key, value in base.items():
            if key.upper() == _PATH_KEY:
                return [entry for entry in value.split(platform.path_separator) if entry]
        return []
    return [entry for entry in base.get(_PATH_KEY, "").split(platform.path_separator) if entry]
