"""Packaging: the frozen build must not be discovered broken on Windows.

PyInstaller is not installed in the development container, so what is checked
here is everything that can be: the argv composition, that every module named as
a hidden import really exists, that the entry point resolves, and that nothing in
the package reads a data file relative to ``__file__`` at import time.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

import devbox
from devbox.packaging.build_exe import (
    EXCLUDED_MODULES,
    BuildOptions,
    artifact_path,
    build,
    hidden_imports,
    pyinstaller_arguments,
)

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(devbox.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]


class TestArguments:
    def test_every_hidden_import_is_included(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions())
        for module in hidden_imports():
            assert module in arguments

    def test_the_source_directory_is_on_the_path(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions())
        assert "--paths" in arguments
        source_path = Path(arguments[arguments.index("--paths") + 1])
        assert source_path == REPO_ROOT / "src"
        assert source_path.is_dir()

    def test_default_paths_do_not_depend_on_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        options = BuildOptions()
        assert options.entry_point == PACKAGE_ROOT / "__main__.py"
        assert options.dist_dir == REPO_ROOT / "dist"
        assert options.work_dir == REPO_ROOT / "build" / "pyinstaller"

    def test_a_one_file_console_executable_is_the_default(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions())
        assert "--onefile" in arguments
        assert "--console" in arguments
        assert "--uac-admin" not in arguments

    def test_one_dir_can_be_selected(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions(one_file=False))
        assert "--onedir" in arguments
        assert "--onefile" not in arguments

    def test_the_entry_point_comes_last(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions())
        assert arguments[-1].endswith("__main__.py")

    def test_heavy_development_modules_are_excluded(self) -> None:
        arguments = pyinstaller_arguments(BuildOptions())
        for module in ("pytest", "mypy", "tkinter"):
            assert module in EXCLUDED_MODULES
            assert module in arguments


class TestBundleContents:
    def test_every_hidden_import_actually_exists(self) -> None:
        """A stale name in the list would hide a genuinely missing module."""
        for module in hidden_imports():
            assert importlib.util.find_spec(module) is not None, module

    def test_every_hidden_import_can_be_imported(self) -> None:
        for module in hidden_imports():
            importlib.import_module(module)

    def test_the_entry_point_exists_and_exposes_main(self) -> None:
        entry = PACKAGE_ROOT / "__main__.py"
        assert entry.is_file()
        module = importlib.import_module("devbox.__main__")
        assert callable(module.main)

    def test_the_console_script_target_resolves(self) -> None:
        """`devbox = devbox.cli.main:main` in pyproject.toml must be real."""
        from devbox.cli.main import main

        assert callable(main)

    def test_every_cli_command_module_is_declared_as_a_hidden_import(self) -> None:
        declared = set(hidden_imports())
        commands = PACKAGE_ROOT / "cli" / "commands"
        for path in sorted(commands.glob("*_cmd.py")):
            module = f"devbox.cli.commands.{path.stem}"
            assert module in declared, f"{module} is not listed in hidden_imports()"

    def test_every_infrastructure_provider_is_declared(self) -> None:
        declared = set(hidden_imports())
        for name in ("postgres", "mysql"):
            assert f"devbox.infra.{name}" in declared


class TestFrozenSafety:
    def test_no_module_reads_a_data_file_relative_to_dunder_file_at_import_time(
        self,
    ) -> None:
        """A frozen executable has no source tree beside it.

        ``devbox.testing.fake_runtime`` reads its own module source on purpose —
        it is a development facility, documented as unavailable when frozen —
        and does it inside a function, not at import time.
        """
        offenders: list[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                # Function and class bodies do not run at import time.
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and child.id == "__file__":
                        offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{child.lineno}")
        assert offenders == [], f"__file__ used at import time in {offenders}"

    def test_the_package_has_no_dynamic_import_of_a_computed_name(self) -> None:
        """``importlib.import_module(variable)`` is invisible to the analyser."""
        offenders: list[str] = []
        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else target.id
                    if isinstance(target, ast.Name)
                    else ""
                )
                if name not in {"import_module", "__import__"}:
                    continue
                if node.args and not isinstance(node.args[0], ast.Constant):
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")
        assert offenders == [], f"dynamic import of a computed name in {offenders}"

    def test_only_pure_python_runtime_dependencies_are_declared(self) -> None:
        """A compiled wheel makes the frozen build platform-specific."""
        import tomllib

        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["dependencies"]
        names = {entry.split(">")[0].split("[")[0].strip().lower() for entry in dependencies}
        assert names == {"typer", "pyyaml"}, names


class TestBuildEntryPoint:
    def test_build_reports_a_missing_entry_point(self, tmp_path: Path) -> None:
        code = build(BuildOptions(entry_point=tmp_path / "nope.py"))
        assert code == 2

    def test_build_reports_a_missing_pyinstaller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("devbox.packaging.build_exe.pyinstaller_available", lambda: False)
        code = build(BuildOptions(entry_point=PACKAGE_ROOT / "__main__.py"))
        assert code == 3

    def test_build_rejects_a_missing_output_after_pyinstaller_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("devbox.packaging.build_exe.pyinstaller_available", lambda: True)
        monkeypatch.setattr(
            "devbox.packaging.build_exe.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
        )
        options = BuildOptions(entry_point=PACKAGE_ROOT / "__main__.py", dist_dir=tmp_path)
        assert build(options) == 4

    def test_successful_build_writes_a_sha256_sidecar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("devbox.packaging.build_exe.pyinstaller_available", lambda: True)
        monkeypatch.setattr(
            "devbox.packaging.build_exe.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
        )
        options = BuildOptions(entry_point=PACKAGE_ROOT / "__main__.py", dist_dir=tmp_path)
        produced = artifact_path(options)
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"release candidate")

        assert build(options) == 0
        checksum = produced.with_name(f"{produced.name}.sha256")
        expected = hashlib.sha256(b"release candidate").hexdigest()
        assert checksum.read_text(encoding="ascii") == f"{expected}  {produced.name}\n"
