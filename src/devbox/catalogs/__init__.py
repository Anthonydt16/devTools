"""Registries of *kinds*: runtime families and infrastructure providers.

A deliberately dependency-free leaf package.  Configuration validation needs to
know which runtimes and providers exist, and the runtime and infrastructure
layers need the same answer — putting the catalogs anywhere else would make the
config layer depend on the layers it validates, which is how import cycles and
untestable coupling start.

Both catalogs are open: registering a new entry is a call, not a code change to
the orchestrator.  See ``tests/unit/test_extensibility.py``.
"""

from devbox.catalogs.providers import (
    ProviderDescriptor,
    known_providers,
    provider_descriptor,
    register_provider_descriptor,
    unregister_provider_descriptor,
)
from devbox.catalogs.runtime_kinds import (
    RuntimeKind,
    known_runtime_kinds,
    register_runtime_kind,
    runtime_kind,
    unregister_runtime_kind,
)

__all__ = [
    "ProviderDescriptor",
    "RuntimeKind",
    "known_providers",
    "known_runtime_kinds",
    "provider_descriptor",
    "register_provider_descriptor",
    "register_runtime_kind",
    "runtime_kind",
    "unregister_provider_descriptor",
    "unregister_runtime_kind",
]
