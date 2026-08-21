"""PostgreSQL as shared local infrastructure.

Runs a **user-space** PostgreSQL: the binaries come from a runtime Devbox
downloaded and verified, the data directory lives under
``%LOCALAPPDATA%\\Devbox\\infra\\postgres\\<instance>\\data``, and the server runs
as the logged-in user.  No Windows service is registered, no installer runs, and
nothing needs administrator rights.

One instance serves every project on the machine.  ``devbox down`` on a project
never touches it, and its data survives — that is the whole point of shared
infrastructure and it is what non-regression tests #2 and #3 check.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from devbox.catalogs.providers import POSTGRES, ProviderDescriptor
from devbox.config.models import Healthcheck
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
)
from devbox.infra.sql import SqlDatabaseClient

__all__ = ["PostgresProvider"]

_TRUST_NOTE = (
    "The instance listens on 127.0.0.1 only and authenticates the local user; "
    "it is a development database and must never be exposed on a network."
)


@dataclass(frozen=True, slots=True)
class PostgresProvider:
    """Manages a user-space PostgreSQL instance."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        return POSTGRES

    # -- lifecycle -----------------------------------------------------------
    def needs_initialisation(self, context: InfraContext) -> bool:
        """``PG_VERSION`` is the file initdb writes last; its absence means empty."""
        return not (context.data_dir / "PG_VERSION").is_file()

    def initialise(self, context: InfraContext) -> None:
        """Run ``initdb`` once, into an empty data directory.

        Guarded by :meth:`needs_initialisation` at the call site *and* here:
        running ``initdb`` over an existing cluster would destroy every project's
        databases, so the check is worth making twice.
        """
        if not self.needs_initialisation(context):
            return
        context.data_dir.mkdir(parents=True, exist_ok=True)
        username = context.spec.username or POSTGRES.default_username
        result = context.require_runner().run(
            [
                context.executable("initdb"),
                "--pgdata",
                str(context.data_dir),
                "--username",
                username,
                "--encoding",
                "UTF8",
                "--locale",
                "C",
                # No superuser password: loopback-only, local user. See _TRUST_NOTE.
                "--auth-local",
                "trust",
                "--auth-host",
                "trust",
            ],
            env=dict(context.environment),
            timeout=300.0,
        )
        result.raise_for_status(what=f"initdb for {context.name}")

    def start_command(self, context: InfraContext) -> Sequence[str]:
        """Run ``postgres`` in the foreground so Devbox supervises it directly.

        ``pg_ctl start`` would daemonise and hand back a PID Devbox does not own,
        which breaks both process-tree shutdown and the ownership check.
        """
        return (
            context.executable("postgres"),
            "-D",
            str(context.data_dir),
            "-p",
            str(context.port),
            "-h",
            context.host,
            "-c",
            f"log_directory={context.log_dir}",
            "-c",
            "logging_collector=on",
        )

    def stop_command(self, context: InfraContext) -> Sequence[str] | None:
        """``pg_ctl stop -m fast`` checkpoints; a signal forces recovery next start."""
        return (
            context.executable("pg_ctl"),
            "stop",
            "-D",
            str(context.data_dir),
            "-m",
            "fast",
            "-w",
            "-t",
            "30",
        )

    def healthcheck(self, context: InfraContext) -> Healthcheck:
        return default_healthcheck(context)

    # -- databases -----------------------------------------------------------
    def connection_url(
        self,
        context: InfraContext,
        database: str,
        *,
        username: str = "",
        password: str = "",
    ) -> str:
        return POSTGRES.connection_url(
            host=context.host,
            port=context.port,
            database=database,
            username=username or context.spec.username or POSTGRES.default_username,
            password=password,
        )

    def database_client(self, context: InfraContext) -> DatabaseClient:
        username = context.spec.username or POSTGRES.default_username
        common = (
            "--host",
            context.host,
            "--port",
            str(context.port),
            "--username",
            username,
            "--no-password",
        )
        return SqlDatabaseClient(
            context=context,
            runner=context.require_runner(),
            list_argv=(
                context.executable("psql"),
                *common,
                "--dbname",
                "postgres",
                # -A -t: unaligned, tuples only -> one database name per line.
                "-A",
                "-t",
                "--command",
                "SELECT datname FROM pg_database WHERE datistemplate = false",
            ),
            create_argv_template=(
                context.executable("createdb"),
                *common,
                "--owner",
                "{owner}",
                "{database}",
            ),
        )


register_provider(PostgresProvider())
