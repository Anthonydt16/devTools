"""The CLI, driven the way a user and a CI job drive it.

Everything goes through ``devbox.cli.main.run``, which is what the console script
calls, so exit codes, error rendering and non-interactive behaviour are exercised
exactly as shipped.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from devbox.cli import main as cli_main
from devbox.cli.main_state import reset_context
from devbox.errors import ExitCode
from devbox.host.platform_info import Platform
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_cli_state() -> Iterator[None]:
    reset_context()
    yield
    reset_context()


@pytest.fixture
def registry(tmp_path: Path, host_platform: Platform) -> FakeRegistry:
    return build_fake_registry(
        tmp_path / "registry",
        [("node", "18.19.0"), ("node", "24.6.0")],
        platform=host_platform.tag,
        extra_executables=("npm",),
    )


@pytest.fixture
def cli(
    tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[int, str, str]]:
    """Run the CLI in a project directory, capturing stdout, stderr and the code."""
    home = tmp_path / "devbox-home"
    monkeypatch.setenv("DEVBOX_HOME", str(home))
    monkeypatch.setenv("DEVBOX_REGISTRY", str(registry.root))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("PY", sys.executable)

    def _run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        if cwd is not None:
            monkeypatch.chdir(cwd)
        try:
            code = cli_main.run(list(args))
        finally:
            monkeypatch.undo()
            monkeypatch.setenv("DEVBOX_HOME", str(home))
            monkeypatch.setenv("DEVBOX_REGISTRY", str(registry.root))
            monkeypatch.setenv("NO_COLOR", "1")
            monkeypatch.setenv("PY", sys.executable)
        return code, out.getvalue(), err.getvalue()

    return _run


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, int]:
    """A project whose single service is a real, health-checkable process."""
    root = tmp_path / "My Projects" / "demo portal"
    (root / "api").mkdir(parents=True)
    port = free_port()
    root.joinpath("devbox.yml").write_text(
        f"""
version: 1
project: demo-portal

services:
  api:
    runtime: {{ node: "18.19.0" }}
    working_dir: ./api
    command: >-
      ${{env.PY}} ${{env.DEVBOX_HOME}}/runtimes/node/18.19.0/runtime_main.py serve
      --port {port} --max-seconds 120
    healthcheck:
      http:
        url: "http://127.0.0.1:{port}/health"
        interval: 0.1
        timeout: 3
        retries: 100
