"""Shared local infrastructure: one PostgreSQL and one MySQL per machine, not per project.

Importing this package registers the built-in providers.  A third-party provider
registers itself the same way — see ``tests/unit/test_extensibility.py``.
"""

from devbox.infra import mysql as _mysql  # noqa: F401 - registers the provider
from devbox.infra import postgres as _postgres  # noqa: F401 - registers the provider
from devbox.infra.manager import InfraManager, InfraStatus, StartResult, StopResult
from devbox.infra.provider import (
    DatabaseClient,
    InfraContext,
    InfraProvider,
    get_provider,
    known_provider_implementations,
    register_provider,
)

__all__ = [
    "DatabaseClient",
    "InfraContext",
    "InfraManager",
    "InfraProvider",
    "InfraStatus",
    "StartResult",
    "StopResult",
    "get_provider",
    "known_provider_implementations",
    "register_provider",
]
