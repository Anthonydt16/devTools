"""The shared-infrastructure provider contract.

A "provider" is a piece of long-lived shared infrastructure: PostgreSQL, MySQL,
and — the point of having a contract — anything added later, ``company-redis``
being the worked example.  The orchestrator never names a provider: it asks the
catalog for one and drives it through this protocol.  A new provider is a class
plus a registration; nothing in ``services/``, ``infra/manager.py`` or the CLI
changes.  ``tests/unit/test_extensibility.py`` proves it.

Shared is the operating model, not an optimisation:

* one PostgreSQL instance on the machine serves every project, exactly like a
  real company environment;
* its data lives in ``%LOCALAPPDATA%\\Devbox\\infra\\<provider>\\<instance>\\data``
  and is **never** deleted by ``devbox down``;
* ``devbox down`` on one project never stops an instance another project is
  using, and never stops one started by ``devbox infra up``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from devbox.catalogs.providers import ProviderDescriptor
from devbox.config.models import Healthcheck, InfrastructureSpec, TcpHealthcheck
from devbox.errors import InfrastructureError
from devbox.process.runner import CommandRunner

__all__ = [
    "InfraContext",
    "InfraProvider",
    "get_provider",
    "known_provider_implementations",
    "register_provider",
]


@dataclass(frozen=True, slots=True)
class InfraContext:
    """Everything a provider needs to manage one instance.

    Built by :class:`~devbox.infra.manager.InfraManager`, so a provider never
    has to know where Devbox keeps things or how runtimes are installed.
    """

    spec: InfrastructureSpec
    data_dir: Path
    log_dir: Path
    runtime_root: Path | None
    """Install root of the runtime backing this provider, when it has one."""

    environment: Mapping[str, str] = field(default_factory=dict)
    """The environment to hand to the provider's child processes, with the
    runtime's ``bin`` already on ``PATH``."""

    runner: CommandRunner | None = None
    """Runner for one-shot commands (``initdb``, ``createdb``)."""

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def host(self) -> str:
        return self.spec.host

    @property
    def port(self) -> int:
        if self.spec.port is None:
            msg = f"infrastructure {self.spec.name!r} has no port"
            raise InfrastructureError(msg)
        return self.spec.port

    def executable(self, program: str) -> str:
        """Resolve *program* inside this provider's runtime, not the machine PATH."""
        from devbox.host.platform_info import current_platform
        from devbox.security.command import resolve_executable

        entries = self.environment.get("PATH", "").split(current_platform().path_separator)
        found = resolve_executable(
            program,
            path_entries=[entry for entry in entries if entry],
            is_windows=current_platform().is_windows,
        )
        if found is None:
            where = self.runtime_root or "<no runtime installed>"
            msg = f"{self.spec.provider}: {program} was not found under {where}"
            raise InfrastructureError(
                msg,
                hint=f"Run 'devbox runtime install {self.spec.provider} {self.spec.version}'.",
            )
        return found

    def require_runner(self) -> CommandRunner:
        if self.runner is None:  # pragma: no cover - the manager always supplies one
            msg = "this operation needs a command runner"
            raise InfrastructureError(msg)
        return self.runner


@runtime_checkable
class InfraProvider(Protocol):
    """Lifecycle of one kind of shared infrastructure."""

    @property
    def descriptor(self) -> ProviderDescriptor:
        """Static facts: default port, URL scheme, backing runtime."""
        ...

    def needs_initialisation(self, context: InfraContext) -> bool:
        """True when the data directory has not been created yet.

        Must be *false* for an existing data directory: initialising over live
        data would destroy every project's databases.
        """
        ...

    def initialise(self, context: InfraContext) -> None:
        """Create the data directory (``initdb``, ``mysqld --initialize``).

        Called only when :meth:`needs_initialisation` says so.
        """
        ...

    def start_command(self, context: InfraContext) -> Sequence[str]:
        """argv that runs the server in the foreground, supervised by Devbox."""
        ...

    def healthcheck(self, context: InfraContext) -> Healthcheck:
        """How to tell the instance is accepting connections."""
        ...

    def stop_command(self, context: InfraContext) -> Sequence[str] | None:
        """argv for a clean shutdown, or ``None`` to signal the process instead.

        PostgreSQL much prefers ``pg_ctl stop -m fast`` to a signal: it
        checkpoints rather than forcing recovery on the next start.
        """
        ...

    def connection_url(
        self,
        context: InfraContext,
        database: str,
        *,
        username: str = "",
        password: str = "",
    ) -> str:
        """Connection URL for a logical database inside this instance."""
        ...

    def database_client(self, context: InfraContext) -> DatabaseClient:
        """Client used to inspect and create logical databases."""
        ...


@runtime_checkable
class DatabaseClient(Protocol):
    """Inspecting and creating logical databases inside a running instance."""

    def database_exists(self, name: str) -> bool: ...

    def create_database(self, name: str, *, owner: str = "") -> None: ...

    def list_databases(self) -> tuple[str, ...]: ...


_IMPLEMENTATIONS: dict[str, InfraProvider] = {}


def register_provider(provider: InfraProvider) -> InfraProvider:
    """Make a provider available to the orchestrator.

    Registration is all it takes: nothing in the core has a list of providers
    to update.
    """
    _IMPLEMENTATIONS[provider.descriptor.name] = provider
    return provider


def get_provider(name: str) -> InfraProvider:
    provider = _IMPLEMENTATIONS.get(name)
    if provider is None:
        known = ", ".join(sorted(_IMPLEMENTATIONS)) or "<none>"
        msg = f"no implementation registered for provider {name!r}"
        raise InfrastructureError(msg, hint=f"Available providers: {known}")
    return provider


def known_provider_implementations() -> frozenset[str]:
    return frozenset(_IMPLEMENTATIONS)


def unregister_provider(name: str) -> None:
    """Test helper: remove a provider so registrations do not leak between tests."""
    _IMPLEMENTATIONS.pop(name, None)


def default_healthcheck(context: InfraContext, *, retries: int = 30) -> Healthcheck:
    """A TCP check on the instance's own host and port.

    Adequate for anything that listens on a socket, which is why a new provider
    usually does not need to implement :meth:`InfraProvider.healthcheck` at all.
    """
    return TcpHealthcheck(
        host=context.host,
        port=context.port,
        interval=0.5,
        timeout=2.0,
        retries=retries,
        start_period=0.0,
    )
