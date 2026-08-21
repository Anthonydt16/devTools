"""A working infrastructure provider backed by the fake runtime.

Real PostgreSQL cannot run in the Linux development container, and installing it
on a developer's machine to run the test suite would defeat the point of Devbox.
This provider fills the gap without weakening what is tested: it starts a **real
process** (the fake runtime's HTTP server), binds a **real port**, answers a
**real healthcheck**, and keeps its catalogue of databases in a **real file**
inside the data directory — so "an existing database is never re-initialised"
survives a stop, a restart and a second project, for real.

It is also the worked example for provider extensibility: this file adds a
provider to Devbox without changing one line of the orchestrator, the CLI or the
infrastructure manager.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from devbox.catalogs.providers import ProviderDescriptor, register_provider_descriptor
from devbox.config.models import Healthcheck
from devbox.errors import DatabaseError
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    default_healthcheck,
    register_provider,
)

__all__ = ["FAKEDB", "FakeDbClient", "FakeDbProvider", "install_fake_provider"]

FAKEDB = ProviderDescriptor(
    name="fakedb",
    default_port=6543,
    default_version="1.0",
    url_scheme="fakedb",
    default_username="devbox",
    default_database="fakedb",
    runtime_kind="fakedb",
)

_CATALOGUE = "databases.json"


@dataclass(frozen=True, slots=True)
class FakeDbClient:
    """A database catalogue persisted as JSON in the instance's data directory."""

    catalogue_path: Path

    def _load(self) -> list[str]:
        if not self.catalogue_path.is_file():
            return []
        try:
            document = json.loads(self.catalogue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"the fakedb catalogue at {self.catalogue_path} is unreadable: {exc}"
            raise DatabaseError(msg) from exc
        return [str(name) for name in document] if isinstance(document, list) else []

    def list_databases(self) -> tuple[str, ...]:
        return tuple(sorted(self._load()))

    def database_exists(self, name: str) -> bool:
        return name in self._load()

    def create_database(self, name: str, *, owner: str = "") -> None:
        del owner
        names = self._load()
        if name in names:  # pragma: no cover - the manager checks first
            return
        names.append(name)
        self.catalogue_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalogue_path.write_text(
            json.dumps(sorted(names), indent=2) + "\n", encoding="utf-8", newline="\n"
        )


@dataclass(frozen=True, slots=True)
class FakeDbProvider:
    """A shared server that behaves like a real one, without needing one."""

    python_executable: str = ""

    @property
    def descriptor(self) -> ProviderDescriptor:
        return FAKEDB

    def needs_initialisation(self, context: InfraContext) -> bool:
        return not (context.data_dir / "INITIALISED").is_file()

    def initialise(self, context: InfraContext) -> None:
        """Create the data directory once — the equivalent of ``initdb``."""
        if not self.needs_initialisation(context):
            return
        context.data_dir.mkdir(parents=True, exist_ok=True)
        (context.data_dir / "INITIALISED").write_text(
            f"{context.spec.provider} {context.spec.version}\n", encoding="utf-8"
        )
        (context.data_dir / _CATALOGUE).write_text("[]\n", encoding="utf-8")

    def start_command(self, context: InfraContext) -> Sequence[str]:
        """Run the fake runtime's HTTP server, which binds the instance's port."""
        runtime_root = context.runtime_root
        if runtime_root is None:  # pragma: no cover - the manager always resolves one
            msg = "fakedb needs its runtime installed"
            raise DatabaseError(msg)
        return (
            self.python_executable or sys.executable,
            str(runtime_root / "runtime_main.py"),
            "serve",
            "--host",
            context.host,
            "--port",
            str(context.port),
            "--max-seconds",
            "900",
        )

    def stop_command(self, context: InfraContext) -> Sequence[str] | None:
        """No graceful CLI: Devbox signals the process, as it would any server."""
        del context
        return None

    def healthcheck(self, context: InfraContext) -> Healthcheck:
        return default_healthcheck(context, retries=60)

    def connection_url(
        self,
        context: InfraContext,
        database: str,
        *,
        username: str = "",
        password: str = "",
    ) -> str:
        return FAKEDB.connection_url(
            host=context.host,
            port=context.port,
            database=database,
            username=username or FAKEDB.default_username,
            password=password,
        )

    def database_client(self, context: InfraContext) -> DatabaseClient:
        return FakeDbClient(catalogue_path=context.data_dir / _CATALOGUE)


def install_fake_provider(*, python_executable: str = "") -> FakeDbProvider:
    """Register ``fakedb`` as a provider and as a runtime kind.

    Registration is the entire integration: nothing in the core knows this
    provider exists.
    """
    from devbox.catalogs.runtime_kinds import RuntimeKind, register_runtime_kind

    register_provider_descriptor(FAKEDB)
    register_runtime_kind(RuntimeKind(name="fakedb", bin_subdirs=(".",), probe_executable="fakedb"))
    provider = FakeDbProvider(python_executable=python_executable)
    register_provider(provider)
    return provider


def uninstall_fake_provider() -> None:
    """Remove the registrations so they do not leak between tests."""
    from devbox.catalogs.providers import unregister_provider_descriptor
    from devbox.catalogs.runtime_kinds import unregister_runtime_kind
    from devbox.infra.provider import unregister_provider

    unregister_provider("fakedb")
    unregister_provider_descriptor("fakedb")
    unregister_runtime_kind("fakedb")
