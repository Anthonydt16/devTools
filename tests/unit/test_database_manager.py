"""Logical databases: existence, one-shot init, and the external guarantee.

Non-regression #3 lives here: an existing database is never re-initialised.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from devbox.catalogs.providers import ProviderDescriptor, register_provider_descriptor
from devbox.config.models import (
    DatabaseInit,
    DatabaseSpec,
    InfrastructureSpec,
    Mode,
)
from devbox.database import DatabaseManager
from devbox.errors import DatabaseError
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
    unregister_provider,
)
from devbox.infra.sql import SqlDatabaseClient, parse_database_list
from devbox.process.runner import CommandResult, RecordingRunner

pytestmark = pytest.mark.unit

FAKEDB = ProviderDescriptor(
    name="fakedb", default_port=6000, url_scheme="fakedb", default_username="devbox"
)


class InMemoryClient:
    """A database server's catalog, in memory."""

    def __init__(self, existing: tuple[str, ...] = ()) -> None:
        self.databases = set(existing)
        self.created: list[tuple[str, str]] = []

    def database_exists(self, name: str) -> bool:
        return name in self.databases

    def create_database(self, name: str, *, owner: str = "") -> None:
        self.databases.add(name)
        self.created.append((name, owner))

    def list_databases(self) -> tuple[str, ...]:
        return tuple(sorted(self.databases))


class FakeDbProvider:
    def __init__(self, client: InMemoryClient) -> None:
        self.client = client

    @property
    def descriptor(self) -> ProviderDescriptor:
        return FAKEDB

    def needs_initialisation(self, context: InfraContext) -> bool:
        del context
        return False

    def initialise(self, context: InfraContext) -> None:  # pragma: no cover
        del context

    def start_command(self, context: InfraContext):  # type: ignore[no-untyped-def]
        del context
        return ("fakedb",)

    def stop_command(self, context: InfraContext):  # type: ignore[no-untyped-def]
        del context
        return

    def healthcheck(self, context: InfraContext):  # type: ignore[no-untyped-def]
        return default_healthcheck(context)

    def connection_url(
        self, context: InfraContext, database: str, *, username: str = "", password: str = ""
    ) -> str:
        return FAKEDB.connection_url(
            host=context.host,
            port=context.port,
            database=database,
            username=username,
            password=password,
        )

    def database_client(self, context: InfraContext) -> DatabaseClient:
        del context
        return self.client


@pytest.fixture
def catalog() -> InMemoryClient:
    register_provider_descriptor(FAKEDB)
    client = InMemoryClient()
    register_provider(FakeDbProvider(client))
    yield client  # type: ignore[misc]
    unregister_provider("fakedb")


def infra(mode: Mode = Mode.MANAGED, **kwargs: object) -> InfrastructureSpec:
    return InfrastructureSpec(
        name="company-db",
        provider="fakedb",
        version="1.0",
        mode=mode,
        port=6000,
        url_env="PROD_DB_URL" if mode is Mode.EXTERNAL else None,
        **kwargs,  # type: ignore[arg-type]
    )


def database(name: str = "clients", init: DatabaseInit | None = None) -> DatabaseSpec:
    return DatabaseSpec(
        name=name, provider="fakedb", infrastructure="company-db", database=name, init=init
    )


@pytest.fixture
def make_manager(tmp_path: Path) -> Callable[..., tuple[DatabaseManager, RecordingRunner]]:
    def _make(environ: dict[str, str] | None = None) -> tuple[DatabaseManager, RecordingRunner]:
        runner = RecordingRunner()
        manager = DatabaseManager(
            runner=runner,
            context_factory=lambda spec: InfraContext(
                spec=spec,
                data_dir=tmp_path / "data",
                log_dir=tmp_path / "logs",
                runtime_root=None,
                environment={"PATH": "/usr/bin"},
                runner=runner,
            ),
            environ=environ or {},
        )
        return manager, runner

    return _make


