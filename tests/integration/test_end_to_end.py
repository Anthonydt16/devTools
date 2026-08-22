"""The scenario the whole project exists for, start to finish.

    install fake runtimes -> read devbox.yml -> resolve devbox.lock ->
    start shared infrastructure -> create and initialise a database ->
    start two services on two different Node versions -> wait for healthchecks ->
    ps -> logs -> exec -> down

Every step runs for real: real archives with real SHA256 digests, real child
processes, real ports, real HTTP healthchecks, real log files.  Nothing here is
a mock.  What is *simulated* is Node and PostgreSQL themselves — replaced by the
fake runtime — which is what lets this run on a machine with neither installed
and with no network access.
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.conftest import minimal_child_environment

from devbox.config.loader import load_config
from devbox.errors import ServiceStartError
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import Platform
from devbox.lockfile.io import read_lockfile, write_lockfile
from devbox.logs.reader import LogStream
from devbox.process.factory import create_supervisor
from devbox.process.runner import SubprocessRunner
from devbox.process.supervisor import ProcessState
from devbox.runtime.registry import FilesystemRegistry
from devbox.runtime.resolver import resolve_lockfile
from devbox.services.events import EventCollector, EventKind
from devbox.services.orchestrator import Orchestrator
from devbox.services.project import ProjectContext
from devbox.testing.fake_provider import install_fake_provider, uninstall_fake_provider
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate: Callable[[], bool], *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def fake_provider() -> Iterator[None]:
    install_fake_provider(python_executable=sys.executable)
    try:
        yield
    finally:
        uninstall_fake_provider()


@pytest.fixture
def ports() -> dict[str, int]:
    return {
        "database": free_tcp_port(),
        "legacy": free_tcp_port(),
        "modern": free_tcp_port(),
    }


@pytest.fixture
def registry(tmp_path: Path, host_platform: Platform) -> FakeRegistry:
    """Publishes both Node versions and the fake database server."""
    return build_fake_registry(
        tmp_path / "registry",
        [("node", "18.19.0"), ("node", "24.6.0"), ("fakedb", "1.0")],
        platform=host_platform.tag,
        extra_executables=("npm",),
    )


@pytest.fixture
def project(tmp_path: Path, ports: dict[str, int]) -> Path:
    """A project laid out like the real thing, with spaces in its path."""
    root = tmp_path / "My Projects" / "customer portal"
    (root / "legacy-api").mkdir(parents=True)
    (root / "modern-api").mkdir(parents=True)

    runtime = "${env.DEVBOX_TEST_RUNTIME}"
    (root / "devbox.yml").write_text(
        f"""
version: 1
project: customer-portal

infrastructure:
  company-db:
    provider: fakedb
    version: "1.0"
    mode: managed
    port: {ports["database"]}

databases:
  clients:
    provider: fakedb
    infrastructure: company-db
    database: clients
    init:
      if_missing:
        command: {sys.executable} init_db.py
        working_dir: legacy-api

services:
  legacy-api:
    runtime: {{ node: "18.19.0" }}
    working_dir: ./legacy-api
    command: >-
      {sys.executable} {runtime}/node/18.19.0/runtime_main.py serve
      --port {ports["legacy"]} --max-seconds 300
    env:
      DATABASE_URL: ${{database.clients.url}}
    depends_on: [company-db, clients]
    healthcheck:
      http:
        url: "http://127.0.0.1:{ports["legacy"]}/health"
        interval: 0.1
        timeout: 2
        retries: 100

  modern-api:
    runtime: {{ node: "24.6.0" }}
    working_dir: ./modern-api
    command: >-
      {sys.executable} {runtime}/node/24.6.0/runtime_main.py serve
      --port {ports["modern"]} --max-seconds 300
    depends_on: [legacy-api]
    healthcheck:
      tcp:
        host: 127.0.0.1
        port: {ports["modern"]}
        interval: 0.1
        timeout: 2
        retries: 100
