"""``devbox.lock``: the reproducible resolution of ``devbox.yml``.

Deliberately free of any dependency on the runtime layer: resolution *produces* a
lockfile by querying a registry, so it lives in :mod:`devbox.runtime.resolver`,
one layer up.  Keeping the direction one-way is what stops the two packages from
importing each other.
"""

from devbox.lockfile.io import dump_lockfile, parse_lockfile, read_lockfile, write_lockfile
from devbox.lockfile.models import LOCK_VERSION, LockedRuntime, Lockfile, is_valid_sha256

__all__ = [
    "LOCK_VERSION",
    "LockedRuntime",
    "Lockfile",
    "dump_lockfile",
    "is_valid_sha256",
    "parse_lockfile",
    "read_lockfile",
    "write_lockfile",
]
