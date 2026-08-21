"""Shared infrastructure: ownership, persistence and the external guarantee.

Uses the fake supervisor and a recording command runner, because a real
PostgreSQL cannot run in the development container.  What is under test is
Devbox's own logic — who may stop what, what is initialised when, which
arguments are built — and all of it is real.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from devbox.catalogs.providers import ProviderDescriptor, register_provider_descriptor
from devbox.config.models import InfrastructureSpec, Mode
from devbox.errors import InfrastructureError
from devbox.health.checks import HealthResult
from devbox.host.paths import DevboxPaths
from devbox.infra import InfraManager
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
    unregister_provider,
)
from devbox.process.fake import FakeSupervisor
from devbox.process.runner import CommandResult, RecordingRunner
from devbox.runtime.manager import RuntimeManager
from devbox.state import StateStore

pytestmark = pytest.mark.unit

PROJECT_A = "app-a-111"
PROJECT_B = "app-b-222"

FAKEDB = ProviderDescriptor(
    name="fakedb",
    default_port=6000,
    default_version="1.0",
    url_scheme="fakedb",
    default_username="devbox",
    runtime_kind="",
)


class FakeDbProvider:
    """A provider that needs nothing installed — the extensibility example."""

    def __init__(self) -> None:
        self.initialised: list[str] = []

    @property
    def descriptor(self) -> ProviderDescriptor:
        return FAKEDB

    def needs_initialisation(self, context: InfraContext) -> bool:
        return not (context.data_dir / "INITIALISED").is_file()

    def initialise(self, context: InfraContext) -> None:
        (context.data_dir / "INITIALISED").write_text("yes", encoding="utf-8")
        self.initialised.append(context.name)

    def start_command(self, context: InfraContext):  # type: ignore[no-untyped-def]
        return ("fakedb-server", "--port", str(context.port), "--data", str(context.data_dir))

    def stop_command(self, context: InfraContext):  # type: ignore[no-untyped-def]
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
def provider() -> Callable[[], FakeDbProvider]:
    register_provider_descriptor(FAKEDB)
    instance = FakeDbProvider()
    register_provider(instance)
    yield lambda: instance  # type: ignore[misc]
    unregister_provider("fakedb")


@pytest.fixture
def manager(devbox_home: DevboxPaths, monkeypatch: pytest.MonkeyPatch) -> InfraManager:
    """An InfraManager whose healthchecks pass immediately."""
    monkeypatch.setattr(
        "devbox.infra.manager.wait_for_health",
        lambda *args, **kwargs: HealthResult(
            healthy=True, attempts=1, elapsed=0.0, description="fake"
        ),
    )
    return InfraManager(
        paths=devbox_home,
        supervisor=FakeSupervisor(),
        state=StateStore(paths=devbox_home).ensure(),
        runtimes=RuntimeManager(devbox_home),
        runner=RecordingRunner(),
        sleep=lambda _seconds: None,
    )


def spec(
    name: str = "company-db", mode: Mode = Mode.MANAGED, **kwargs: object
) -> InfrastructureSpec:
    return InfrastructureSpec(
        name=name,
        provider="fakedb",
        version="1.0",
        mode=mode,
        port=6000,
        url_env="PROD_DB_URL" if mode is Mode.EXTERNAL else None,
        **kwargs,  # type: ignore[arg-type]
    )


class TestStart:
    def test_an_instance_starts_and_is_recorded(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        result = manager.up(spec(), project_id=PROJECT_A)
        assert result.started
        assert result.record is not None
        assert result.record.claims == (PROJECT_A,)

    def test_an_uninitialised_instance_is_initialised_once(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        manager.down(spec(), project_id=PROJECT_A)
        second = manager.up(spec(), project_id=PROJECT_A)
        assert provider().initialised == ["company-db"], "initialisation must not run twice"
        assert not second.initialised

    def test_starting_an_already_running_instance_is_a_no_op(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        second = manager.up(spec(), project_id=PROJECT_A)
        assert second.already_running
        assert not second.started

    def test_a_second_project_claims_the_same_instance(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        result = manager.up(spec(), project_id=PROJECT_B)
        assert result.already_running
        assert result.record is not None
        assert result.record.claims == (PROJECT_A, PROJECT_B)

    def test_data_and_log_directories_are_created_under_the_devbox_home(
        self,
        manager: InfraManager,
        provider: Callable[[], FakeDbProvider],
        devbox_home: DevboxPaths,
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        data = devbox_home.infra_data_dir("fakedb", "company-db")
        assert data.is_dir()
        assert devbox_home.root in data.parents


class TestExternalIsNeverStarted:
    """Non-regression #9."""

    def test_up_never_starts_an_external_instance(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        result = manager.up(spec(mode=Mode.EXTERNAL), project_id=PROJECT_A)
        assert result.skipped_external
        assert not result.started
        assert provider().initialised == [], "an external server must never be initialised"

    def test_no_process_is_spawned_for_an_external_instance(
        self, devbox_home: DevboxPaths, provider: Callable[[], FakeDbProvider]
    ) -> None:
        supervisor = FakeSupervisor()
        manager = InfraManager(
            paths=devbox_home,
            supervisor=supervisor,
            state=StateStore(paths=devbox_home).ensure(),
            runtimes=RuntimeManager(devbox_home),
            runner=RecordingRunner(),
        )
        manager.up(spec(mode=Mode.EXTERNAL), project_id=PROJECT_A)
        assert supervisor.started == []

    def test_down_never_stops_an_external_instance(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        result = manager.down(spec(mode=Mode.EXTERNAL), project_id=PROJECT_A)
        assert not result.stopped
        assert "external" in result.detail

    def test_status_reports_external_without_probing(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        status = manager.status(spec(mode=Mode.EXTERNAL))
        assert status.is_external
        assert not status.running
        assert "never starts or stops" in status.detail


class TestOwnership:
    """Non-regression #2."""

    def test_a_project_may_stop_what_only_it_uses(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        result = manager.down(spec(), project_id=PROJECT_A)
        assert result.stopped
        assert not result.refused

    def test_a_project_never_stops_an_instance_another_project_uses(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        manager.up(spec(), project_id=PROJECT_B)

        result = manager.down(spec(), project_id=PROJECT_A)

        assert result.refused
        assert not result.stopped
        assert "still used by 1 other project" in result.detail
        assert manager.status(spec()).running, "the shared instance must still be up"

    def test_a_project_never_stops_an_instance_started_by_devbox_infra_up(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), global_ownership=True)
        manager.up(spec(), project_id=PROJECT_A)

        result = manager.down(spec(), project_id=PROJECT_A)

        assert result.refused
        assert "devbox infra down" in result.detail
        assert manager.status(spec()).running

    def test_devbox_infra_down_stops_a_globally_owned_instance(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), global_ownership=True)
        result = manager.down(spec(), force_global=True)
        assert result.stopped
        assert not manager.status(spec()).running

    def test_releasing_the_last_claim_allows_a_stop(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        manager.up(spec(), project_id=PROJECT_B)
        manager.down(spec(), project_id=PROJECT_B)
        result = manager.down(spec(), project_id=PROJECT_A)
        assert result.stopped

    def test_claims_can_be_released_without_stopping(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        released = manager.release_project(PROJECT_A)
        assert released == ("company-db",)
        assert manager.status(spec()).running


class TestDataPersistence:
    """Stopping an instance must never take the databases with it."""

    def test_the_data_directory_survives_a_stop(
        self,
        manager: InfraManager,
        provider: Callable[[], FakeDbProvider],
        devbox_home: DevboxPaths,
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        data = devbox_home.infra_data_dir("fakedb", "company-db")
        (data / "clients.db").write_text("precious", encoding="utf-8")

        manager.down(spec(), project_id=PROJECT_A)

        assert (data / "clients.db").read_text(encoding="utf-8") == "precious"

    def test_a_restart_reuses_the_existing_data_directory(
        self,
        manager: InfraManager,
        provider: Callable[[], FakeDbProvider],
        devbox_home: DevboxPaths,
    ) -> None:
        manager.up(spec(), project_id=PROJECT_A)
        data = devbox_home.infra_data_dir("fakedb", "company-db")
        (data / "clients.db").write_text("precious", encoding="utf-8")
        manager.down(spec(), project_id=PROJECT_A)
        manager.up(spec(), project_id=PROJECT_A)
        assert (data / "clients.db").read_text(encoding="utf-8") == "precious"


class TestIdempotence:
    def test_stopping_something_that_never_started_is_success(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        result = manager.down(spec(), project_id=PROJECT_A)
        assert result.stopped
        assert not result.was_running

    def test_status_of_something_that_never_started(
        self, manager: InfraManager, provider: Callable[[], FakeDbProvider]
    ) -> None:
        assert not manager.status(spec()).running

    def test_a_stale_record_for_a_dead_process_is_cleaned_up(
        self,
        manager: InfraManager,
        provider: Callable[[], FakeDbProvider],
        devbox_home: DevboxPaths,
    ) -> None:
        """What Devbox finds after a reboot."""
        manager.up(spec(), project_id=PROJECT_A)
        store = StateStore(paths=devbox_home)
        record = store.load_infra().get("company-db")
        assert record is not None

        # The supervisor no longer knows this process: a new Devbox invocation.
        fresh = InfraManager(
            paths=devbox_home,
            supervisor=FakeSupervisor(),
            state=store,
            runtimes=RuntimeManager(devbox_home),
            runner=RecordingRunner(),
        )
        assert not fresh.status(spec()).running
        assert fresh.down(spec(), project_id=PROJECT_A).stopped


class TestUnknownProvider:
    def test_an_unregistered_provider_is_an_actionable_error(self, manager: InfraManager) -> None:
        unknown = InfrastructureSpec(name="x", provider="cassandra", version="5", port=9042)
        with pytest.raises(InfrastructureError) as caught:
            manager.up(unknown)
        assert "no implementation registered" in str(caught.value)
        assert caught.value.hint is not None


class TestCommandConstruction:
    """The argv built for real providers, asserted without either server installed."""

    def test_postgres_start_command(self, devbox_home: DevboxPaths, tmp_path: Path) -> None:
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ("postgres",))
        argv = list(PostgresProvider().start_command(context))
        assert argv[0].endswith("postgres")
        assert "-D" in argv
        assert str(context.data_dir) in argv
        assert "-p" in argv
        assert "5432" in argv

    def test_postgres_stops_with_pg_ctl_fast_not_a_signal(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ("pg_ctl",))
        argv = PostgresProvider().stop_command(context)
        assert argv is not None
        assert "stop" in argv
        assert "fast" in argv

    def test_postgres_never_initialises_an_existing_cluster(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        """Running initdb over live data would destroy every project's databases."""
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ("initdb",))
        (context.data_dir / "PG_VERSION").write_text("17\n", encoding="utf-8")
        assert not PostgresProvider().needs_initialisation(context)

        runner = context.runner
        assert isinstance(runner, RecordingRunner)
        PostgresProvider().initialise(context)
        assert runner.calls == [], "initdb must not run against an existing cluster"

    def test_mysql_start_command(self, devbox_home: DevboxPaths, tmp_path: Path) -> None:
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "mysql", ("mysqld",), port=3306
        )
        argv = list(MysqlProvider().start_command(context))
        assert argv[0].endswith("mysqld")
        assert f"--datadir={context.data_dir}" in argv
        assert "--port=3306" in argv

    def test_mysql_initialises_only_an_empty_data_directory(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "mysql", ("mysqld",), port=3306
        )
        assert MysqlProvider().needs_initialisation(context)

        (context.data_dir / "mysql").mkdir(parents=True, exist_ok=True)
        assert not MysqlProvider().needs_initialisation(context)

        runner = context.runner
        assert isinstance(runner, RecordingRunner)
        MysqlProvider().initialise(context)
        assert runner.calls == [], "--initialize must not run over an existing data directory"

    def test_mysql_initialisation_uses_initialize_insecure(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "mysql", ("mysqld",), port=3306
        )
        runner = context.runner
        assert isinstance(runner, RecordingRunner)
        MysqlProvider().initialise(context)

        assert runner.ran("--initialize-insecure")
        assert runner.ran(f"--datadir={context.data_dir}")

    def test_mysql_stops_with_mysqladmin_shutdown(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "mysql", ("mysqladmin",), port=3306
        )
        argv = MysqlProvider().stop_command(context)
        assert argv is not None
        assert argv[0].endswith("mysqladmin")
        assert "shutdown" in argv

    def test_mysql_lists_databases_one_per_line(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        """--batch --skip-column-names is what makes the output parseable."""
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "mysql", ("mysql", "mysqladmin"), port=3306
        )
        client = MysqlProvider().database_client(context)
        from devbox.infra.sql import SqlDatabaseClient

        assert isinstance(client, SqlDatabaseClient)
        assert "--batch" in client.list_argv
        assert "--skip-column-names" in client.list_argv
        assert "SHOW DATABASES" in client.list_argv

    def test_mysql_urls_use_the_mysql_scheme(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "mysql", (), port=3306)
        url = MysqlProvider().connection_url(context, "legacy", username="root")
        assert url == "mysql://root@127.0.0.1:3306/legacy"

    def test_mysql_healthcheck_targets_its_own_port(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.config.models import TcpHealthcheck
        from devbox.infra.mysql import MysqlProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "mysql", (), port=3306)
        check = MysqlProvider().healthcheck(context)
        assert isinstance(check, TcpHealthcheck)
        assert check.port == 3306

    def test_postgres_lists_databases_unaligned_and_tuples_only(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.postgres import PostgresProvider
        from devbox.infra.sql import SqlDatabaseClient

        context = _context_with_fake_binaries(
            devbox_home, tmp_path, "postgres", ("psql", "createdb")
        )
        client = PostgresProvider().database_client(context)
        assert isinstance(client, SqlDatabaseClient)
        assert "-A" in client.list_argv and "-t" in client.list_argv
        assert "pg_database" in " ".join(client.list_argv)
        assert "--owner" in client.create_argv_template

    def test_postgres_initialisation_creates_the_cluster(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ("initdb",))
        runner = context.runner
        assert isinstance(runner, RecordingRunner)
        PostgresProvider().initialise(context)

        assert runner.ran("initdb")
        assert runner.ran("--pgdata")
        assert runner.ran("UTF8")

    def test_a_missing_binary_is_an_actionable_error(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ())
        with pytest.raises(InfrastructureError) as caught:
            PostgresProvider().start_command(context)
        assert "was not found" in str(caught.value)
        assert caught.value.hint is not None
        assert "devbox runtime install" in caught.value.hint

    def test_connection_urls_are_built_from_the_descriptor(
        self, devbox_home: DevboxPaths, tmp_path: Path
    ) -> None:
        from devbox.infra.postgres import PostgresProvider

        context = _context_with_fake_binaries(devbox_home, tmp_path, "postgres", ())
        url = PostgresProvider().connection_url(context, "clients", username="devbox")
        assert url == "postgresql://devbox@127.0.0.1:5432/clients"


def _context_with_fake_binaries(
    devbox_home: DevboxPaths,
    tmp_path: Path,
    provider_name: str,
    binaries: tuple[str, ...],
    port: int = 5432,
) -> InfraContext:
    """Build a context whose PATH contains stand-ins for the provider's binaries."""
    import os

    bin_dir = tmp_path / "runtime-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for binary in binaries:
        target = bin_dir / binary
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        (bin_dir / f"{binary}.exe").write_bytes(b"MZ")

    data_dir = devbox_home.infra_data_dir(provider_name, "company-db")
    log_dir = devbox_home.infra_logs_dir(provider_name, "company-db")
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    return InfraContext(
        spec=InfrastructureSpec(name="company-db", provider=provider_name, version="17", port=port),
        data_dir=data_dir,
        log_dir=log_dir,
        runtime_root=bin_dir,
        environment={"PATH": str(bin_dir) + os.pathsep + "/usr/bin"},
        runner=RecordingRunner(default=CommandResult(argv=(), returncode=0)),
    )
