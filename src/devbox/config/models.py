"""Typed, immutable representation of ``devbox.yml``.

These objects are the *only* thing the rest of Devbox sees.  Nothing downstream
ever touches a raw dictionary parsed from YAML, so a field that reaches the
orchestrator has already been validated, defaulted and cross-checked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "SCHEMA_VERSION",
    "DatabaseInit",
    "DatabaseSpec",
    "Healthcheck",
    "HttpHealthcheck",
    "InfrastructureSpec",
    "Mode",
    "ProjectConfig",
    "RuntimeRequirement",
    "ServiceSpec",
    "TcpHealthcheck",
]

SCHEMA_VERSION: Final = 1
"""The only ``version:`` value this build of Devbox understands."""

DEFAULT_HOST: Final = "127.0.0.1"
"""Devbox binds to loopback only.  Exposing a dev database on 0.0.0.0 on a
corporate laptop is a security problem, not a feature."""


class Mode(StrEnum):
    """Who owns the lifecycle of a piece of infrastructure."""

    MANAGED = "managed"
    """Devbox starts and stops it, and owns its data directory."""

    EXTERNAL = "external"
    """It lives somewhere else.  Devbox may health-check it and inject its URL,
    but will never start, stop, migrate or create anything in it."""


@dataclass(frozen=True, slots=True, order=True)
class RuntimeRequirement:
    """One pinned runtime, e.g. ``node 18.19.0``."""

    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.name} {self.version}"

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class _HealthcheckTiming:
    interval: float = 1.0
    """Seconds between two attempts."""

    timeout: float = 30.0
    """Seconds allowed for a *single* attempt."""

    retries: int = 10
    """Attempts before declaring failure."""

    start_period: float = 0.0
    """Grace period before the first attempt; failures during it are not counted."""


@dataclass(frozen=True, slots=True)
class HttpHealthcheck(_HealthcheckTiming):
    """``GET <url>`` until the status code is acceptable."""

    url: str = ""
    method: str = "GET"
    expect_status: tuple[int, ...] = ()
    """Accepted status codes.  Empty means "any 2xx or 3xx"."""

    def describe(self) -> str:
        return f"HTTP {self.method} {self.url}"

    def accepts(self, status: int) -> bool:
        if self.expect_status:
            return status in self.expect_status
        return 200 <= status < 400


@dataclass(frozen=True, slots=True)
class TcpHealthcheck(_HealthcheckTiming):
    """Connect to ``host:port`` until the handshake succeeds."""

    host: str = DEFAULT_HOST
    port: int = 0

    def describe(self) -> str:
        return f"TCP {self.host}:{self.port}"


Healthcheck = HttpHealthcheck | TcpHealthcheck


@dataclass(frozen=True, slots=True)
class DatabaseInit:
    """One-shot initialisation for a logical database.

    Runs **only** when the database did not exist and Devbox created it.  An
    existing database is never re-initialised — see
    ``tests/unit/test_database_manager.py``.
    """

    command: str
    working_dir: str | None = None
    runtime: tuple[RuntimeRequirement, ...] = ()
    """Runtimes to put on ``PATH`` while running the init command (e.g. node)."""


@dataclass(frozen=True, slots=True)
class InfrastructureSpec:
    """A shared database server or comparable long-lived dependency.

    Shared is the point: one PostgreSQL instance on the machine serves every
    project, exactly like a real company environment.  See
    docs/shared-infrastructure.md.
    """

    name: str
    provider: str
    version: str = ""
    mode: Mode = Mode.MANAGED
    host: str = DEFAULT_HOST
    port: int | None = None
    url_env: str | None = None
    """External mode only: environment variable holding the connection URL."""

    username: str | None = None
    password_env: str | None = None
    """Name of an environment variable holding the password.  Never the password
    itself — no secret is ever stored in ``devbox.yml``."""

    healthcheck: Healthcheck | None = None
    options: Mapping[str, str] = field(default_factory=dict)
    """Provider-specific knobs, passed through untouched."""

    @property
    def is_managed(self) -> bool:
        return self.mode is Mode.MANAGED


@dataclass(frozen=True, slots=True)
class DatabaseSpec:
    """A logical database inside a shared infrastructure instance."""

    name: str
    provider: str
    infrastructure: str
    database: str
    username: str | None = None
    password_env: str | None = None
    init: DatabaseInit | None = None


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """An application process supervised by Devbox."""

    name: str
    command: str
    working_dir: str = "."
    runtime: tuple[RuntimeRequirement, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    healthcheck: Healthcheck | None = None
    port: int | None = None
    """Purely informational: shown in ``devbox ps``.  Inferred from a TCP
    healthcheck or an HTTP healthcheck URL when not given."""

    stop_timeout: float = 10.0
    """Seconds to wait for a graceful stop before terminating the tree."""

    @property
    def runtime_map(self) -> dict[str, str]:
        return {req.name: req.version for req in self.runtime}


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """A fully validated ``devbox.yml``."""

    version: int
    project: str
    environment: str
    services: Mapping[str, ServiceSpec] = field(default_factory=dict)
    infrastructure: Mapping[str, InfrastructureSpec] = field(default_factory=dict)
    databases: Mapping[str, DatabaseSpec] = field(default_factory=dict)
    source_path: str | None = None

    # -- convenience accessors ------------------------------------------------
    def service(self, name: str) -> ServiceSpec:
        try:
            return self.services[name]
        except KeyError:
            from devbox.errors import ConfigError

            known = ", ".join(sorted(self.services)) or "<none>"
            msg = f"unknown service {name!r}"
            raise ConfigError(msg, hint=f"Declared services: {known}") from None

    def infrastructure_spec(self, name: str) -> InfrastructureSpec:
        try:
            return self.infrastructure[name]
        except KeyError:
            from devbox.errors import ConfigError

            known = ", ".join(sorted(self.infrastructure)) or "<none>"
            msg = f"unknown infrastructure {name!r}"
            raise ConfigError(msg, hint=f"Declared infrastructure: {known}") from None

    def managed_infrastructure(self) -> tuple[InfrastructureSpec, ...]:
        """Infrastructure Devbox is allowed to start.

        ``external`` entries are excluded here and nowhere else, which is what
        guarantees that a production configuration never boots a local server.
        """
        return tuple(spec for spec in self.infrastructure.values() if spec.is_managed)

    def required_runtimes(self) -> tuple[RuntimeRequirement, ...]:
        """Every distinct runtime this project needs, sorted for reproducibility."""
        seen: set[RuntimeRequirement] = set()
        for service in self.services.values():
            seen.update(service.runtime)
        for database in self.databases.values():
            if database.init is not None:
                seen.update(database.init.runtime)
        return tuple(sorted(seen))

    def node_names(self) -> frozenset[str]:
        """Every name usable in ``depends_on``."""
        return frozenset(self.services) | frozenset(self.infrastructure) | frozenset(self.databases)

    def all_dependencies(self) -> Mapping[str, Sequence[str]]:
        """Adjacency list for the start-up graph.

        Databases implicitly depend on the infrastructure that hosts them, which
        is what makes ``depends_on: [company-postgres]`` on a service enough.
        """
        edges: dict[str, list[str]] = {}
        for name in self.infrastructure:
            edges[name] = []
        for name, database in self.databases.items():
            edges[name] = [database.infrastructure]
        for name, service in self.services.items():
            edges[name] = list(service.depends_on)
        return edges
