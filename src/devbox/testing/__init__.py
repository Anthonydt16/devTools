"""Reusable test doubles and fixtures, shipped with the package.

They live in ``src/`` rather than ``tests/`` for two reasons: ``devbox test``
uses them, and anyone can then exercise Devbox on a machine that has no Node, no
PostgreSQL and no network access.
"""

from devbox.testing.fake_runtime import (
    FakeArtifact,
    FakeRegistry,
    build_fake_registry,
    build_fake_runtime,
    package_fake_runtime,
)

__all__ = [
    "FakeArtifact",
    "FakeRegistry",
    "build_fake_registry",
    "build_fake_runtime",
    "package_fake_runtime",
]
