"""MySQL as shared local infrastructure.

Same model as PostgreSQL: binaries from a verified runtime, data under
``%LOCALAPPDATA%\\Devbox\\infra\\mysql\\<instance>\\data``, running as the
logged-in user with no service registration and no elevation.

The differences from PostgreSQL are confined to this file — initialisation flags,
the catalog query, the client binaries — which is exactly what the provider
protocol exists to allow.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from devbox.catalogs.providers import MYSQL, ProviderDescriptor
from devbox.config.models import Healthcheck
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
)
from devbox.infra.sql import SqlDatabaseClient

__all__ = ["MysqlProvider"]


@dataclass(frozen=True, slots=True)
class MysqlProvider:
    """Manages a user-space MySQL instance."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        return MYSQL

    def needs_initialisation(self, context: InfraContext) -> bool:
        """``mysql/`` is the system schema directory ``--initialize`` creates."""
        return not (context.data_dir / "mysql").is_dir()

    def initialise(self, context: InfraContext) -> None:
        if not self.needs_initialisation(context):
            return
        context.data_dir.mkdir(parents=True, exist_ok=True)
        result = context.require_runner().run(
            [
                context.executable("mysqld"),
                f"--datadir={context.data_dir}",
                # Loopback-only development instance; see docs/security.md.
                "--initialize-insecure",
                f"--log-error={context.log_dir / 'mysql-init.log'}",
            ],
            env=dict(context.environment),
            timeout=300.0,
        )
        result.raise_for_status(what=f"mysqld --initialize for {context.name}")

    def start_command(self, context: InfraContext) -> Sequence[str]:
        return (
            context.executable("mysqld"),
            f"--datadir={context.data_dir}",
            f"--port={context.port}",
            f"--bind-address={context.host}",
            f"--log-error={context.log_dir / 'mysql.log'}",
            "--console",
        )

    def stop_command(self, context: InfraContext) -> Sequence[str] | None:
        return (
            context.executable("mysqladmin"),
            "--host",
            context.host,
            "--port",
            str(context.port),
            "--user",
            context.spec.username or MYSQL.default_username,
            "shutdown",
        )

    def healthcheck(self, context: InfraContext) -> Healthcheck:
        return default_healthcheck(context)

    def connection_url(
        self,
        context: InfraContext,
        database: str,
        *,
        username: str = "",
        password: str = "",
    ) -> str:
        return MYSQL.connection_url(
            host=context.host,
            port=context.port,
            database=database,
            username=username or context.spec.username or MYSQL.default_username,
            password=password,
        )

    def database_client(self, context: InfraContext) -> DatabaseClient:
        username = context.spec.username or MYSQL.default_username
        common = (
            "--host",
            context.host,
            "--port",
            str(context.port),
            "--user",
            username,
        )
        return SqlDatabaseClient(
            context=context,
            runner=context.require_runner(),
            list_argv=(
                context.executable("mysql"),
                *common,
                # --batch --skip-column-names: one database name per line.
                "--batch",
                "--skip-column-names",
                "--execute",
                "SHOW DATABASES",
            ),
            create_argv_template=(
                context.executable("mysqladmin"),
                *common,
                "create",
                "{database}",
            ),
            quote_identifier="`",
        )


register_provider(MysqlProvider())