class TestExistingDatabases:
    """Non-regression #3."""

    def test_an_existing_database_is_left_untouched(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        catalog.databases.add("clients")
        manager, runner = make_manager()

        outcome = manager.ensure(
            database("clients", DatabaseInit(command="npm run db:init")),
            infra(),
            project_root=tmp_path,
        )

        assert outcome.existed
        assert not outcome.created
        assert not outcome.initialised
        assert catalog.created == [], "an existing database must never be recreated"
        assert runner.calls == [], "an existing database must never be re-initialised"

    def test_two_projects_can_share_one_database(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager()
        first = manager.ensure(database(), infra(), project_root=tmp_path)
        second = manager.ensure(database(), infra(), project_root=tmp_path)
        assert first.created
        assert second.existed
        assert not second.created
        assert catalog.created == [("clients", "")], "created exactly once"

    def test_init_runs_exactly_once_across_repeated_ups(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, runner = make_manager()
        spec = database("clients", DatabaseInit(command="npm run db:init"))
        for _ in range(3):
            manager.ensure(spec, infra(), project_root=tmp_path)
        assert len(runner.calls) == 1


class TestCreation:
    def test_a_missing_database_is_created(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager()
        outcome = manager.ensure(database(), infra(), project_root=tmp_path)
        assert outcome.created
        assert not outcome.existed
        assert catalog.databases == {"clients"}

    def test_the_owner_is_passed_through(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager()
        spec = DatabaseSpec(
            name="clients",
            provider="fakedb",
            infrastructure="company-db",
            database="clients",
            username="app",
        )
        manager.ensure(spec, infra(), project_root=tmp_path)
        assert catalog.created == [("clients", "app")]

    def test_init_receives_the_database_url(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, runner = make_manager()
        manager.ensure(
            database("clients", DatabaseInit(command="npm run db:init")),
            infra(),
            project_root=tmp_path,
        )
        assert runner.calls[0].env["DATABASE_URL"].endswith("/clients")
        assert runner.calls[0].env["DEVBOX_DATABASE_NAME"] == "clients"

    def test_init_runs_in_its_working_directory(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        (tmp_path / "backend").mkdir()
        manager, runner = make_manager()
        manager.ensure(
            database("clients", DatabaseInit(command="npm run db:init", working_dir="backend")),
            infra(),
            project_root=tmp_path,
        )
        assert runner.calls[0].cwd == str(tmp_path / "backend")

    def test_the_init_command_is_an_argv_list_not_a_shell_string(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, runner = make_manager()
        manager.ensure(
            database("clients", DatabaseInit(command="npm run db:init && rm -rf /")),
            infra(),
            project_root=tmp_path,
        )
        assert runner.calls[0].argv == ("npm", "run", "db:init", "&&", "rm", "-rf", "/")

    def test_a_failing_init_is_reported_with_its_output(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, runner = make_manager()
        runner.default = CommandResult(argv=(), returncode=1, stderr="migration 004 failed")

        with pytest.raises(DatabaseError) as caught:
            manager.ensure(
                database("clients", DatabaseInit(command="npm run db:init")),
                infra(),
                project_root=tmp_path,
            )

        message = str(caught.value)
        assert "exit code 1" in message
        assert "migration 004 failed" in message
        assert caught.value.hint is not None
        assert "left in place" in caught.value.hint

    def test_a_missing_init_working_directory_is_reported(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager()
        with pytest.raises(DatabaseError, match="working_dir does not exist"):
            manager.ensure(
                database("clients", DatabaseInit(command="x", working_dir="nope")),
                infra(),
                project_root=tmp_path,
            )


class TestExternal:
    """Non-regression #9, at the database level."""

    def test_nothing_is_created_in_an_external_server(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, runner = make_manager({"PROD_DB_URL": "postgresql://prod/clients"})
        outcome = manager.ensure(
            database("clients", DatabaseInit(command="npm run db:init")),
            infra(mode=Mode.EXTERNAL),
            project_root=tmp_path,
        )
        assert outcome.skipped_external
        assert catalog.created == []
        assert runner.calls == [], "an external database must never be initialised"

    def test_the_url_comes_from_the_declared_environment_variable(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager({"PROD_DB_URL": "postgresql://prod.internal:5432/clients"})
        outcome = manager.ensure(database(), infra(mode=Mode.EXTERNAL), project_root=tmp_path)
        assert outcome.url == "postgresql://prod.internal:5432/clients"

    def test_a_missing_url_variable_is_an_actionable_error(
        self,
        catalog: InMemoryClient,
        make_manager: Callable[..., tuple[DatabaseManager, RecordingRunner]],
        tmp_path: Path,
    ) -> None:
        manager, _ = make_manager({})
        with pytest.raises(DatabaseError) as caught:
            manager.ensure(database(), infra(mode=Mode.EXTERNAL), project_root=tmp_path)
        assert "PROD_DB_URL" in str(caught.value)
        assert caught.value.hint is not None


class TestSqlIdentifierSafety:
    """SQL identifiers cannot be parameterised, so they are allowlisted."""

    @pytest.fixture
    def client(self, tmp_path: Path) -> SqlDatabaseClient:
        runner = RecordingRunner()
        context = InfraContext(
            spec=infra(),
            data_dir=tmp_path,
            log_dir=tmp_path,
            runtime_root=None,
            environment={},
            runner=runner,
        )
        return SqlDatabaseClient(
            context=context,
            runner=runner,
            list_argv=("psql", "-c", "SELECT datname FROM pg_database"),
            create_argv_template=("createdb", "{database}"),
        )

    @pytest.mark.parametrize(
        "name",
        [
            "clients; DROP DATABASE production; --",
            "clients'--",
            'clients"',
            "clients`",
            "clients ; SELECT 1",
            "clients\nDROP",
            "",
            "1clients",
            "a" * 64,
        ],
    )
    def test_a_dangerous_database_name_is_refused(
        self, client: SqlDatabaseClient, name: str
    ) -> None:
        with pytest.raises(DatabaseError):
            client.create_database(name)

    def test_a_normal_name_is_accepted(self, client: SqlDatabaseClient) -> None:
        client.create_database("customer_portal-v2")
        assert client.runner.ran("createdb customer_portal-v2")  # type: ignore[attr-defined]

    def test_the_list_command_output_is_parsed(self, client: SqlDatabaseClient) -> None:
        client.runner.default = CommandResult(  # type: ignore[attr-defined]
            argv=(), returncode=0, stdout="postgres\nclients\nlegacy\n"
        )
        assert client.list_databases() == ("clients", "legacy", "postgres")

    def test_a_failing_list_command_is_an_actionable_error(self, client: SqlDatabaseClient) -> None:
        client.runner.default = CommandResult(  # type: ignore[attr-defined]
            argv=(), returncode=2, stderr="could not connect to server"
        )
        with pytest.raises(DatabaseError) as caught:
            client.list_databases()
        assert "could not connect" in str(caught.value)
        assert caught.value.hint is not None
        assert "devbox infra status" in caught.value.hint


class TestOutputParsing:
    def test_psql_unaligned_output(self) -> None:
        assert parse_database_list("postgres\nclients\n") == ("clients", "postgres")

    def test_aligned_output_with_headers_is_tolerated(self) -> None:
        raw = " Database\n----------\n clients\n legacy\n(2 rows)\n"
        assert parse_database_list(raw) == ("clients", "legacy")

    def test_empty_output_is_empty(self) -> None:
        assert parse_database_list("") == ()

    def test_duplicates_are_collapsed(self) -> None:
        assert parse_database_list("a\na\nb\n") == ("a", "b")
