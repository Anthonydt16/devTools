"""Registry of shared-infrastructure providers.

The orchestrator knows nothing about PostgreSQL or MySQL specifically: it asks
this catalog for a provider by name and drives it through
:class:`~devbox.infra.provider.InfraProvider`.  Adding ``company-redis`` is a new
entry here plus a provider class — no change to the core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "ProviderDescriptor",
    "known_providers",
    "provider_descriptor",
    "register_provider_descriptor",
    "unregister_provider_descriptor",
]


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Static facts about an infrastructure provider, known before it is started."""

    name: str
    default_port: int
    default_version: str = ""
    url_scheme: str = ""
    default_username: str = ""
    default_database: str = ""
    runtime_kind: str = ""
    """Runtime that must be installed for ``managed`` mode; empty means none."""

    supports_managed: bool = True
    options: Mapping[str, str] = field(default_factory=dict)

    def connection_url(
        self,
        *,
        host: str,
        port: int,
        database: str,
        username: str = "",
        password: str = "",
    ) -> str:
        scheme = self.url_scheme or self.name
        user = username or self.default_username
        credentials = ""
        if user:
            credentials = f"{user}:{password}@" if password else f"{user}@"
        return f"{scheme}://{credentials}{host}:{port}/{database}"


_PROVIDERS: dict[str, ProviderDescriptor] = {}


def register_provider_descriptor(descriptor: ProviderDescriptor) -> ProviderDescriptor:
    _PROVIDERS[descriptor.name] = descriptor
    return descriptor


def provider_descriptor(name: str) -> ProviderDescriptor | None:
    return _PROVIDERS.get(name)


def known_providers() -> frozenset[str]:
    return frozenset(_PROVIDERS)


def unregister_provider_descriptor(name: str) -> None:
    """Test helper: undo a registration so provider state does not leak."""
    _PROVIDERS.pop(name, None)


POSTGRES = register_provider_descriptor(
    ProviderDescriptor(
        name="postgres",
        default_port=5432,
        default_version="17",
        url_scheme="postgresql",
        default_username="devbox",
        default_database="postgres",
        runtime_kind="postgres",
    )
)

MYSQL = register_provider_descriptor(
    ProviderDescriptor(
        name="mysql",
        default_port=3306,
        default_version="8.4",
        url_scheme="mysql",
        default_username="root",
        default_database="mysql",
        runtime_kind="mysql",
    )
)
