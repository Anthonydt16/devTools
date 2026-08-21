"""Command construction: splitting, display, executable resolution, flags."""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.errors import ConfigError
from devbox.security.command import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    WINDOWS_EXECUTABLE_EXTENSIONS,
    creation_flags,
    environment_for_display,
    quote_for_display,
    resolve_executable,
    split_command,
)

pytestmark = pytest.mark.unit


class TestSplitting:
    def test_a_simple_command(self) -> None:
        assert split_command("npm run dev") == ("npm", "run", "dev")

    def test_quotes_group_arguments(self) -> None:
        assert split_command('node "my server.js"') == ("node", "my server.js")

    def test_a_windows_path_inside_quotes_keeps_its_backslashes(self) -> None:
        argv = split_command('node "C:\\Program Files\\app\\server.js"')
        assert argv[1] == "C:\\Program Files\\app\\server.js"

    def test_a_list_is_returned_verbatim(self) -> None:
        raw = ["node", "-e", "console.log('a b')"]
        assert split_command(raw) == tuple(raw)

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert split_command("  npm  run   dev  ") == ("npm", "run", "dev")

    def test_an_empty_command_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="the command is empty"):
            split_command("   ")

    def test_an_empty_list_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="the command is empty"):
            split_command([])

    def test_an_unbalanced_quote_is_reported_with_a_hint(self) -> None:
        with pytest.raises(ConfigError) as caught:
            split_command('node "unclosed')
        assert caught.value.hint is not None
        assert "YAML list" in caught.value.hint

    def test_the_location_appears_in_the_error(self) -> None:
        with pytest.raises(ConfigError, match=r"services\.api\.command"):
            split_command("", where="services.api.command")


class TestDisplay:
    def test_metacharacters_are_quoted_for_display_only(self) -> None:
        rendered = quote_for_display(("echo", "hello world", "&&", "rm"))
        assert rendered == "echo 'hello world' '&&' rm"

    def test_a_plain_command_is_rendered_plainly(self) -> None:
        assert quote_for_display(("npm", "run", "dev")) == "npm run dev"


class TestExecutableResolution:
    @pytest.fixture
    def files(self) -> set[str]:
        return {
            "/rt/node18/node.exe",
            "/rt/node18/npm.cmd",
            "/rt/node24/node.exe",
            "/rt/posix/node",
        }

    def test_windows_extensions_are_tried_in_order(self, files: set[str]) -> None:
        found = resolve_executable(
            "npm", path_entries=["/rt/node18"], is_windows=True, exists=lambda c: c in files
        )
        assert found == "/rt/node18/npm.cmd"

    def test_the_first_matching_path_entry_wins(self, files: set[str]) -> None:
        found = resolve_executable(
            "node",
            path_entries=["/rt/node24", "/rt/node18"],
            is_windows=True,
            exists=lambda c: c in files,
        )
        assert found == "/rt/node24/node.exe"

    def test_no_extension_is_appended_on_posix(self, files: set[str]) -> None:
        assert (
            resolve_executable(
                "node", path_entries=["/rt/node18"], is_windows=False, exists=lambda c: c in files
            )
            is None
        )
        assert (
            resolve_executable(
                "node", path_entries=["/rt/posix"], is_windows=False, exists=lambda c: c in files
            )
            == "/rt/posix/node"
        )

    def test_an_empty_path_entry_is_skipped(self, files: set[str]) -> None:
        found = resolve_executable(
            "node", path_entries=["", "/rt/node18"], is_windows=True, exists=lambda c: c in files
        )
        assert found == "/rt/node18/node.exe"

    def test_a_program_given_as_a_path_is_used_directly(self, files: set[str]) -> None:
        found = resolve_executable(
            "/rt/node18/node.exe", path_entries=[], is_windows=True, exists=lambda c: c in files
        )
        assert found == "/rt/node18/node.exe"

    def test_a_path_without_its_extension_still_resolves_on_windows(self, files: set[str]) -> None:
        found = resolve_executable(
            "/rt/node18/node", path_entries=[], is_windows=True, exists=lambda c: c in files
        )
        assert found == "/rt/node18/node.exe"

    def test_an_unknown_program_resolves_to_nothing(self, files: set[str]) -> None:
        assert (
            resolve_executable(
                "ghost", path_entries=["/rt/node18"], is_windows=True, exists=lambda c: c in files
            )
            is None
        )

    def test_the_default_predicate_looks_at_the_real_filesystem(self, tmp_path: Path) -> None:
        (tmp_path / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
        found = resolve_executable("tool", path_entries=[str(tmp_path)], is_windows=False)
        assert found == f"{tmp_path}/tool"

    def test_the_extension_list_is_what_windows_needs(self) -> None:
        assert WINDOWS_EXECUTABLE_EXTENSIONS[:3] == (".exe", ".cmd", ".bat")


class TestCreationFlags:
    def test_posix_gets_no_flags(self) -> None:
        from devbox.host.platform_info import is_windows

        if is_windows():  # pragma: no cover - covered by the Windows job
            pytest.skip("this asserts the POSIX branch")
        assert creation_flags() == 0

    def test_the_win32_constants_are_the_documented_values(self) -> None:
        assert CREATE_NO_WINDOW == 0x08000000
        assert CREATE_NEW_PROCESS_GROUP == 0x00000200


class TestSecretMasking:
    def test_secret_looking_variables_are_masked(self) -> None:
        masked = environment_for_display(
            {
                "DATABASE_PASSWORD": "hunter2",
                "API_TOKEN": "abc",
                "MY_SECRET": "s",
                "GITHUB_API_KEY": "k",
                "PORT": "3000",
            }
        )
        assert masked["DATABASE_PASSWORD"] == "***"
        assert masked["API_TOKEN"] == "***"
        assert masked["MY_SECRET"] == "***"
        assert masked["GITHUB_API_KEY"] == "***"
        assert masked["PORT"] == "3000"

    def test_extra_names_can_be_masked_explicitly(self) -> None:
        masked = environment_for_display({"LICENCE_KEY_FILE": "x"}, secrets=["licence_key_file"])
        assert masked["LICENCE_KEY_FILE"] == "***"
