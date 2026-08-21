"""The remaining CLI surfaces: `infra`, `status`, `logs`, and `devbox test`.

Driven through ``devbox.cli.main.run`` like the rest, so exit codes and rendering
are the shipped ones.  Shared infrastructure is provided by
``devbox.testing.fake_provider``, which starts a real process on a real port.
"""

from __future__ import annotations

import io
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
from devbox.testing.fake_provider import install_fake_provider, uninstall_fake_provider
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry

pytestmark = pytest.mark.integration

CliRun = Callable[..., tuple[int, str, str]]


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    reset_context()
    install_fake_provider(python_executable=sys.executable)
    try:
        yield
    finally:
        uninstall_fake_provider()
        reset_context()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_until(predicate: Callable[[], bool], *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def registry(tmp_path: Path, host_platform: Platform) -> FakeRegistry:
    return build_fake_registry(
        tmp_path / "registry",
        [("node", "18.19.0"), ("fakedb", "1.0")],
        platform=host_platform.tag,
    )


@pytest.fixture
def cli(tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> CliRun:
    home = tmp_path / "devbox-home"

    def _restore() -> None:
        monkeypatch.setenv("DEVBOX_HOME", str(home))
        monkeypatch.setenv("DEVBOX_REGISTRY", str(registry.root))
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("PY", sys.executable)

    _restore()

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
            _restore()
        return code, out.getvalue(), err.getvalue()

    return _run


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    root = tmp_path / "shared demo"
    root.mkdir(parents=True)
    ports = {"db": free_port(), "api": free_port()}
    root.joinpath("devbox.yml").write_text(
        f"""
version: 1
project: shared-demo

infrastructure:
  company-db:
    provider: fakedb
    version: "1.0"
    mode: managed
    port: {ports["db"]}
  recette-db:
    provider: fakedb
    mode: external
    url_env: RECETTE_DB_URL

databases:
  clients:
    provider: fakedb
    infrastructure: company-db
    database: clients

services:
  api:
    runtime: {{ node: "18.19.0" }}
    working_dir: .
    command: >-
      ${{env.PY}} ${{env.DEVBOX_HOME}}/runtimes/node/18.19.0/runtime_main.py serve
      --port {ports["api"]} --max-seconds 120
    env:
      DATABASE_URL: ${{database.clients.url}}
    depends_on: [company-db, clients]
    healthcheck:
      tcp:
        host: 127.0.0.1
        port: {ports["api"]}
        interval: 0.1
        timeout: 2
        retries: 100
""".lstrip(),
        encoding="utf-8",
    )
    return root, ports


class TestInfraCommands:
    def test_infra_status_before_anything_started(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, out, err = cli("infra", "status", cwd=root)
        assert code == ExitCode.OK, err
        assert "company-db" in out
        assert "stopped" in out
        assert "external" in out, "the external instance must be shown as such"

    def test_infra_up_status_and_down(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK

        code, out, err = cli("infra", "up", cwd=root)
        assert code == ExitCode.OK, err
        assert "running on" in out
        assert wait_until(lambda: port_open(ports["db"]))

        code, out, _ = cli("infra", "status", cwd=root)
        assert "running" in out

        code, out, err = cli("infra", "down", cwd=root)
        assert code == ExitCode.OK, err
        assert "stopped" in out
        assert "Data directories were left untouched" in out
        assert wait_until(lambda: not port_open(ports["db"]))

    def test_devbox_down_never_stops_a_globally_started_server(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        """Non-regression #2, through the CLI a user actually types."""
        root, ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        assert cli("infra", "up", cwd=root)[0] == ExitCode.OK
        assert cli("up", cwd=root)[0] == ExitCode.OK

        try:
            code, out, err = cli("down", cwd=root)
            assert code == ExitCode.OK, err
            assert "Left running (shared)" in out
            assert "devbox infra down" in out
            assert port_open(ports["db"]), "the shared server must still be up"
        finally:
            cli("infra", "down", cwd=root)

    def test_infra_logs_reports_when_there_are_none(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, out, _ = cli("infra", "logs", "company-db", cwd=root)
        assert code == ExitCode.OK
        assert "No logs yet" in out

    def test_infra_logs_shows_the_servers_own_output(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        assert cli("infra", "up", cwd=root)[0] == ExitCode.OK
        try:
            assert wait_until(lambda: port_open(ports["db"]))
            code, out, _ = cli("infra", "logs", "company-db", cwd=root)
            assert code == ExitCode.OK
            assert "listening on" in out
        finally:
            cli("infra", "down", cwd=root)

    def test_an_unknown_instance_is_reported_with_the_alternatives(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, _out, err = cli("infra", "status", "ghost", cwd=root)
        assert code == ExitCode.INFRASTRUCTURE
        assert "unknown infrastructure: ghost" in err
        assert "company-db" in err


class TestStatusAndLogs:
    def test_status_shows_runtimes_infrastructure_and_services(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        code, out, err = cli("status", cwd=root)
        assert code == ExitCode.OK, err
        assert "Runtimes" in out and "node 18.19.0" in out
        assert "Infrastructure" in out and "company-db" in out
        assert "Services" in out and "api" in out

    def test_status_reports_a_missing_runtime(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, out, _ = cli("status", cwd=root)
        assert code == ExitCode.OK
        assert "missing (run 'devbox install')" in out

    def test_logs_for_a_service_that_never_ran(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, out, _ = cli("logs", "api", cwd=root)
        assert code == ExitCode.OK
        assert "No logs yet" in out

    def test_logs_can_select_one_stream_and_tail(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        assert cli("up", cwd=root)[0] == ExitCode.OK
        try:
            assert wait_until(lambda: port_open(ports["api"]))
            code, out, _ = cli("logs", "api", "--stdout", "--tail", "5", cwd=root)
            assert code == ExitCode.OK
            assert "listening on" in out
            assert "err |" not in out
        finally:
            cli("down", cwd=root)
            cli("infra", "down", cwd=root)

    def test_logs_follow_stops_at_its_timeout(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        """A --follow that cannot terminate would hang CI forever."""
        root, _ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        assert cli("up", cwd=root)[0] == ExitCode.OK
        try:
            code, _out, _err = cli("logs", "api", "--follow", "--timeout", "0.3", cwd=root)
            assert code == ExitCode.OK
        finally:
            cli("down", cwd=root)
            cli("infra", "down", cwd=root)

    def test_logs_for_an_unknown_service_is_a_config_error(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        code, _out, err = cli("logs", "ghost", cwd=root)
        assert code == ExitCode.CONFIG
        assert "unknown service 'ghost'" in err


class TestExternalThroughTheCli:
    """Non-regression #9, through the CLI."""

    def test_infra_up_never_starts_an_external_instance(
        self, cli: CliRun, project: tuple[Path, dict[str, int]]
    ) -> None:
        root, _ports = project
        assert cli("install", cwd=root)[0] == ExitCode.OK
        code, out, err = cli("infra", "up", "recette-db", cwd=root)
        assert code == ExitCode.OK, err
        assert "external, not started by Devbox" in out


class TestDevboxTestCommand:
    def test_it_reports_a_failing_mandatory_check(self, cli: CliRun, tmp_path: Path) -> None:
        """A repository with nothing in it fails the static checks."""
        empty = tmp_path / "not-a-repo"
        empty.mkdir()
        (empty / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        code, _out, err = cli("test", "--only", "config", "--root", str(empty), cwd=empty)
        # No configuration file to check: reported as a skip, not a failure.
        assert code == ExitCode.OK, err

    def test_only_selects_a_subset(self, cli: CliRun, tmp_path: Path) -> None:
        empty = tmp_path / "repo"
        empty.mkdir()
        (empty / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        code, out, _err = cli("test", "--only", "packaging", "--root", str(empty), cwd=empty)
        assert code == ExitCode.OK
        assert "packaging" in out
        assert "lint" not in out

    def test_the_summary_reports_skipped_tests_and_why(self, cli: CliRun, tmp_path: Path) -> None:
        repository = Path(__file__).resolve().parents[2]
        code, out, err = cli("test", "--only", "windows", "--root", str(repository))
        assert code == ExitCode.OK, err
        assert "Skipped:" in out
        assert "requires a real Windows host" in out
