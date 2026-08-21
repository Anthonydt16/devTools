"""Per-child environment construction — the mechanism behind runtime isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.config.models import RuntimeRequirement
from devbox.host.platform_info import PLATFORM_LINUX_X64, PLATFORM_WINDOWS_X64
from devbox.runtime.catalog import RuntimeKind, register_runtime_kind, unregister_runtime_kind
from devbox.runtime.environment import build_environment

pytestmark = pytest.mark.unit

WINDOWS_PATH = "C:\\Windows;C:\\Windows\\System32"


def node(version: str) -> list[RuntimeRequirement]:
    return [RuntimeRequirement("node", version)]


class TestPathConstruction:
    def test_runtime_bins_come_first(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": WINDOWS_PATH},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.path_entries[0] == str(tmp_path / "node" / "18.19.0")

    def test_the_inherited_path_is_preserved(self, tmp_path: Path) -> None:
        """git, cmd and the system tools must stay reachable."""
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": WINDOWS_PATH},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert "C:\\Windows\\System32" in env.path_entries

    def test_windows_path_is_read_case_insensitively(self, tmp_path: Path) -> None:
        """A parent process may hand us 'Path' rather than 'PATH'."""
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"Path": WINDOWS_PATH},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert "C:\\Windows" in env.path_entries

    def test_windows_uses_semicolons_and_posix_uses_colons(self, tmp_path: Path) -> None:
        windows = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={},
            platform=PLATFORM_WINDOWS_X64,
        )
        posix = build_environment(
            node("18.19.0"), runtime_root=tmp_path, base_environment={}, platform=PLATFORM_LINUX_X64
        )
        assert ";" in windows.path or len(windows.path_entries) == 1
        assert ":" in posix.path or len(posix.path_entries) == 1

    def test_no_duplicate_entries(self, tmp_path: Path) -> None:
        shared = str(tmp_path / "node" / "18.19.0")
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": shared},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.path_entries.count(shared) == 1

    def test_several_runtimes_are_all_on_the_path(self, tmp_path: Path) -> None:
        env = build_environment(
            [RuntimeRequirement("node", "18.19.0"), RuntimeRequirement("python", "3.13.1")],
            runtime_root=tmp_path,
            base_environment={},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert str(tmp_path / "node" / "18.19.0") in env.path_entries
        assert str(tmp_path / "python" / "3.13.1") in env.path_entries


class TestIsolation:
    """Non-regression #1, at the level of the mechanism that provides it."""

    def test_two_node_versions_produce_two_different_paths(self, tmp_path: Path) -> None:
        legacy = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": WINDOWS_PATH},
            platform=PLATFORM_WINDOWS_X64,
        )
        modern = build_environment(
            node("24.6.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": WINDOWS_PATH},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert legacy.path != modern.path
        assert "18.19.0" in legacy.path_entries[0]
        assert "24.6.0" in modern.path_entries[0]

    def test_building_one_environment_does_not_change_the_other(self, tmp_path: Path) -> None:
        base = {"PATH": WINDOWS_PATH}
        first = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment=base,
            platform=PLATFORM_WINDOWS_X64,
        )
        build_environment(
            node("24.6.0"),
            runtime_root=tmp_path,
            base_environment=base,
            platform=PLATFORM_WINDOWS_X64,
        )
        assert base == {"PATH": WINDOWS_PATH}, "the caller's environment must not be mutated"
        assert "18.19.0" in first.path

    def test_no_global_state_is_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/sentinel")
        build_environment(node("18.19.0"), runtime_root=tmp_path, platform=PLATFORM_WINDOWS_X64)
        import os

        assert os.environ["PATH"] == "/sentinel"


class TestInheritedVariables:
    def test_dangerous_inherited_variables_are_stripped(self, tmp_path: Path) -> None:
        """An inherited NODE_OPTIONS breaks a service in a way nobody can diagnose."""
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"NODE_OPTIONS": "--inspect", "VIRTUAL_ENV": "/other", "HOME": "/h"},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert "NODE_OPTIONS" not in env.variables
        assert "VIRTUAL_ENV" not in env.variables
        assert env.variables["HOME"] == "/h", "harmless variables are preserved"

    def test_stripping_can_be_disabled(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"NODE_OPTIONS": "--inspect"},
            platform=PLATFORM_WINDOWS_X64,
            strip_inherited=False,
        )
        assert env.variables["NODE_OPTIONS"] == "--inspect"

    def test_the_runtime_kind_exports_its_own_variables(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.variables["NODE_PATH"].endswith("lib/node_modules")

    def test_service_variables_win(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"API_URL": "inherited"},
            extra_variables={"API_URL": "declared", "NODE_PATH": "custom"},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.variables["API_URL"] == "declared"
        assert env.variables["NODE_PATH"] == "custom"

    def test_a_service_may_override_path_entirely(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={"PATH": WINDOWS_PATH},
            extra_variables={"PATH": "D:\\only"},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.path_entries == ("D:\\only",)


class TestExecutableResolution:
    def test_windows_extensions_are_tried(self, tmp_path: Path) -> None:
        install = tmp_path / "node" / "18.19.0"
        install.mkdir(parents=True)
        (install / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={},
            platform=PLATFORM_WINDOWS_X64,
        )
        found = env.resolve_executable("npm")
        assert found is not None
        assert found.endswith("npm.cmd")

    def test_an_unknown_program_resolves_to_nothing(self, tmp_path: Path) -> None:
        env = build_environment(
            node("18.19.0"),
            runtime_root=tmp_path,
            base_environment={},
            platform=PLATFORM_WINDOWS_X64,
        )
        assert env.resolve_executable("definitely-not-installed") is None


class TestExtensibility:
    def test_a_new_runtime_kind_needs_no_change_to_the_builder(self, tmp_path: Path) -> None:
        """Adding a runtime family is a registration, not a code change."""
        register_runtime_kind(
            RuntimeKind(
                name="dotnet",
                bin_subdirs=("sdk", "."),
                probe_executable="dotnet",
                env={"DOTNET_ROOT": "{root}"},
            )
        )
        try:
            env = build_environment(
                [RuntimeRequirement("dotnet", "9.0.100")],
                runtime_root=tmp_path,
                base_environment={},
                platform=PLATFORM_WINDOWS_X64,
            )
            assert env.path_entries[0] == str(tmp_path / "dotnet" / "9.0.100" / "sdk")
            assert env.variables["DOTNET_ROOT"] == str(tmp_path / "dotnet" / "9.0.100")
        finally:
            unregister_runtime_kind("dotnet")
