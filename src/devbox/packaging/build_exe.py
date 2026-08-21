"""Building ``devbox.exe`` with PyInstaller.

A single self-contained executable is the point: a developer on a locked-down
Windows 11 machine may not be able to ``pip install`` anything, but they can
always run an ``.exe`` from their own profile.

Three constraints shaped the code base so this works, and they are checked here
rather than discovered at runtime:

* **no dynamic imports PyInstaller cannot see** — every sub-command module is
  imported explicitly by ``devbox.cli.commands``, and the infrastructure
  providers are imported by ``devbox.infra``.  ``hiddenimports`` below names
  them anyway, so a future refactor that breaks the chain fails loudly.
* **no path relative to ``__file__`` left unresolved** — a frozen executable has
  no source tree.  Devbox reads no data files at runtime; the only module that
  touches its own source (``devbox.testing.fake_runtime``) is a development
  facility and says so.
* **one runtime dependency** (Typer) plus PyYAML, both pure Python.

Run it with::

    pip install -e ".[package]"
    python -m devbox.packaging.build_exe
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BuildOptions", "build", "hidden_imports", "pyinstaller_arguments"]


def hidden_imports() -> tuple[str, ...]:
    """Modules that must be in the bundle even if the analyser misses them.

    Every one of these is also imported explicitly somewhere in the package;
    listing them is belt and braces, and it documents what the frozen build
    needs.
    """
    return (
        "devbox.cli.commands.build_cmd",
        "devbox.cli.commands.exec_cmd",
        "devbox.cli.commands.infra_cmd",
        "devbox.cli.commands.init_cmd",
        "devbox.cli.commands.install_cmd",
        "devbox.cli.commands.logs_cmd",
        "devbox.cli.commands.run_cmd",
        "devbox.cli.commands.runtime_cmd",
        "devbox.cli.commands.status_cmd",
        "devbox.cli.commands.test_cmd",
        "devbox.cli.commands.up_down_cmd",
        "devbox.infra.mysql",
        "devbox.infra.postgres",
        "devbox.process.posix",
        "devbox.process.windows",
    )


EXCLUDED_MODULES: tuple[str, ...] = (
    # Nothing in Devbox uses these; excluding them keeps the executable small.
    "tkinter",
    "unittest",
    "pydoc_data",
    "test",
    "distutils",
    "setuptools",
    "pip",
    "pytest",
    "mypy",
    "ruff",
)


@dataclass(frozen=True, slots=True)
class BuildOptions:
    """How to build the executable."""

    name: str = "devbox"
    entry_point: Path = field(default_factory=lambda: Path("src/devbox/__main__.py"))
    dist_dir: Path = field(default_factory=lambda: Path("dist"))
    work_dir: Path = field(default_factory=lambda: Path("build/pyinstaller"))
    one_file: bool = True
    console: bool = True
    clean: bool = True


def pyinstaller_arguments(options: BuildOptions) -> list[str]:
    """The exact argv handed to PyInstaller.

    Returned as data so the composition is unit-tested without PyInstaller being
    installed — which it is not, in the Linux development container.
    """
    arguments = [
        "--name",
        options.name,
        "--distpath",
        str(options.dist_dir),
        "--workpath",
        str(options.work_dir),
        "--specpath",
        str(options.work_dir),
        "--noconfirm",
        "--console" if options.console else "--windowed",
        "--onefile" if options.one_file else "--onedir",
        # src/ is not on sys.path in a frozen build unless it is added here.
        "--paths",
        "src",
    ]
    if options.clean:
        arguments.append("--clean")
    for module in hidden_imports():
        arguments += ["--hidden-import", module]
    for module in EXCLUDED_MODULES:
        arguments += ["--exclude-module", module]
    arguments.append(str(options.entry_point))
    return arguments


def pyinstaller_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("PyInstaller") is not None


def build(options: BuildOptions | None = None, *, argv: Sequence[str] | None = None) -> int:
    """Run PyInstaller.  Returns its exit code."""
    settings = options or BuildOptions()
    if not settings.entry_point.is_file():
        print(f"entry point not found: {settings.entry_point}", file=sys.stderr)
        return 2
    if not pyinstaller_available():
        print(
            'PyInstaller is not installed.\n  pip install -e ".[package]"',
            file=sys.stderr,
        )
        return 3

    arguments = list(argv) if argv is not None else pyinstaller_arguments(settings)
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "PyInstaller", *arguments],
        check=False,
    )
    if completed.returncode == 0:
        suffix = ".exe" if sys.platform == "win32" else ""
        produced = settings.dist_dir / f"{settings.name}{suffix}"
        print(f"\nBuilt {produced}")
        if produced.is_file():
            print(f"  size: {produced.stat().st_size / 1024 / 1024:.1f} MB")
    return completed.returncode


def clean(options: BuildOptions | None = None) -> None:
    """Remove build artifacts."""
    settings = options or BuildOptions()
    shutil.rmtree(settings.work_dir, ignore_errors=True)


def main() -> int:
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
