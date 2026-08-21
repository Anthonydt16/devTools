"""Autonomy: Devbox has to cope with the state it finds, not the state it wants.

A developer's machine is not a clean room.  The cache is half full, a download
was interrupted by a closed laptop, Devbox itself was killed mid-``up``, the
project lives in ``C:\\Users\\Jean Dupont\\Mes Projets\\portail café``.  Each of
those is a test here, and each asserts that Devbox recovers or reports — never
that it silently does the wrong thing.
"""

from __future__ import annotations

import json
import shutil
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from devbox.config.loader import load_config
from devbox.errors import ConfigError
from devbox.host.paths import DEVBOX_HOME_ENV, DevboxPaths, resolve_devbox_home
from devbox.host.platform_info import Platform
from devbox.lockfile.io import write_lockfile
from devbox.process.factory import create_supervisor
from devbox.process.runner import SubprocessRunner
from devbox.runtime.download import Downloader
from devbox.runtime.manager import RuntimeManager
from devbox.runtime.registry import FilesystemRegistry
from devbox.runtime.resolver import resolve_lockfile
from devbox.services.orchestrator import Orchestrator
from devbox.services.project import ProjectContext
from devbox.state.store import StateStore
from devbox.testing.fake_runtime import FakeRegistry, build_fake_registry

pytestmark = pytest.mark.integration


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
        [("node", "18.19.0")],
        platform=host_platform.tag,
        extra_executables=("npm",),
    )


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, int]:
    """A project in a directory whose name would break naive quoting."""
    root = tmp_path / "Mes Projets" / "portail café"
    root.mkdir(parents=True)
    port = free_port()
    (root / "devbox.yml").write_text(
        f"""
version: 1
project: portail

services:
  api:
    runtime: {{ node: "18.19.0" }}
    working_dir: .
    command: >-
      ${{env.PY}} ${{env.DEVBOX_HOME}}/runtimes/node/18.19.0/runtime_main.py serve
      --port {port} --max-seconds 120
    healthcheck:
      tcp:
        host: 127.0.0.1
        port: {port}
        interval: 0.1
        timeout: 2
        retries: 100
""".lstrip(),
        encoding="utf-8",
    )
    return root, port


@pytest.fixture
def build_orchestrator(
    devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
) -> Callable[[Path], Orchestrator]:
    import sys

    def _build(root: Path) -> Orchestrator:
        config = load_config(root / "devbox.yml")
        context = ProjectContext(
            config=config,
            project_root=root,
            paths=devbox_home,
            platform=host_platform,
            environ={
                "PATH": "/usr/bin:/bin",
                "PY": sys.executable,
                "DEVBOX_HOME": str(devbox_home.root),
            },
            supervisor=create_supervisor(),
            runner=SubprocessRunner(),
        )
        return Orchestrator(context=context, registry=FilesystemRegistry(registry.root))

    return _build


@pytest.fixture
def ready(
    project: tuple[Path, int],
    build_orchestrator: Callable[[Path], Orchestrator],
    registry: FakeRegistry,
    host_platform: Platform,
) -> Iterator[tuple[Orchestrator, Path, int]]:
    root, port = project
    orchestrator = build_orchestrator(root)
    resolution = resolve_lockfile(
        orchestrator.config, registry=FilesystemRegistry(registry.root), platform=host_platform
    )
    write_lockfile(root / "devbox.lock", resolution.lockfile)
    try:
        yield orchestrator, root, port
    finally:
        try:
            orchestrator.down()
        except Exception:
            pass