""".lstrip(),
        encoding="utf-8",
    )
    return root, port


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_until(predicate: Callable[[], bool], *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestBasics:
    def test_version(self, cli: Callable[..., tuple[int, str, str]]) -> None:
        code, out, _ = cli("version")
        assert code == ExitCode.OK
        assert "Devbox v" in out

    def test_help_exits_zero(self, cli: Callable[..., tuple[int, str, str]]) -> None:
        code, out, _ = cli("--help")
        assert code == ExitCode.OK
        assert "up" in out and "down" in out

    def test_an_unknown_command_is_a_usage_error(
        self, cli: Callable[..., tuple[int, str, str]]
    ) -> None:
        code, _out, _err = cli("teleport")
        assert code == ExitCode.USAGE

    def test_an_unknown_option_is_a_usage_error(
        self, cli: Callable[..., tuple[int, str, str]]
    ) -> None:
        code, _out, _err = cli("--nonsense", "version")
        assert code == ExitCode.USAGE

    def test_a_missing_config_is_a_config_error_with_a_hint(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        code, _out, err = cli("ps", cwd=empty)
        assert code == ExitCode.CONFIG
        assert "no devbox.yml found" in err
        assert "devbox init" in err
        assert "Traceback" not in err, "the user must never see a traceback"


class TestInit:
    def test_init_writes_a_valid_configuration(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        directory = tmp_path / "fresh project"
        directory.mkdir()
        code, out, _ = cli("init", cwd=directory)
        assert code == ExitCode.OK
        assert (directory / "devbox.yml").is_file()
        assert "Next steps" in out

        from devbox.config.loader import load_config

        config = load_config(directory / "devbox.yml")
        assert config.project == "fresh project"

    def test_init_refuses_to_overwrite_without_force(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        directory = tmp_path / "twice"
        directory.mkdir()
        cli("init", cwd=directory)
        code, _out, err = cli("init", cwd=directory)
        assert code == ExitCode.CONFIG
        assert "--force" in err

    def test_init_force_overwrites(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        directory = tmp_path / "forced"
        directory.mkdir()
        cli("init", cwd=directory)
        code, _out, _err = cli("init", "--force", cwd=directory)
        assert code == ExitCode.OK


class TestLifecycle:
    def test_the_documented_flow_works_end_to_end(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, port = project

        code, out, err = cli("install", cwd=root)
        assert code == ExitCode.OK, err
        assert (root / "devbox.lock").is_file()

        code, out, err = cli("up", cwd=root)
        assert code == ExitCode.OK, err
        assert "All services healthy" in out
        assert port_open(port)

        code, out, _ = cli("ps", cwd=root)
        assert code == ExitCode.OK
        assert "api" in out and "running" in out

        code, out, _ = cli("logs", "api", cwd=root)
        assert code == ExitCode.OK
        assert "listening on" in out

        code, out, _ = cli("exec", "api", "node", "--version", cwd=root)
        assert code == ExitCode.OK
        assert out.strip() == "v18.19.0"

        code, out, err = cli("down", cwd=root)
        assert code == ExitCode.OK, err
        assert wait_until(lambda: not port_open(port))

        code, out, _ = cli("ps", cwd=root)
        assert "running" not in out

    def test_up_without_install_reports_the_missing_lockfile(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, _port = project
        code, _out, err = cli("up", cwd=root)
        assert code == ExitCode.LOCKFILE
        assert "devbox.lock does not cover this project" in err
        assert "devbox install" in err

    def test_install_check_fails_when_the_lockfile_is_stale(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        """The flag a CI job uses to enforce a committed lockfile."""
        root, _port = project
        code, _out, err = cli("install", "--check", cwd=root)
        assert code == ExitCode.LOCKFILE
        assert "out of date" in err

        cli("install", cwd=root)
        code, _out, _err = cli("install", "--check", cwd=root)
        assert code == ExitCode.OK

    def test_install_lock_only_writes_the_lockfile_without_installing(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int], tmp_path: Path
    ) -> None:
        root, _port = project
        code, _out, _err = cli("install", "--lock-only", cwd=root)
        assert code == ExitCode.OK
        assert (root / "devbox.lock").is_file()
        assert not (tmp_path / "devbox-home" / "runtimes" / "node").exists()

    def test_down_before_up_is_not_an_error(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, _port = project
        code, out, _err = cli("down", cwd=root)
        assert code == ExitCode.OK
        assert "Nothing was running" in out or "api" in out

    def test_a_path_containing_spaces_and_accents_works_throughout(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        """Windows quoting bugs live in exactly this scenario."""
        root, _port = project
        assert " " in str(root)
        cli("install", cwd=root)
        code, out, err = cli("up", cwd=root)
        try:
            assert code == ExitCode.OK, err
            assert "All services healthy" in out
        finally:
            cli("down", cwd=root)


class TestFailureRendering:
    def test_a_service_that_fails_produces_the_documented_block(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        root = tmp_path / "broken"
        root.mkdir()
        root.joinpath("devbox.yml").write_text(
            """
version: 1
project: broken
services:
  backend:
    runtime: { node: "18.19.0" }
    working_dir: .
    command: >-
      ${env.PY} ${env.DEVBOX_HOME}/runtimes/node/18.19.0/runtime_main.py
      fail --code 1 --message "Error: connect ECONNREFUSED 127.0.0.1:5432"
    healthcheck:
      tcp: { host: 127.0.0.1, port: 1, interval: 0.05, timeout: 1, retries: 20 }
