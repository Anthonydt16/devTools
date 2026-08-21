"""The shipped example, run for real.

``examples/node-migration/`` is what the README points people at, so it is tested
like production code: the *same* ``devbox.yml``, the same commands, the same
healthchecks — on a machine with neither Node version installed and no network.

The only substitution is the runtime itself: ``server.js.fake`` next to each
``server.js`` tells the fake runtime which port to bind and which path answers
the healthcheck.  Real Node ignores that file, so the example a developer runs
and the example CI runs are the same example.
"""

from __future__ import annotations

import re
import shutil
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from devbox.config.loader import load_config
from devbox.errors import ServiceStartError
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import Platform
from devbox.lockfile.io import write_lockfile
from devbox.logs.reader import LogStream
from devbox.process.factory import create_supervisor
from devbox.process.runner import SubprocessRunner
from devbox.process.supervisor import ProcessState
from devbox.runtime.registry import FilesystemRegistry
from devbox.runtime.resolver import resolve_lockfile
from devbox.services.orchestrator import Orchestrator
from devbox.services.project import ProjectContext
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry

pytestmark = [pytest.mark.integration, pytest.mark.slow]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "node-migration"


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
def example(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    """A copy of the example with its fixed ports swapped for free ones.

    Only the ports change: a suite that binds 3001 fails the moment a developer
    has the real example running.
    """
    root = tmp_path / "node-migration"
    shutil.copytree(EXAMPLE, root)

    ports = {"legacy-api": free_port(), "modern-api": free_port()}
    replacements = {"3001": str(ports["legacy-api"]), "3002": str(ports["modern-api"])}

    for path in (root / "devbox.yml", *root.rglob("server.js.fake")):
        text = path.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = re.sub(rf"\b{old}\b", new, text)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root, ports


@pytest.fixture
def registry(tmp_path: Path, host_platform: Platform) -> FakeRegistry:
    return build_fake_registry(
        tmp_path / "registry",
        [("node", "18.19.0"), ("node", "24.6.0")],
        platform=host_platform.tag,
        extra_executables=("npm",),
    )


@pytest.fixture
def orchestrator(
    example: tuple[Path, dict[str, int]],
    devbox_home: DevboxPaths,
    registry: FakeRegistry,
    host_platform: Platform,
) -> Iterator[Orchestrator]:
    root, _ports = example
    config = load_config(root / "devbox.yml")
    context = ProjectContext(
        config=config,
        project_root=root,
        paths=devbox_home,
        platform=host_platform,
        environ={"PATH": "/usr/bin:/bin"},
        supervisor=create_supervisor(),
        runner=SubprocessRunner(),
    )
    instance = Orchestrator(context=context, registry=FilesystemRegistry(registry.root))
    resolution = resolve_lockfile(
        config, registry=FilesystemRegistry(registry.root), platform=host_platform
    )
    write_lockfile(root / "devbox.lock", resolution.lockfile)
    try:
        yield instance
    finally:
        try:
            instance.down()
        except Exception:
            pass


class TestTheExampleAsShipped:
    def test_the_shipped_configuration_is_valid(self) -> None:
        """The file the README tells people to copy must actually load."""
        config = load_config(EXAMPLE / "devbox.yml")
        assert config.project == "node-migration"
        assert [str(r) for r in config.required_runtimes()] == [
            "node 18.19.0",
            "node 24.6.0",
        ]

    def test_both_services_ship_a_real_server_and_a_fake_directive(self) -> None:
        for service in ("legacy-api", "modern-api"):
            assert (EXAMPLE / service / "server.js").is_file()
            assert (EXAMPLE / service / "server.js.fake").is_file()
            assert (EXAMPLE / service / "package.json").is_file()

    def test_the_real_server_reads_the_port_from_the_environment(self) -> None:
        """The .fake directive and the real server must agree on how ports arrive."""
        import json

        for service in ("legacy-api", "modern-api"):
            source = (EXAMPLE / service / "server.js").read_text(encoding="utf-8")
            directive = json.loads((EXAMPLE / service / "server.js.fake").read_text())
            assert "process.env.PORT" in source
            assert directive["port_env"] == "PORT"

    def test_the_readme_documents_the_commands_it_shows(self) -> None:
        readme = (EXAMPLE / "README.md").read_text(encoding="utf-8")
        for command in ("devbox install", "devbox up", "devbox exec", "devbox down"):
            assert command in readme


class TestRunningTheExample:
    def test_both_services_start_and_become_healthy(
        self, orchestrator: Orchestrator, example: tuple[Path, dict[str, int]]
    ) -> None:
        _root, ports = example
        report = orchestrator.up()

        assert report.healthy
        assert sorted(report.started_services) == ["legacy-api", "modern-api"]
        assert port_open(ports["legacy-api"])
        assert port_open(ports["modern-api"])

    def test_each_service_runs_its_own_node_version(self, orchestrator: Orchestrator) -> None:
        """The reason the example exists."""
        orchestrator.up()
        runner = SubprocessRunner()

        legacy_argv, legacy_cwd, legacy_env = orchestrator.exec_argv(
            "legacy-api", ["node", "--version"]
        )
        modern_argv, modern_cwd, modern_env = orchestrator.exec_argv(
            "modern-api", ["node", "--version"]
        )
        legacy = runner.run(legacy_argv, cwd=legacy_cwd, env=legacy_env)
        modern = runner.run(modern_argv, cwd=modern_cwd, env=modern_env)

        assert legacy.stdout.strip() == "v18.19.0"
        assert modern.stdout.strip() == "v24.6.0"

    def test_the_start_order_follows_depends_on(self, orchestrator: Orchestrator) -> None:
        report = orchestrator.up()
        assert [view.name for view in report.services] == ["legacy-api", "modern-api"]

    def test_ps_shows_both_with_their_runtimes(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        rows = {view.name: view for view in orchestrator.ps()}
        assert rows["legacy-api"].runtime == "node 18.19.0"
        assert rows["modern-api"].runtime == "node 24.6.0"
        assert all(view.state is ProcessState.RUNNING for view in rows.values())

    def test_logs_are_captured_for_both(self, orchestrator: Orchestrator) -> None:
        orchestrator.up()
        for service in ("legacy-api", "modern-api"):
            reader = orchestrator.log_reader(service)
            out = [line.text for line in reader.read(streams=[LogStream.STDOUT])]
            assert any("listening on" in line for line in out), f"{service} logged nothing"

    def test_down_stops_both_and_releases_the_ports(
        self, orchestrator: Orchestrator, example: tuple[Path, dict[str, int]]
    ) -> None:
        _root, ports = example
        orchestrator.up()
        report = orchestrator.down()

        assert not report.refused
        assert wait_until(lambda: not port_open(ports["legacy-api"]))
        assert wait_until(lambda: not port_open(ports["modern-api"]))
        assert all(not view.running for view in orchestrator.ps())

    def test_run_tests_the_same_command_on_both_versions(self, orchestrator: Orchestrator) -> None:
        """The migration workflow: run the suite on 18, then on 24."""
        orchestrator.up()
        runner = SubprocessRunner()
        results = {}
        for version in ("18.19.0", "24.6.0"):
            argv, cwd, env = orchestrator.run_argv({"node": version}, ["node", "--version"])
            results[version] = runner.run(argv, cwd=cwd, env=env).stdout.strip()
        assert results == {"18.19.0": "v18.19.0", "24.6.0": "v24.6.0"}


class TestTheExampleFailsUsefully:
    def test_a_service_whose_script_is_missing_reports_it(
        self,
        example: tuple[Path, dict[str, int]],
        devbox_home: DevboxPaths,
        registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        """What the example's README says failure looks like."""
        root, _ports = example
        (root / "legacy-api" / "server.js.fake").unlink()

        config = load_config(root / "devbox.yml")
        context = ProjectContext(
            config=config,
            project_root=root,
            paths=devbox_home,
            platform=host_platform,
            environ={"PATH": "/usr/bin:/bin"},
            supervisor=create_supervisor(),
            runner=SubprocessRunner(),
        )
        orchestrator = Orchestrator(context=context, registry=FilesystemRegistry(registry.root))
        resolution = resolve_lockfile(
            config, registry=FilesystemRegistry(registry.root), platform=host_platform
        )
        write_lockfile(root / "devbox.lock", resolution.lockfile)

        try:
            with pytest.raises(ServiceStartError) as caught:
                orchestrator.up()
        finally:
            orchestrator.down()

        error = caught.value
        assert error.service == "legacy-api"
        assert error.details is not None
        assert error.details.command == "node server.js"
        assert "no directive file was found" in error.details.stderr_tail
        assert error.details.stderr_log
