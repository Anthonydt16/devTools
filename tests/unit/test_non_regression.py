"""The ten rules that must never break.

Each is stated in ``CLAUDE.md`` and has a class here.  Several are also covered
end to end in ``tests/integration/`` — this file is the checklist: one place a
reviewer can read to see that every rule is enforced, and where.

The last class is a *meta* test: it deliberately breaks a mandatory behaviour and
asserts that the suite notices.  A test suite that stays green when the code is
wrong is decoration.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.catalogs.providers import ProviderDescriptor, register_provider_descriptor
from devbox.config.models import (
    DatabaseInit,
    DatabaseSpec,
    InfrastructureSpec,
    Mode,
    RuntimeRequirement,
)
from devbox.database.manager import DatabaseManager
from devbox.errors import DependencyGraphError, IntegrityError, SecurityError
from devbox.graph.dependency import DependencyGraph
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import PLATFORM_WINDOWS_X64
from devbox.infra.manager import InfraManager
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
    unregister_provider,
)
from devbox.lockfile.models import LockedRuntime
from devbox.process.fake import FakeSupervisor
from devbox.process.runner import RecordingRunner
from devbox.process.supervisor import LaunchSpec, ProcessState
from devbox.runtime.environment import build_environment
from devbox.runtime.manager import RuntimeManager
from devbox.runtime.registry import FilesystemRegistry
from devbox.security.archive import extract_archive
from devbox.security.command import split_command
from devbox.state.store import StateStore
from devbox.validation.checks import Check, CheckOutcome, CheckResult
from devbox.validation.suite import ValidationSuite

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Two Node versions, two PATH values, at the same time.
# ---------------------------------------------------------------------------
class TestRule01_TwoRuntimesAtOnce:
    """Also proven with real processes in tests/integration/test_end_to_end.py."""

    def test_two_services_receive_two_different_paths(self, tmp_path: Path) -> None:
        legacy = build_environment(
            [RuntimeRequirement("node", "18.19.0")],
            runtime_root=tmp_path,
            base_environment={"PATH": "C:\\Windows"},
            platform=PLATFORM_WINDOWS_X64,
        )
        modern = build_environment(
            [RuntimeRequirement("node", "24.6.0")],
            runtime_root=tmp_path,
            base_environment={"PATH": "C:\\Windows"},
            platform=PLATFORM_WINDOWS_X64,
        )

        assert legacy.variables["PATH"] != modern.variables["PATH"]
        assert "18.19.0" in legacy.path_entries[0]
        assert "24.6.0" in modern.path_entries[0]
        # Both exist at once: nothing global was mutated to make either work.
        assert "24.6.0" not in legacy.variables["PATH"]
        assert "18.19.0" not in modern.variables["PATH"]


# ---------------------------------------------------------------------------
# 2. `devbox down` never stops infrastructure started by `devbox infra up`.
# ---------------------------------------------------------------------------
FAKEDB = ProviderDescriptor(name="nrdb", default_port=7000, url_scheme="nrdb")


class _Provider:
    @property
    def descriptor(self) -> ProviderDescriptor:
        return FAKEDB

    def needs_initialisation(self, context: InfraContext) -> bool:
        return not (context.data_dir / "INIT").is_file()

    def initialise(self, context: InfraContext) -> None:
        (context.data_dir / "INIT").write_text("y", encoding="utf-8")

    def start_command(self, context: InfraContext) -> tuple[str, ...]:
        return ("nrdb", "--port", str(context.port))

    def stop_command(self, context: InfraContext) -> None:
        del context
        return

    def healthcheck(self, context: InfraContext):  # type: ignore[no-untyped-def]
        return default_healthcheck(context, retries=1)

    def connection_url(
        self, context: InfraContext, database: str, *, username: str = "", password: str = ""
    ) -> str:
        del password
        return FAKEDB.connection_url(
            host=context.host, port=context.port, database=database, username=username
        )

    def database_client(self, context: InfraContext) -> DatabaseClient:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def infra(devbox_home: DevboxPaths, monkeypatch: pytest.MonkeyPatch) -> InfraManager:
    from devbox.health.checks import HealthResult

    register_provider_descriptor(FAKEDB)
    register_provider(_Provider())
    monkeypatch.setattr(
        "devbox.infra.manager.wait_for_health",
        lambda *a, **k: HealthResult(True, 1, 0.0, "fake"),
    )
    manager = InfraManager(
        devbox_home,
        supervisor=FakeSupervisor(),
        state=StateStore(paths=devbox_home).ensure(),
        runtimes=RuntimeManager(devbox_home),
        runner=RecordingRunner(),
        sleep=lambda _s: None,
    )
    yield manager  # type: ignore[misc]
    unregister_provider("nrdb")


SHARED = InfrastructureSpec(name="company-db", provider="nrdb", version="1", port=7000)


class TestRule02_SharedInfrastructureSurvivesProjectShutdown:
    def test_a_project_cannot_stop_what_devbox_infra_up_started(self, infra: InfraManager) -> None:
        infra.up(SHARED, global_ownership=True)
        infra.up(SHARED, project_id="app-a")

        result = infra.down(SHARED, project_id="app-a")

        assert result.refused
        assert "devbox infra down" in result.detail
        assert infra.status(SHARED).running

    def test_a_project_cannot_stop_what_another_project_uses(self, infra: InfraManager) -> None:
        infra.up(SHARED, project_id="app-a")
        infra.up(SHARED, project_id="app-b")

        assert infra.down(SHARED, project_id="app-a").refused
        assert infra.status(SHARED).running

    def test_stopping_never_deletes_the_data_directory(
        self, infra: InfraManager, devbox_home: DevboxPaths
    ) -> None:
        infra.up(SHARED, project_id="app-a")
        data = devbox_home.infra_data_dir("nrdb", "company-db")
        (data / "clients").write_text("every project's data", encoding="utf-8")

        infra.down(SHARED, project_id="app-a")

        assert (data / "clients").read_text(encoding="utf-8") == "every project's data"


# ---------------------------------------------------------------------------
# 3. An existing database is never re-initialised.
# ---------------------------------------------------------------------------
class _Catalogue:
    def __init__(self, existing: tuple[str, ...] = ()) -> None:
        self.databases = set(existing)
        self.created: list[str] = []

    def database_exists(self, name: str) -> bool:
        return name in self.databases

    def create_database(self, name: str, *, owner: str = "") -> None:
        del owner
        self.databases.add(name)
        self.created.append(name)

    def list_databases(self) -> tuple[str, ...]:
        return tuple(sorted(self.databases))


class TestRule03_ExistingDatabaseIsNeverReinitialised:
    def test_an_existing_database_is_neither_recreated_nor_initialised(
        self, tmp_path: Path
    ) -> None:
        catalogue = _Catalogue(existing=("clients",))
        runner = RecordingRunner()

        class Provider(_Provider):
            def database_client(self, context: InfraContext) -> DatabaseClient:
                del context
                return catalogue  # type: ignore[return-value]

        register_provider_descriptor(FAKEDB)
        register_provider(Provider())
        try:
            manager = DatabaseManager(
                runner=runner,
                context_factory=lambda spec: InfraContext(
                    spec=spec,
                    data_dir=tmp_path,
                    log_dir=tmp_path,
                    runtime_root=None,
                    environment={},
                    runner=runner,
                ),
            )
            outcome = manager.ensure(
                DatabaseSpec(
                    name="clients",
                    provider="nrdb",
                    infrastructure="company-db",
                    database="clients",
                    init=DatabaseInit(command="npm run db:init"),
                ),
                SHARED,
                project_root=tmp_path,
            )
        finally:
            unregister_provider("nrdb")

        assert outcome.existed
        assert not outcome.created
        assert not outcome.initialised
        assert catalogue.created == []
        assert runner.calls == [], "the init command must not have run"


# ---------------------------------------------------------------------------
# 4. A SHA256 mismatch refuses the install; nothing is extracted or executed.
# ---------------------------------------------------------------------------
class TestRule04_IntegrityMismatchRefusesInstall:
    def test_a_wrong_digest_refuses_and_leaves_nothing_behind(
        self,
        devbox_home: DevboxPaths,
        fake_registry,
        host_platform,  # type: ignore[no-untyped-def]
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        artifact = fake_registry.artifact("node", "18.19.0")
        tampered = LockedRuntime(
            name="node",
            version="18.19.0",
            platform=host_platform.tag,
            sha256="f" * 64,
            source=str(fake_registry.root),
            url=artifact.relative_url,
        )

        with pytest.raises(IntegrityError) as caught:
            manager.install_locked(tampered, FilesystemRegistry(fake_registry.root))

        assert "f" * 64 in str(caught.value)
        assert not manager.install_root("node", "18.19.0").exists()
        assert list(devbox_home.downloads.glob("*.part")) == []
        assert not any(devbox_home.cache.rglob("*.zip"))


# ---------------------------------------------------------------------------
# 5. An archive containing ../../evil.exe is rejected.
# ---------------------------------------------------------------------------
class TestRule05_ZipSlipIsRejected:
    def test_a_traversing_zip_entry_is_refused_and_nothing_is_written(self, tmp_path: Path) -> None:
        archive = tmp_path / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("bin/node", "#!/bin/sh\n")
            zf.writestr("../../evil.exe", "MZ")

        with pytest.raises(SecurityError) as caught:
            extract_archive(archive, tmp_path / "install")

        assert "../../evil.exe" in str(caught.value)
        assert not (tmp_path / "install").exists()
        assert not (tmp_path.parent / "evil.exe").exists()

    def test_a_traversing_tar_entry_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "runtime.tar"
        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo("../../evil.exe")
            info.size = 2
            tf.addfile(info, io.BytesIO(b"MZ"))
        with pytest.raises(SecurityError):
            extract_archive(archive, tmp_path / "install")


# ---------------------------------------------------------------------------
# 6. A dependency cycle produces a clear error naming the cycle.
# ---------------------------------------------------------------------------
class TestRule06_DependencyCycleIsNamed:
    def test_a_three_node_cycle_is_reported_in_order(self) -> None:
        with pytest.raises(DependencyGraphError) as caught:
            DependencyGraph({"a": ["b"], "b": ["c"], "c": ["a"]}).validate()

        message = str(caught.value)
        assert "dependency cycle detected" in message
        assert "a -> b -> c -> a" in message
        assert caught.value.hint is not None


# ---------------------------------------------------------------------------
# 7. After a hard kill, Devbox never kills a process it does not own.
# ---------------------------------------------------------------------------
class TestRule07_PidReuseIsRefused:
    """Also proven against real processes in tests/integration and tests/windows."""

    def test_a_recycled_pid_is_never_signalled(self, tmp_path: Path) -> None:
        working = tmp_path / "app"
        working.mkdir()
        supervisor = FakeSupervisor()
        record = supervisor.start(
            LaunchSpec(
                name="backend",
                argv=("node", "server.js"),
                working_dir=working,
                env={},
                stdout_path=tmp_path / "o.log",
                stderr_path=tmp_path / "e.log",
            ),
            project_id="demo",
        )

        # The machine rebooted; the number is alive again but is somebody else's.
        supervisor.simulate_pid_reuse("backend")

        assert supervisor.status(record).state is ProcessState.PID_REUSED
        outcome = supervisor.stop(record)
        assert outcome.refused
        assert not outcome.stopped

    def test_a_record_with_no_creation_time_is_never_signalled(self, tmp_path: Path) -> None:
        working = tmp_path / "app"
        working.mkdir()
        supervisor = FakeSupervisor()
        record = supervisor.start(
            LaunchSpec(
                name="backend",
                argv=("node", "server.js"),
                working_dir=working,
                env={},
                stdout_path=tmp_path / "o.log",
                stderr_path=tmp_path / "e.log",
            ),
            project_id="demo",
        )
        assert supervisor.stop(replace(record, creation_time=None)).refused


# ---------------------------------------------------------------------------
# 8. Commands with spaces and special characters are passed through unaltered.
# ---------------------------------------------------------------------------
class TestRule08_ArgumentsAreNotReinterpreted:
    """Also proven by running a real process in tests/integration and tests/windows."""

    def test_metacharacters_become_literal_arguments(self) -> None:
        argv = split_command('npm run build -- --out "dist folder" && rm -rf /')
        assert argv == (
            "npm",
            "run",
            "build",
            "--",
            "--out",
            "dist folder",
            "&&",
            "rm",
            "-rf",
            "/",
        )

    def test_a_windows_path_with_spaces_stays_one_argument(self) -> None:
        argv = split_command('node "C:/Program Files/My App/server.js" --port 3000')
        assert argv[1] == "C:/Program Files/My App/server.js"

    def test_an_explicit_list_is_passed_through_verbatim(self) -> None:
        raw = ["node", "-e", 'console.log("a; b && c")']
        assert split_command(raw) == tuple(raw)

    def test_shell_is_never_used_anywhere_in_the_package(self) -> None:
        """A structural check over the real source: no call may pass shell=True.

        Parsed rather than grepped, so the ban in the docstrings that *mention*
        ``shell=True`` does not make this pass or fail for the wrong reason.
        """
        import ast

        import devbox

        package_root = Path(devbox.__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(package_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "shell":
                        continue
                    value = keyword.value
                    if not (isinstance(value, ast.Constant) and value.value is False):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"shell= passed to a call in {offenders}"


# ---------------------------------------------------------------------------
# 9. An `external` configuration never starts local infrastructure.
# ---------------------------------------------------------------------------
class TestRule09_ExternalNeverStartsAnything:
    def test_up_starts_no_process_for_an_external_instance(self, devbox_home: DevboxPaths) -> None:
        register_provider_descriptor(FAKEDB)
        register_provider(_Provider())
        supervisor = FakeSupervisor()
        try:
            manager = InfraManager(
                devbox_home,
                supervisor=supervisor,
                state=StateStore(paths=devbox_home).ensure(),
                runtimes=RuntimeManager(devbox_home),
                runner=RecordingRunner(),
            )
            external = replace(SHARED, mode=Mode.EXTERNAL, url_env="PROD_DB_URL")
            result = manager.up(external, project_id="app-a")
        finally:
            unregister_provider("nrdb")

        assert result.skipped_external
        assert not result.started
        assert supervisor.started == [], "no process may be spawned for an external server"

    def test_a_production_environment_declares_no_managed_infrastructure(self) -> None:
        from devbox.config import parse_config
        from devbox.config.loader import parse_yaml_document

        document = parse_yaml_document(
            "version: 1\n"
            "infrastructure:\n"
            "  pg: { provider: postgres, version: '17', port: 5432 }\n"
            "environments:\n"
            "  production:\n"
            "    infrastructure:\n"
            "      pg: { mode: external, url_env: PROD_DATABASE_URL }\n",
            source="devbox.yml",
        )
        config = parse_config(document, environment="production", source="devbox.yml")
        assert config.managed_infrastructure() == ()
        assert config.required_runtimes() == (), "an external server needs nothing installed"


# ---------------------------------------------------------------------------
# 10. `devbox test` exits non-zero when a mandatory check fails.
# ---------------------------------------------------------------------------
class _AlwaysFails(Check):
    def run(self, runner, *, cwd, env) -> CheckResult:  # type: ignore[no-untyped-def]
        del runner, cwd, env
        return CheckResult(
            name=self.name,
            outcome=CheckOutcome.FAILED,
            mandatory=self.mandatory,
            summary="deliberately failing",
            detail="this check exists to prove the suite reports failures",
        )


class _AlwaysPasses(Check):
    def run(self, runner, *, cwd, env) -> CheckResult:  # type: ignore[no-untyped-def]
        del runner, cwd, env
        return CheckResult(name=self.name, outcome=CheckOutcome.PASSED, summary="fine")


class TestRule10_ValidationFailsLoudly:
    def test_a_failing_mandatory_check_makes_the_run_fail(self, tmp_path: Path) -> None:
        from devbox.errors import ExitCode

        suite = ValidationSuite(
            root=tmp_path,
            checks=[
                _AlwaysPasses("ok", "passes"),
                _AlwaysFails("broken", "fails", mandatory=True),
            ],
        )
        report = suite.run()

        assert not report.ok
        assert report.exit_code() == ExitCode.VALIDATION
        assert [result.name for result in report.blocking] == ["broken"]

    def test_a_failing_optional_check_does_not_fail_the_run(self, tmp_path: Path) -> None:
        from devbox.errors import ExitCode

        suite = ValidationSuite(
            root=tmp_path,
            checks=[_AlwaysFails("optional", "fails", mandatory=False)],
        )
        report = suite.run()

        assert report.ok
        assert report.exit_code() == ExitCode.OK
        assert len(report.failed) == 1, "it is still reported"

    def test_an_all_passing_run_succeeds(self, tmp_path: Path) -> None:
        report = ValidationSuite(root=tmp_path, checks=[_AlwaysPasses("ok", "passes")]).run()
        assert report.ok
        assert report.exit_code() == 0