""".lstrip(),
            encoding="utf-8",
        )
        cli("install", cwd=root)
        code, _out, err = cli("up", cwd=root)

        assert code == ExitCode.SERVICE
        assert "backend failed to start" in err
        assert "Command:" in err
        assert "Exit code:" in err
        assert "ECONNREFUSED" in err
        assert "Logs:" in err
        assert "Traceback" not in err

    def test_debug_adds_a_traceback_for_us(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        empty = tmp_path / "no-project"
        empty.mkdir()
        code, _out, err = cli("--debug", "ps", cwd=empty)
        assert code == ExitCode.CONFIG
        assert "Traceback" in err

    def test_an_invalid_configuration_lists_every_problem(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        root = tmp_path / "invalid"
        root.mkdir()
        root.joinpath("devbox.yml").write_text(
            "version: 1\nservices:\n  a:\n    command: 3\n    depend_on: []\n",
            encoding="utf-8",
        )
        code, _out, err = cli("ps", cwd=root)
        assert code == ExitCode.CONFIG
        assert "expected a string" in err
        assert "depend_on" in err


class TestRuntimeCommands:
    def test_runtime_list_is_empty_before_installing(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        code, out, _ = cli("runtime", "list", cwd=tmp_path)
        assert code == ExitCode.OK
        assert "No runtimes are installed" in out

    def test_runtime_install_list_and_remove(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        code, out, err = cli("runtime", "install", "node", "24.6.0", cwd=tmp_path)
        assert code == ExitCode.OK, err
        assert "installed" in out

        code, out, _ = cli("runtime", "list", cwd=tmp_path)
        assert "24.6.0" in out and "ready" in out

        code, out, _ = cli("runtime", "remove", "node", "24.6.0", cwd=tmp_path)
        assert code == ExitCode.OK
        assert "removed" in out

    def test_removing_a_runtime_that_is_not_installed_is_an_error(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        code, _out, err = cli("runtime", "remove", "node", "99.0.0", cwd=tmp_path)
        assert code == ExitCode.REGISTRY
        assert "not installed" in err

    def test_run_needs_the_runtime_installed(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, _port = project
        code, _out, err = cli("run", "--node", "24.6.0", "node", "--version", cwd=root)
        assert code == ExitCode.REGISTRY
        assert "not installed" in err

    def test_run_executes_under_the_chosen_version(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, _port = project
        cli("runtime", "install", "node", "24.6.0", cwd=root)
        code, out, err = cli("run", "--node", "24.6.0", "node", "--version", cwd=root)
        assert code == ExitCode.OK, err
        assert out.strip() == "v24.6.0"


class TestBuildCommand:
    def test_build_produces_an_artifact_and_a_manifest(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        root, _port = project
        cli("install", cwd=root)
        code, out, err = cli("build", "--version", "1.4.2", cwd=root)
        assert code == ExitCode.OK, err

        artifact = root / "dist" / "demo-portal-1.4.2.zip"
        manifest = root / "dist" / "demo-portal-1.4.2.manifest.json"
        assert artifact.is_file()
        assert json.loads(manifest.read_text())["version"] == "1.4.2"
        assert "sha256" in out


class TestNonInteractive:
    def test_quiet_suppresses_progress_but_not_errors(
        self, cli: Callable[..., tuple[int, str, str]], tmp_path: Path
    ) -> None:
        empty = tmp_path / "quiet"
        empty.mkdir()
        code, out, err = cli("--quiet", "ps", cwd=empty)
        assert code == ExitCode.CONFIG
        assert out == "", "quiet mode should not print progress"
        assert err, "an error must still be reported"

    def test_nothing_ever_reads_from_stdin(
        self, cli: Callable[..., tuple[int, str, str]], project: tuple[Path, int]
    ) -> None:
        """A command that blocks on stdin hangs CI forever."""
        root, _port = project
        import sys as _sys

        class Exploding(io.StringIO):
            def read(self, *args: object, **kwargs: object) -> str:
                raise AssertionError("a command read from stdin")

            def readline(self, *args: object, **kwargs: object) -> str:
                raise AssertionError("a command read from stdin")

        original = _sys.stdin
        _sys.stdin = Exploding()
        try:
            for arguments in (("version",), ("install",), ("ps",), ("status",)):
                code, _out, _err = cli(*arguments, cwd=root)
                assert code in (ExitCode.OK, ExitCode.LOCKFILE)
        finally:
            _sys.stdin = original