""".lstrip(),
        encoding="utf-8",
    )
    (root / "legacy-api" / "init_db.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path('initialised.txt').write_text(os.environ['DATABASE_URL'])\n",
        encoding="utf-8",
    )
    return root


def make_orchestrator(
    project: Path,
    devbox_home: DevboxPaths,
    registry: FakeRegistry,
    host_platform: Platform,
    *,
    events: EventCollector | None = None,
) -> tuple[Orchestrator, EventCollector]:
    """Build the real thing: real supervisor, real runner, real state store."""
    collector = events or EventCollector()
    config = load_config(project / "devbox.yml")
    context = ProjectContext(
        config=config,
        project_root=project,
        paths=devbox_home,
        platform=host_platform,
        environ=minimal_child_environment(
            # Lets devbox.yml point at the installed runtime without hardcoding
            # a machine-specific path in the fixture.
            DEVBOX_TEST_RUNTIME=str(devbox_home.runtimes),
        ),
        supervisor=create_supervisor(),
        runner=SubprocessRunner(),
    )
    return (
        Orchestrator(
            context=context,
            registry=FilesystemRegistry(registry.root),
            on_event=collector,
            sleep=time.sleep,
        ),
        collector,
    )


@pytest.fixture
def orchestrator(
    project: Path, devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
) -> Iterator[Orchestrator]:
    instance, _ = make_orchestrator(project, devbox_home, registry, host_platform)
    lock = resolve_lockfile(
        instance.config, registry=FilesystemRegistry(registry.root), platform=host_platform
    )
    write_lockfile(project / "devbox.lock", lock.lockfile)
    try:
        yield instance
    finally:
        try:
            instance.down()
        except Exception:
            pass


class TestFullScenario:
    def test_the_whole_project_comes_up_and_goes_down(
        self, orchestrator: Orchestrator, project: Path, ports: dict[str, int]
    ) -> None:
        report = orchestrator.up()

        assert report.healthy, "every service should be healthy"
        assert sorted(report.started_services) == ["legacy-api", "modern-api"]
        assert [i.name for i in report.infrastructure] == ["company-db"]
        assert [d.name for d in report.databases] == ["clients"]

        assert port_open(ports["database"]), "the shared server never bound its port"
        assert port_open(ports["legacy"])
        assert port_open(ports["modern"])

        down = orchestrator.down()

        assert not down.refused
        assert wait_until(lambda: not port_open(ports["legacy"]))
        assert wait_until(lambda: not port_open(ports["modern"]))

    def test_runtimes_are_installed_from_the_registry(
        self, orchestrator: Orchestrator, devbox_home: DevboxPaths
    ) -> None:
        orchestrator.up()
        assert (devbox_home.runtimes / "node" / "18.19.0" / "runtime_main.py").is_file()
        assert (devbox_home.runtimes / "node" / "24.6.0" / "runtime_main.py").is_file()

    def test_the_database_is_created_and_initialised_exactly_once(
        self, orchestrator: Orchestrator, project: Path
    ) -> None:
        """Non-regression #3, through the whole stack."""
        first = orchestrator.up()
        assert first.databases[0].created
        assert first.databases[0].initialised

        marker = project / "legacy-api" / "initialised.txt"
        assert marker.is_file(), "the init command should have run"
        assert marker.read_text(encoding="utf-8").startswith("fakedb://")

        marker.unlink()
        orchestrator.down()
        second = orchestrator.up()

        assert second.databases[0].existed
        assert not second.databases[0].created
        assert not second.databases[0].initialised
        assert not marker.exists(), "an existing database must never be re-initialised"

    def test_services_start_in_dependency_order(self, orchestrator: Orchestrator) -> None:
        collector = EventCollector()
        orchestrator.on_event = collector
        orchestrator.up()

        order = [
            event.name
            for event in collector.events
            if event.kind is EventKind.SUCCESS
            and event.name in {"company-db", "clients", "legacy-api", "modern-api"}
        ]
        assert order.index("company-db") < order.index("clients")
        assert order.index("clients") < order.index("legacy-api")
        assert order.index("legacy-api") < order.index("modern-api")


class TestRuntimeIsolation:
    """Non-regression #1, proven by running the binaries."""

    def test_two_services_get_two_different_node_versions_at_once(
        self, orchestrator: Orchestrator
    ) -> None:
        orchestrator.up()

        legacy_argv, legacy_cwd, legacy_env = orchestrator.exec_argv(
            "legacy-api", ["node", "--version"]
        )
        modern_argv, modern_cwd, modern_env = orchestrator.exec_argv(
            "modern-api", ["node", "--version"]
        )

        assert legacy_env["PATH"] != modern_env["PATH"]
        assert "18.19.0" in legacy_argv[0]
        assert "24.6.0" in modern_argv[0]

        legacy = SubprocessRunner().run(legacy_argv, cwd=legacy_cwd, env=legacy_env)
        modern = SubprocessRunner().run(modern_argv, cwd=modern_cwd, env=modern_env)

        assert legacy.stdout.strip() == "v18.19.0"
        assert modern.stdout.strip() == "v24.6.0"

    def test_both_versions_are_resolvable_while_both_services_run(
        self, orchestrator: Orchestrator, ports: dict[str, int]
    ) -> None:
        orchestrator.up()
        assert port_open(ports["legacy"]) and port_open(ports["modern"])

        legacy = orchestrator.build_service_environment(orchestrator.config.service("legacy-api"))
        modern = orchestrator.build_service_environment(orchestrator.config.service("modern-api"))

        assert legacy.resolve_executable("node") != modern.resolve_executable("node")
        assert "18.19.0" in (legacy.resolve_executable("node") or "")
        assert "24.6.0" in (modern.resolve_executable("node") or "")

    def test_run_uses_an_explicit_version_with_no_service_involved(
        self, orchestrator: Orchestrator
    ) -> None:
        orchestrator.up()
        argv, cwd, env = orchestrator.run_argv({"node": "24.6.0"}, ["node", "--version"])
        result = SubprocessRunner().run(argv, cwd=cwd, env=env)
        assert result.stdout.strip() == "v24.6.0"


