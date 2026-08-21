"""SHA256 helpers.

Digests are computed while data streams past — Devbox never loads a 60 MB
runtime archive into memory to hash it, and never hashes a file it has already
moved into place.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Final

from devbox.errors import IntegrityError

__all__ = ["CHUNK_SIZE", "StreamHasher", "sha256_bytes", "sha256_file", "verify_sha256"]

CHUNK_SIZE: Final = 256 * 1024


class StreamHasher:
    """Accumulates a SHA256 while bytes flow through it."""

    __slots__ = ("_digest", "bytes_read")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self.bytes_read += len(chunk)

    def feed(self, chunks: Iterable[bytes]) -> Iterator[bytes]:
        """Pass chunks through, hashing them on the way."""
        for chunk in chunks:
            self.update(chunk)
            yield chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """Digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(actual: str, expected: str, *, subject: str) -> None:
    """Raise :class:`IntegrityError` unless the two digests match.

    Comparison is case-insensitive on the hex text but exact on the value.  A
    mismatch is fatal by design: the caller must delete the artifact and must not
    execute it.
    """
    if actual.lower() == expected.lower():
        return
    msg = (
        f"integrity check failed for {subject}\n"
        f"  expected sha256: {expected}\n"
        f"  actual sha256:   {actual}"
    )
    raise IntegrityError(msg, expected=expected, actual=actual)