class TestFirstRunOnACleanMachine:
    def test_the_home_directory_is_created_on_demand(self, tmp_path: Path) -> None:
        home = tmp_path / "brand new" / "Devbox"
        assert not home.exists()
        paths = DevboxPaths(root=home).ensure()
        assert paths.runtimes.is_dir()
        assert paths.cache.is_dir()
        assert paths.state.is_dir()
        assert paths.missing_directories() == ()

    def test_ensure_is_idempotent(self, tmp_path: Path) -> None:
        paths = DevboxPaths(root=tmp_path / "Devbox").ensure()
        (paths.runtimes / "keep.txt").write_text("x", encoding="utf-8")
        paths.ensure()
        assert (paths.runtimes / "keep.txt").is_file()

    def test_devbox_home_can_be_redirected(self, tmp_path: Path) -> None:
        """Needed to put runtimes on another drive, and by every test here."""
        target = tmp_path / "D drive" / "devbox"
        assert resolve_devbox_home({DEVBOX_HOME_ENV: str(target)}) == target.resolve()

    def test_an_empty_cache_downloads_and_a_full_cache_does_not(
        self, devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        artifact = registry.artifact("node", "18.19.0")
        from devbox.lockfile.models import LockedRuntime

        locked = LockedRuntime(
            name="node",
            version="18.19.0",
            platform=host_platform.tag,
            sha256=artifact.sha256,
            source=str(registry.root),
            url=artifact.relative_url,
        )
        first = manager.install_locked(locked, FilesystemRegistry(registry.root))
        assert not first.from_cache

        manager.remove("node", "18.19.0")
        second = manager.install_locked(locked, FilesystemRegistry(registry.root))
        assert second.from_cache


class TestInterruption:
    def test_an_interrupted_download_resumes(
        self, devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
    ) -> None:
        downloader = Downloader(devbox_home)
        source = FilesystemRegistry(registry.root)
        manifest = source.resolve("node", "18.19.0", host_platform)
        artifact = registry.artifact("node", "18.19.0")

        devbox_home.downloads.mkdir(parents=True, exist_ok=True)
        downloader.partial_path(manifest).write_bytes(
            artifact.path.read_bytes()[: artifact.size // 3]
        )

        result = downloader.fetch(manifest, source)
        assert result.resumed
        assert result.sha256 == artifact.sha256

    def test_temporary_files_are_cleaned_up(
        self, devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
    ) -> None:
        downloader = Downloader(devbox_home)
        manifest = FilesystemRegistry(registry.root).resolve("node", "18.19.0", host_platform)
        devbox_home.downloads.mkdir(parents=True, exist_ok=True)
        downloader.partial_path(manifest).write_bytes(b"half a runtime")

        assert downloader.clean_partials() == 1
        assert list(devbox_home.downloads.iterdir()) == []

    def test_an_interrupted_install_is_detected_not_used(
        self, devbox_home: DevboxPaths, registry: FakeRegistry, host_platform: Platform
    ) -> None:
        """A half-extracted runtime must never be mistaken for a good one."""
        manager = RuntimeManager(devbox_home, platform=host_platform)
        install_root = manager.install_root("node", "18.19.0")
        install_root.mkdir(parents=True)
        (install_root / "node").write_text("#!/bin/sh\n", encoding="utf-8")
        # No completion marker: this is what an interrupted extraction leaves.

        assert not manager.is_installed("node", "18.19.0")
        ok, reason = manager.verify("node", "18.19.0")
        assert not ok
        assert "incomplete" in reason
        assert manager.prune_incomplete() == ("node 18.19.0",)

    def test_an_interrupted_state_write_is_cleaned_up(self, devbox_home: DevboxPaths) -> None:
        store = StateStore(paths=devbox_home).ensure()
        leftover = store.projects_dir / "demo.json.tmp"
        leftover.write_text("half", encoding="utf-8")
        assert store.cleanup_temporary_files() == 1
        assert not leftover.exists()


class TestDamagedState:
    def test_corrupt_project_state_is_quarantined_and_reported(
        self, devbox_home: DevboxPaths
    ) -> None:
        store = StateStore(paths=devbox_home).ensure()
        path = store.project_file("demo")
        path.write_text("{ this is not json", encoding="utf-8")

        state = store.load_project("demo")

        assert state.processes == {}
        warnings = store.take_warnings()
        assert warnings and "not valid JSON" in warnings[0].message
        assert list(store.projects_dir.glob("*.corrupt-*"))

    def test_status_surfaces_state_warnings_to_the_user(
        self, ready: tuple[Orchestrator, Path, int], devbox_home: DevboxPaths
    ) -> None:
        orchestrator, _root, _port = ready
        store = orchestrator.context.state
        store.project_file(orchestrator.context.project_id).write_text("nope", encoding="utf-8")

        report = orchestrator.status()

        assert any("moved aside" in warning for warning in report.warnings)

    def test_state_referring_to_a_dead_process_reads_as_stopped(
        self, ready: tuple[Orchestrator, Path, int]
    ) -> None:
        """What Devbox finds after a reboot: the PID is in state, the process is not."""
        orchestrator, _root, _port = ready
        orchestrator.up()
        record = orchestrator.context.load_state().record("api")
        assert record is not None

        orchestrator.down()

        fresh = orchestrator.context.state.load_project(orchestrator.context.project_id)
        assert fresh.record("api") is None


class TestRecoveryAfterACrash:
    def test_devbox_restarts_a_project_whose_state_survived_a_kill(
        self,
        ready: tuple[Orchestrator, Path, int],
        build_orchestrator: Callable[[Path], Orchestrator],
    ) -> None:
        """Devbox is killed; the service is not. A later invocation finds it running."""
        orchestrator, root, port = ready
        orchestrator.up()
        assert port_open(port)

        # A brand-new Devbox process, with none of the previous one's handles.
        reborn = build_orchestrator(root)
        rows = {view.name: view for view in reborn.ps()}
        assert rows["api"].running, "an already-running service must be recognised"

        assert reborn.up().services[0].running, "up must not start a second copy"
        assert reborn.down().services[0].stopped
        assert wait_until(lambda: not port_open(port))

    def test_a_service_killed_behind_devboxs_back_is_reported_as_stopped(
        self, ready: tuple[Orchestrator, Path, int]
    ) -> None:
        import os
        import signal

        orchestrator, _root, port = ready
        orchestrator.up()
        record = orchestrator.context.load_state().record("api")
        assert record is not None

        os.killpg(record.process_group or record.pid, signal.SIGKILL)
        assert wait_until(lambda: not port_open(port))

        assert not orchestrator.ps()[0].running
        # And stopping it is not an error.
        assert orchestrator.down().services[0].stopped


class TestAwkwardEnvironments:
    def test_a_project_path_with_spaces_and_accents_works_end_to_end(
        self, ready: tuple[Orchestrator, Path, int]
    ) -> None:
        orchestrator, root, port = ready
        assert " " in str(root)
        assert "café" in str(root)

        report = orchestrator.up()
        assert report.healthy
        assert port_open(port)
        orchestrator.down()

    def test_a_devbox_home_with_spaces_works(
        self, tmp_path: Path, registry: FakeRegistry, host_platform: Platform
    ) -> None:
        home = DevboxPaths(root=tmp_path / "App Data" / "Devbox").ensure()
        manager = RuntimeManager(home, platform=host_platform)
        from devbox.lockfile.models import LockedRuntime

        artifact = registry.artifact("node", "18.19.0")
        manager.install_locked(
            LockedRuntime(
                name="node",
                version="18.19.0",
                platform=host_platform.tag,
                sha256=artifact.sha256,
                source=str(registry.root),
                url=artifact.relative_url,
            ),
            FilesystemRegistry(registry.root),
        )
        assert manager.verify("node", "18.19.0")[0]

    def test_a_minimal_configuration_is_enough(self, tmp_path: Path) -> None:
        path = tmp_path / "devbox.yml"
        path.write_text("version: 1\n", encoding="utf-8")
        config = load_config(path)
        assert config.services == {}

    def test_an_invalid_configuration_fails_before_anything_starts(self, tmp_path: Path) -> None:
        path = tmp_path / "devbox.yml"
        path.write_text("version: 1\nservices:\n  a:\n    command: 3\n", encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_config(path)
        assert "expected a string" in str(caught.value)


class TestIsolationFromTheMachine:
    def test_everything_written_stays_inside_the_devbox_home(
        self, ready: tuple[Orchestrator, Path, int], devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        """Zero privileges: nothing outside %LOCALAPPDATA%\\Devbox and .devbox/."""
        orchestrator, root, _port = ready

        def outside() -> set[Path]:
            return {
                path
                for path in tmp_path.rglob("*")
                if devbox_home.root not in path.parents
                and devbox_home.root != path
                and (root / ".devbox") not in path.parents
                and path != root / ".devbox"
                and not path.name.startswith("devbox.lock")
            }

        before = outside()
        orchestrator.up()
        orchestrator.down()
        assert outside() == before

    def test_the_process_environment_of_devbox_itself_is_never_mutated(
        self, ready: tuple[Orchestrator, Path, int]
    ) -> None:
        import os

        orchestrator, _root, _port = ready
        before = dict(os.environ)
        orchestrator.up()
        orchestrator.down()
        assert dict(os.environ) == before


class TestArtifactCleanliness:
    def test_a_build_excludes_devbox_internals(
        self, ready: tuple[Orchestrator, Path, int], tmp_path: Path
    ) -> None:
        from devbox.build.artifact import Builder, BuildSpec
        from devbox.lockfile.io import read_lockfile

        orchestrator, root, _port = ready
        orchestrator.up()
        orchestrator.down()
        assert (root / ".devbox" / "logs").is_dir(), "there are logs to exclude"

        result = Builder(environ={"SOURCE_DATE_EPOCH": "1700000000"}).build(
            BuildSpec(
                application="portail",
                version="1.0.0",
                project_root=root,
                output_dir=tmp_path / "dist",
            ),
            lockfile=read_lockfile(root / "devbox.lock"),
        )
        assert not any(entry.startswith(".devbox/") for entry in result.entries)
        assert "devbox.yml" in result.entries

    def test_the_manifest_is_machine_readable(
        self, ready: tuple[Orchestrator, Path, int], tmp_path: Path
    ) -> None:
        from devbox.build.artifact import Builder, BuildSpec
        from devbox.lockfile.io import read_lockfile

        _orchestrator, root, _port = ready
        result = Builder(environ={"SOURCE_DATE_EPOCH": "1700000000"}).build(
            BuildSpec(
                application="portail",
                version="1.0.0",
                project_root=root,
                output_dir=tmp_path / "dist",
                runtimes={"node": "18.19.0"},
            ),
            lockfile=read_lockfile(root / "devbox.lock"),
        )
        document = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert document["runtime"] == {"node": "18.19.0"}
        assert len(document["devbox_lock_hash"]) == 64
        shutil.rmtree(tmp_path / "dist")
