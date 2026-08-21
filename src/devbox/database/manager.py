"""Logical databases inside a shared server.

Several projects share one PostgreSQL instance and each owns a few databases in
it.  This module answers one question per database — *does it exist?* — and acts
on the answer under three rules that are not negotiable:

1. **An existing database is never touched.**  Not recreated, not migrated, not
   re-initialised.  It may hold another project's data, or three weeks of the
   current project's.  Non-regression test #3.
2. **``init.if_missing`` runs exactly once**, in the run that created the
   database.  A second ``devbox up`` finds the database present and does nothing.
3. **Nothing is created inside an ``external`` server.**  Recette and production
   databases are provisioned by whoever owns them; Devbox reads their URL and
   stops there.

A failed ``init`` is reported with the command's own output, and the database is
left in place — deleting it would destroy whatever the half-finished
initialisation did manage to write, and hide the reason it failed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from devbox.config.models import DatabaseSpec, InfrastructureSpec, Mode
from devbox.errors import DatabaseError
from devbox.infra.provider import DatabaseClient, InfraContext, get_provider
from devbox.process.runner import CommandRunner, SubprocessRunner
from devbox.security.command import split_command

__all__ = ["DatabaseManager", "DatabaseOutcome", "ExternalUrls"]


@dataclass(frozen=True, slots=True)
class DatabaseOutcome:
    """What happened to one logical database — reported, never inferred."""

    name: str
    existed: bool
    created: bool
    initialised: bool
    skipped_external: bool = False
    url: str = ""
    detail: str = ""

    def render(self) -> str:
        if self.skipped_external:
            return f"{self.name} external"
        if self.created and self.initialised:
            return f"{self.name} created and initialised"
        if self.created:
            return f"{self.name} created"
        return f"{self.name} ready"


@dataclass(frozen=True, slots=True)
class ExternalUrls:
    """Connection URLs read from the environment for ``external`` servers."""

    values: Mapping[str, str]

    def require(self, spec: InfrastructureSpec) -> str:
        variable = spec.url_env or ""
        url = self.values.get(variable, "")
        if not url:
            msg = (
                f"infrastructure {spec.name!r} is external and expects its connection URL in "
                f"the environment variable {variable!r}, which is not set"
            )
            raise DatabaseError(
                msg,
                hint=(
                    f"Set it before running Devbox: $env:{variable}='...' (PowerShell), "
                    "or switch this environment to mode: managed."
                ),
            )
        return url


class DatabaseManager:
    """Ensures the logical databases a project declares are present and usable."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        context_factory: Callable[[InfrastructureSpec], InfraContext],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._context_for = context_factory
        self._environ: Mapping[str, str] = dict(environ or {})

    # -- inspection ----------------------------------------------------------
    def client_for(self, spec: InfrastructureSpec) -> DatabaseClient:
        provider = get_provider(spec.provider)
        return provider.database_client(self._context_for(spec))

    def connection_url(self, database: DatabaseSpec, infrastructure: InfrastructureSpec) -> str:
        """The URL to inject into services for this database.

        For an ``external`` server the URL comes from the environment variable
        the configuration names; Devbox appends the database name only when the
        variable clearly carries a bare server URL.
        """
        if infrastructure.mode is Mode.EXTERNAL:
            return ExternalUrls(self._environ).require(infrastructure)
        provider = get_provider(infrastructure.provider)
        password = self._password_for(database, infrastructure)
        return provider.connection_url(
            self._context_for(infrastructure),
            database.database,
            username=database.username or infrastructure.username or "",
            password=password,
        )

    def _password_for(self, database: DatabaseSpec, infrastructure: InfrastructureSpec) -> str:
        variable = database.password_env or infrastructure.password_env
        if not variable:
            return ""
        return self._environ.get(variable, "")

    # -- reconciliation ------------------------------------------------------
    def ensure(
        self,
        database: DatabaseSpec,
        infrastructure: InfrastructureSpec,
        *,
        project_root: Path,
        service_environment: Mapping[str, str] | None = None,
    ) -> DatabaseOutcome:
        """Make sure *database* exists, creating and initialising it if missing."""
        if infrastructure.mode is Mode.EXTERNAL:
            # Devbox does not create anything in a server it does not own.
            return DatabaseOutcome(
                name=database.name,
                existed=True,
                created=False,
                initialised=False,
                skipped_external=True,
                url=ExternalUrls(self._environ).require(infrastructure),
                detail="external server: Devbox neither creates nor initialises databases here",
            )

        client = self.client_for(infrastructure)
        url = self.connection_url(database, infrastructure)

        if client.database_exists(database.database):
            # The single most important branch in this file: stop here.
            return DatabaseOutcome(
                name=database.name,
                existed=True,
                created=False,
                initialised=False,
                url=url,
                detail="already exists; left untouched",
            )

        owner = database.username or infrastructure.username or ""
        client.create_database(database.database, owner=owner)

        initialised = False
        if database.init is not None:
            self._run_init(
                database,
                project_root=project_root,
                url=url,
                service_environment=service_environment or {},
            )
            initialised = True

        return DatabaseOutcome(
            name=database.name,
            existed=False,
            created=True,
            initialised=initialised,
            url=url,
        )

    def ensure_all(
        self,
        databases: Sequence[DatabaseSpec],
        infrastructure: Mapping[str, InfrastructureSpec],
        *,
        project_root: Path,
        service_environment: Mapping[str, str] | None = None,
    ) -> tuple[DatabaseOutcome, ...]:
        outcomes: list[DatabaseOutcome] = []
        for database in databases:
            host = infrastructure.get(database.infrastructure)
            if host is None:  # pragma: no cover - config validation rejects this earlier
                msg = f"database {database.name!r} refers to unknown infrastructure"
                raise DatabaseError(msg)
            outcomes.append(
                self.ensure(
                    database,
                    host,
                    project_root=project_root,
                    service_environment=service_environment,
                )
            )
        return tuple(outcomes)

    def _run_init(
        self,
        database: DatabaseSpec,
        *,
        project_root: Path,
        url: str,
        service_environment: Mapping[str, str],
    ) -> None:
        """Run ``init.if_missing.command`` once, in the project."""
        init = database.init
        if init is None:  # pragma: no cover - guarded by the caller
            return
        argv = split_command(init.command, where=f"databases.{database.name}.init.if_missing")
        working_dir = project_root / (init.working_dir or ".")
        if not working_dir.is_dir():
            msg = (
                f"databases.{database.name}.init.if_missing.working_dir does not exist: "
                f"{working_dir}"
            )
            raise DatabaseError(msg)

        environment = {
            **self._environ,
            **service_environment,
            "DATABASE_URL": url,
            "DEVBOX_DATABASE_NAME": database.database,
        }
        result = self._runner.run(
            argv,
            cwd=working_dir,
            env=environment,
            timeout=600.0,
        )
        if result.ok:
            return
        detail = (result.stderr or result.stdout).strip()
        tail = "\n  output: " + detail.replace("\n", "\n          ") if detail else ""
        msg = (
            f"initialisation of database {database.name!r} failed with exit code "
            f"{result.returncode}\n  command: {result.command_line()}{tail}"
        )
        raise DatabaseError(
            msg,
            hint=(
                f"The database {database.database!r} was created and is left in place. "
                "Fix the command and run 'devbox up' again, or drop the database to "
                "re-run initialisation from scratch."
            ),
        )
