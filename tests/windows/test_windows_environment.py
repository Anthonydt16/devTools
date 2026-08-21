"""Windows filesystem, PATH and encoding behaviour, on a real Windows host."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from devbox.config.models import RuntimeRequirement
from devbox.host.paths import DevboxPaths, resolve_devbox_home
from devbox.host.platform_info import current_platform
from devbox.logs.reader import decode_log_bytes
from devbox.runtime.environment import build_environment
from devbox.security.command import resolve_executable
from devbox.testing.fake_runtime import build_fake_runtime

pytestmark = pytest.mark.windows


class TestPaths:
    def test_the_devbox_home_lives_under_local_appdata(self) -> None:
        """Zero privileges: a per-user directory, never Program Files."""
        environ = {"LOCALAPPDATA": "C:\\Users\\dev\\AppData\\Local"}
        home = resolve_devbox_home(environ)
        assert home == Path("C:\\Users\\dev\\AppData\\Local\\Devbox")

    def test_the_real_home_is_writable_without_elevation(self, tmp_path: Path) -> None:
        paths = DevboxPaths(root=tmp_path / "Devbox").ensure()
        probe = paths.runtimes / "probe.txt"
        probe.write_text("ok", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "ok"

    def test_nothing_is_written_to_program_files(self, tmp_path: Path) -> None:
        program_files = Path(os.environ.get("PROGRAMFILES", "C:\\Program Files"))
        before = set(program_files.iterdir()) if program_files.is_dir() else set()
        DevboxPaths(root=tmp_path / "Devbox").ensure()
        after = set(program_files.iterdir()) if program_files.is_dir() else set()
        assert after == before

    def test_a_path_with_spaces_and_accents_round_trips(self, tmp_path: Path) -> None:
        directory = tmp_path / "Mes Projets" / "portail café"
        directory.mkdir(parents=True)
        target = directory / "devbox.yml"
        target.write_text("version: 1\n", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "version: 1\n"

    def test_long_paths_are_handled(self, tmp_path: Path) -> None:
        """Windows MAX_PATH is 260 unless long paths are enabled; report, do not crash."""
        deep = tmp_path
        for index in range(12):
            deep = deep / f"level-{index:02d}-{'x' * 15}"
        try:
            deep.mkdir(parents=True)
        except OSError as error:
            pytest.skip(f"long paths are not enabled on this host: {error}")
        target = deep / "file.txt"
        target.write_text("ok", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "ok"


class TestPathConstruction:
    def test_two_runtimes_produce_two_different_paths(self, tmp_path: Path) -> None:
        """Non-regression #1, on the target platform."""
        legacy = build_environment(
            [RuntimeRequirement("node", "18.19.0")],
            runtime_root=tmp_path,
            base_environment=dict(os.environ),
        )
        modern = build_environment(
            [RuntimeRequirement("node", "24.6.0")],
            runtime_root=tmp_path,
            base_environment=dict(os.environ),
        )
        assert legacy.path != modern.path
        assert ";" in legacy.path, "Windows separates PATH entries with semicolons"

    def test_the_machine_path_is_never_modified(self, tmp_path: Path) -> None:
        before = os.environ.get("PATH", "")
        build_environment(
            [RuntimeRequirement("node", "18.19.0")],
            runtime_root=tmp_path,
            base_environment=dict(os.environ),
        )
        assert os.environ.get("PATH", "") == before

    def test_a_cmd_shim_is_resolved_like_npm(self, tmp_path: Path) -> None:
        """npm/yarn/pnpm are .cmd files; CreateProcess needs the extension."""
        runtime = build_fake_runtime(
            tmp_path / "node" / "18.19.0",
            name="node",
            version="18.19.0",
            extra_executables=("npm",),
        )
        found = resolve_executable("npm", path_entries=[str(runtime)], is_windows=True)
        assert found is not None
        assert found.lower().endswith(".cmd")

    def test_the_resolved_executable_actually_runs(self, tmp_path: Path) -> None:
        runtime = build_fake_runtime(tmp_path / "node" / "24.6.0", name="node", version="24.6.0")
        environment = build_environment(
            [RuntimeRequirement("node", "24.6.0")],
            runtime_root=tmp_path,
            base_environment=dict(os.environ),
        )
        executable = environment.resolve_executable("node")
        assert executable is not None
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=environment.as_dict(),
        )
        assert result.stdout.strip() == "v24.6.0", result.stderr
        assert runtime.is_dir()


class TestEncoding:
    """Windows consoles write logs in at least four encodings."""

    def test_cp1252_output_is_decoded(self) -> None:
        assert decode_log_bytes("démarré".encode("cp1252")) == "démarré"

    def test_utf16_redirection_output_is_decoded(self) -> None:
        raw = b"\xff\xfe" + "démarré".encode("utf-16-le")
        assert decode_log_bytes(raw) == "démarré"

    def test_a_child_writing_the_ansi_code_page_is_readable(self, tmp_path: Path) -> None:
        log = tmp_path / "out.log"
        log.write_bytes("service démarré\r\n".encode("cp1252"))
        from devbox.logs.reader import LogReader, LogStream

        reader = LogReader("api", log, tmp_path / "err.log")
        assert [line.text for line in reader.read(streams=[LogStream.STDOUT])] == [
            "service démarré"
        ]


class TestPlatformDetection:
    def test_the_platform_tag_is_windows(self) -> None:
        assert current_platform().is_windows
        assert current_platform().tag.startswith("windows-")

    def test_the_executable_suffix_is_exe(self) -> None:
        assert current_platform().executable_suffix == ".exe"

    def test_the_path_separator_is_a_semicolon(self) -> None:
        assert current_platform().path_separator == ";"


class TestCliOnWindows:
    def test_the_cli_answers_version(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "devbox", "version"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Devbox v" in result.stdout

    def test_the_cli_renders_without_a_unicode_error(self, tmp_path: Path) -> None:
        """A cp1252 console must not make the renderer explode."""
        import io

        from devbox.cli.output import Renderer

        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        renderer = Renderer(stream=stream, error_stream=stream, colour=False)
        renderer.success("démarré")
        renderer.success("日本語")
        stream.flush()


class TestFakeRuntimeOnWindows:
    def test_the_generated_cmd_launcher_runs(self, tmp_path: Path) -> None:
        runtime = build_fake_runtime(tmp_path / "node", name="node", version="18.19.0")
        result = subprocess.run(
            [str(runtime / "node.cmd"), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "v18.19.0"