class TestInspection:
    def test_ps_reports_running_services_with_their_pids(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        rows = {view.name: view for view in orchestrator.ps()}
        assert rows["legacy-api"].state is ProcessState.RUNNING
        assert rows["legacy-api"].pid is not None
        assert rows["legacy-api"].runtime == "node 18.19.0"
        assert rows["modern-api"].runtime == "node 24.6.0"

    def test_ps_after_down_reports_everything_stopped(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        orchestrator.down()
        assert all(not view.running for view in orchestrator.ps())

    def test_logs_capture_stdout_and_stderr_separately(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        reader = orchestrator.log_reader("legacy-api")
        out = [line.text for line in reader.read(streams=[LogStream.STDOUT])]
        err = [line.text for line in reader.read(streams=[LogStream.STDERR])]
        assert any("listening on" in line for line in out)
        assert any("diagnostics on stderr" in line for line in err)

    def test_status_reports_the_whole_picture(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        status = orchestrator.status()
        assert status.project == "customer-portal"
        assert status.environment == "local"
        installed = {(name, version): ok for name, version, ok in status.runtimes}
        assert installed == {
            ("node", "18.19.0"): True,
            ("node", "24.6.0"): True,
            # The managed server's own runtime counts too: `devbox install` has
            # to fetch the database binaries, not just Node.
            ("fakedb", "1.0"): True,
        }
        assert [i.name for i in status.infrastructure] == ["company-db"]
        assert all(view.running for view in status.services)


class TestEnvironmentInjection:
    def test_the_database_url_is_interpolated_into_the_service_environment(
        self, orchestrator: Orchestrator, ports: dict[str, int]
    ) -> None:
        orchestrator.up()
        environment = orchestrator.build_service_environment(
            orchestrator.config.service("legacy-api"),
            databases=(),
        )
        url = environment.variables["DATABASE_URL"]
        assert url.startswith("fakedb://")
        assert str(ports["database"]) in url
        assert url.endswith("/clients")

    def test_devbox_identifies_itself_to_the_child(self, orchestrator: Orchestrator) -> None:
        environment = orchestrator.build_service_environment(
            orchestrator.config.service("legacy-api")
        )
        assert environment.variables["DEVBOX_PROJECT"] == "customer-portal"
        assert environment.variables["DEVBOX_SERVICE"] == "legacy-api"
        assert environment.variables["DEVBOX_ENVIRONMENT"] == "local"


class TestIdempotence:
    def test_up_twice_is_a_no_op_for_what_is_already_running(
        self, orchestrator: Orchestrator
    ) -> None:
        first = orchestrator.up()
        pids = {view.name: view.pid for view in first.services}
        second = orchestrator.up()
        assert {view.name: view.pid for view in second.services} == pids

    def test_down_twice_is_not_an_error(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        orchestrator.down()
        report = orchestrator.down()
        assert not report.refused

    def test_down_without_ever_starting_is_not_an_error(self, orchestrator: Orchestrator) -> None:
        assert not orchestrator.down().refused


class TestFailures:
    def test_a_service_that_exits_immediately_reports_its_stderr(
        self,
        project: Path,
        devbox_home: DevboxPaths,
        registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        (project / "devbox.yml").write_text(
            f"""
version: 1
project: broken
services:
  api:
    runtime: {{ node: "18.19.0" }}
    working_dir: .
    command: >-
      {sys.executable} ${{env.DEVBOX_TEST_RUNTIME}}/node/18.19.0/runtime_main.py
      fail --code 3 --message "cannot connect to the database"
    healthcheck:
      tcp: {{ host: 127.0.0.1, port: 1, interval: 0.05, timeout: 1, retries: 50 }}
""".lstrip(),
            encoding="utf-8",
        )
        orchestrator, _ = make_orchestrator(project, devbox_home, registry, host_platform)
        lock = resolve_lockfile(
            orchestrator.config,
            registry=FilesystemRegistry(registry.root),
            platform=host_platform,
        )
        write_lockfile(project / "devbox.lock", lock.lockfile)

        with pytest.raises(ServiceStartError) as caught:
            orchestrator.up()

        error = caught.value
        assert error.service == "api"
        assert error.details is not None
        assert error.details.exit_code == 3
        assert "cannot connect to the database" in error.details.stderr_tail
        assert error.details.stderr_log

    def test_a_failed_up_leaves_nothing_running(
        self,
        project: Path,
        devbox_home: DevboxPaths,
        registry: FakeRegistry,
        host_platform: Platform,
        ports: dict[str, int],
    ) -> None:
        """A half-started project is worse than a failed one."""
        (project / "devbox.yml").write_text(
            f"""
version: 1
project: partial
services:
  good:
    runtime: {{ node: "18.19.0" }}
    working_dir: .
    command: >-
      {sys.executable} ${{env.DEVBOX_TEST_RUNTIME}}/node/18.19.0/runtime_main.py serve
      --port {ports["legacy"]} --max-seconds 60
    healthcheck:
      tcp: {{ host: 127.0.0.1, port: {ports["legacy"]}, interval: 0.05, timeout: 1, retries: 100 }}
  bad:
    runtime: {{ node: "18.19.0" }}
    working_dir: .
    command: >-
      {sys.executable} ${{env.DEVBOX_TEST_RUNTIME}}/node/18.19.0/runtime_main.py
      fail --code 1 --message boom
    depends_on: [good]
    healthcheck:
      tcp: {{ host: 127.0.0.1, port: 1, interval: 0.05, timeout: 1, retries: 20 }}
""".lstrip(),
            encoding="utf-8",
        )
        orchestrator, _ = make_orchestrator(project, devbox_home, registry, host_platform)
        lock = resolve_lockfile(
            orchestrator.config,
            registry=FilesystemRegistry(registry.root),
            platform=host_platform,
        )
        write_lockfile(project / "devbox.lock", lock.lockfile)

        with pytest.raises(ServiceStartError):
            orchestrator.up()

        assert wait_until(lambda: not port_open(ports["legacy"])), (
            "the service started by the failed run should have been stopped"
        )
        assert all(not view.running for view in orchestrator.ps())

    def test_a_lockfile_that_does_not_cover_the_project_fails_before_starting(
        self,
        project: Path,
        devbox_home: DevboxPaths,
        registry: FakeRegistry,
        host_platform: Platform,
        ports: dict[str, int],
    ) -> None:
        from devbox.errors import LockfileError

        orchestrator, _ = make_orchestrator(project, devbox_home, registry, host_platform)
        (project / "devbox.lock").write_text("lock_version: 1\nruntimes: {}\n", encoding="utf-8")

        with pytest.raises(LockfileError) as caught:
            orchestrator.up()

        assert "does not cover this project" in str(caught.value)
        assert not port_open(ports["database"]), "nothing may start before validation passes"


class TestSharedInfrastructure:
    """Non-regression #2, through the orchestrator."""

    def test_a_second_project_reuses_the_running_server(
        self,
        project: Path,
        tmp_path: Path,
        devbox_home: DevboxPaths,
        registry: FakeRegistry,
        host_platform: Platform,
        ports: dict[str, int],
    ) -> None:
        first, _ = make_orchestrator(project, devbox_home, registry, host_platform)
        lock = resolve_lockfile(
            first.config, registry=FilesystemRegistry(registry.root), platform=host_platform
        )
        write_lockfile(project / "devbox.lock", lock.lockfile)
        first.up()

        second_root = tmp_path / "other project"
        second_root.mkdir(parents=True)
        (second_root / "devbox.yml").write_text(
            (project / "devbox.yml")
            .read_text(encoding="utf-8")
            .replace("project: customer-portal", "project: reporting"),
            encoding="utf-8",
        )
        (second_root / "legacy-api").mkdir()
        (second_root / "modern-api").mkdir()
        (second_root / "legacy-api" / "init_db.py").write_text("pass\n", encoding="utf-8")
        write_lockfile(second_root / "devbox.lock", read_lockfile(project / "devbox.lock"))

        second, _ = make_orchestrator(second_root, devbox_home, registry, host_platform)
        report = second.up(["company-db"])
        assert report.infrastructure[0].already_running

        try:
            down = second.down(["company-db"])
            assert down.infrastructure[0].refused, (
                "one project's down must never stop a server another project uses"
            )
            assert port_open(ports["database"]), "the shared server must still be up"
        finally:
            second.down()
            first.down()
