"""Shared machinery for SQL-server providers.

PostgreSQL and MySQL differ in their binaries and their catalog queries, not in
their shape: initialise a data directory, run a server in the foreground, ask the
catalog whether a database exists, create it if not.  What is common lives here;
what differs is a handful of small methods in each provider.

Every command is built as an argv list and executed through a
:class:`~devbox.process.runner.CommandRunner`, which means the arguments Devbox
constructs are unit-tested exactly, on a machine with neither server installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from devbox.errors import DatabaseError
from devbox.infra.provider import InfraContext
from devbox.process.runner import CommandRunner

__all__ = ["SqlDatabaseClient", "parse_database_list"]


def parse_database_list(output: str) -> tuple[str, ...]:
    """Parse one-name-per-line output from a database client.

    Blank lines, headers, row-count footers and surrounding whitespace are all
    discarded; what remains is sorted so callers get a stable answer.
    """
    names: list[str] = []
    for raw in output.splitlines():
        name = raw.strip().strip("|").strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered.startswith(("---", "===", "(", "database", "+")) or lowered.endswith(" rows)"):
            continue
        names.append(name)
    return tuple(sorted(set(names)))


@dataclass(frozen=True, slots=True)
class SqlDatabaseClient:
    """Inspects and creates databases by running the server's own CLI client.

    Shelling out to ``psql``/``mysql`` rather than adding ``psycopg`` or
    ``mysqlclient`` is deliberate: those wheels need a compiler or bundled
    libraries on Windows, and Devbox is meant to be installable by a standard
    user with ``pip install`` and nothing else.  The client binaries ship *inside*
    the runtime Devbox already installed and verified.
    """

    context: InfraContext
    runner: CommandRunner
    list_argv: tuple[str, ...]
    create_argv_template: tuple[str, ...]
    quote_identifier: str = '"'

    def list_databases(self) -> tuple[str, ...]:
        result = self.runner.run(
            self.list_argv,
            env=dict(self.context.environment),
            timeout=30.0,
        )
        if not result.ok:
            detail = (result.stderr or result.stdout).strip()
            msg = (
                f"cannot list databases on {self.context.name} "
                f"({self.context.host}:{self.context.port}): {detail or 'no output'}"
            )
            raise DatabaseError(
                msg,
                hint="Is the instance running? Try 'devbox infra status'.",
            )
        return parse_database_list(result.stdout)

    def database_exists(self, name: str) -> bool:
        return name in self.list_databases()

    def create_database(self, name: str, *, owner: str = "") -> None:
        """Create a database.

        The name is validated rather than escaped.  SQL identifiers cannot be
        parameterised, so the only safe rule is a strict allowlist: anything that
        is not ``[A-Za-z0-9_$-]`` is refused before a statement is built.
        """
        _validate_identifier(name)
        if owner:
            _validate_identifier(owner)
        argv = tuple(
            part.replace("{database}", name).replace("{owner}", owner)
            for part in self.create_argv_template
            if not (part == "{owner}" and not owner)
        )
        result = self.runner.run(argv, env=dict(self.context.environment), timeout=60.0)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip()
            msg = f"cannot create database {name!r} on {self.context.name}: {detail or 'no output'}"
            raise DatabaseError(msg)


_ALLOWED_IDENTIFIER_EXTRA = frozenset("_-$")


def _validate_identifier(name: str) -> None:
    """Refuse anything that could change the meaning of a SQL statement.

    ``clients; DROP DATABASE production; --`` never reaches a server.
    """
    if not name:
        msg = "a database name must not be empty"
        raise DatabaseError(msg)
    if len(name) > 63:
        msg = f"database name {name!r} is too long (max 63 characters)"
        raise DatabaseError(msg)
    if name[0].isdigit():
        msg = f"invalid database name {name!r}: it must not start with a digit"
        raise DatabaseError(msg)
    bad = [
        char
        for char in name
        if not (char.isalnum() and char.isascii()) and char not in _ALLOWED_IDENTIFIER_EXTRA
    ]
    if bad:
        msg = (
            f"invalid database name {name!r}: {bad[0]!r} is not allowed "
            "(letters, digits, '_', '-' and '$' only)"
        )
        raise DatabaseError(
            msg,
            hint="SQL identifiers cannot be parameterised, so Devbox refuses "
            "anything it cannot prove is safe.",
        )


def credentials_environment(
    base: Sequence[tuple[str, str]],
    *,
    password: str,
    password_variable: str,
) -> dict[str, str]:
    """Build the environment carrying a password.

    Passwords go through an environment variable, never through the command
    line: ``psql --password=...`` would be visible to every other process on the
    machine in the process list.
    """
    env = dict(base)
    if password:
        env[password_variable] = password
    return env
