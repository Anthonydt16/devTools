"""Reading service logs.

Children write raw bytes into ``.devbox/logs/<service>.{out,err}.log``.  Reading
them back on Windows is the interesting part:

* a modern tool writes UTF-8;
* ``cmd.exe`` and many Windows programs write the ANSI code page — ``cp1252`` in
  Western Europe;
* PowerShell's ``>`` redirection writes UTF-16 LE with a BOM;
* an editor may have added a UTF-8 BOM.

So decoding is a cascade: BOM sniffing first (it is authoritative), then strict
UTF-8, then the configured fallback code page, and finally UTF-8 with
replacement characters.  A log line is *always* returned — an unreadable byte
must never be the reason a developer cannot see why their service died.

Reading is streaming and bounded: ``--tail`` seeks from the end rather than
loading a 500 MB log into memory.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

__all__ = ["LogLine", "LogReader", "LogStream", "decode_log_bytes", "follow_file"]

_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

DEFAULT_FALLBACK_ENCODING: Final = "cp1252"
"""Windows-1252: the ANSI code page on a French/Western European Windows."""

_TAIL_BLOCK: Final = 64 * 1024


class LogStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class LogLine:
    """One decoded line, tagged with the stream it came from."""

    service: str
    stream: LogStream
    text: str

    def render(self, *, prefix: bool = True) -> str:
        if not prefix:
            return self.text
        marker = "err" if self.stream is LogStream.STDERR else "out"
        return f"{self.service} | {marker} | {self.text}"


def decode_log_bytes(data: bytes, *, fallback: str = DEFAULT_FALLBACK_ENCODING) -> str:
    """Decode log bytes, never raising.

    BOM wins, then strict UTF-8, then the fallback code page, then UTF-8 with
    replacement characters so something is always shown.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            # utf-16-le / utf-32-le keep the BOM as a zero-width space; strip it
            # so a PowerShell-redirected log does not start with a stray U+FEFF.
            return data.decode(encoding, errors="replace").lstrip("\ufeff")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(fallback)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class LogReader:
    """Reads one service's stdout and stderr logs."""

    service: str
    stdout_path: Path
    stderr_path: Path
    fallback_encoding: str = DEFAULT_FALLBACK_ENCODING

    def available(self) -> tuple[bool, bool]:
        return self.stdout_path.is_file(), self.stderr_path.is_file()

    def read(
        self,
        *,
        streams: Sequence[LogStream] = (LogStream.STDOUT, LogStream.STDERR),
        tail: int | None = None,
    ) -> list[LogLine]:
        """Return decoded lines, optionally only the last *tail* per stream."""
        lines: list[LogLine] = []
        for stream in streams:
            path = self.stdout_path if stream is LogStream.STDOUT else self.stderr_path
            for text in self._read_one(path, tail):
                lines.append(LogLine(service=self.service, stream=stream, text=text))
        return lines

    def tail_text(self, stream: LogStream, *, lines: int = 20) -> str:
        """Convenience for error blocks: the last *lines* of one stream."""
        path = self.stdout_path if stream is LogStream.STDOUT else self.stderr_path
        return "\n".join(self._read_one(path, lines))

    def _read_one(self, path: Path, tail: int | None) -> list[str]:
        if not path.is_file():
            return []
        data = _read_tail_bytes(path, tail) if tail is not None else path.read_bytes()
        text = decode_log_bytes(data, fallback=self.fallback_encoding)
        split = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if split and split[-1] == "":
            split.pop()
        if tail is not None and len(split) > tail:
            return split[-tail:]
        return split

    def follow(
        self,
        *,
        streams: Sequence[LogStream] = (LogStream.STDOUT, LogStream.STDERR),
        poll_interval: float = 0.25,
        stop_after: float | None = None,
        from_start: bool = False,
    ) -> Iterator[LogLine]:
        """Yield lines as they are appended, like ``tail -f``.

        Not a generator function: the starting offsets are captured **when
        follow() is called**, not when the first line is pulled.  Otherwise
        output written between the call and the first ``next()`` would be
        silently skipped — which is exactly the window in which a service
        being started writes its first lines.

        *stop_after* bounds the wait; without it the caller breaks out itself
        (the CLI stops on Ctrl-C).  Having a bounded form is what makes
        ``--follow`` testable without a test that can hang forever.
        """
        followers = {
            stream: _Follower(
                self.stdout_path if stream is LogStream.STDOUT else self.stderr_path,
                from_start=from_start,
                fallback=self.fallback_encoding,
            )
            for stream in streams
        }
        return self._follow_loop(followers, poll_interval, stop_after)

    def _follow_loop(
        self,
        followers: Mapping[LogStream, _Follower],
        poll_interval: float,
        stop_after: float | None,
    ) -> Iterator[LogLine]:
        deadline = None if stop_after is None else time.monotonic() + stop_after
        while True:
            produced = False
            for stream, follower in followers.items():
                for text in follower.poll():
                    produced = True
                    yield LogLine(service=self.service, stream=stream, text=text)
            if deadline is not None and time.monotonic() >= deadline:
                return
            if not produced:
                time.sleep(poll_interval)


class _Follower:
    """Tracks the read position of one growing file."""

    __slots__ = ("_buffer", "_fallback", "_offset", "path")

    def __init__(self, path: Path, *, from_start: bool, fallback: str) -> None:
        self.path = path
        self._fallback = fallback
        self._buffer = b""
        self._offset = 0
        if not from_start and path.is_file():
            self._offset = path.stat().st_size

    def poll(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # Truncated or rotated: start again from the beginning.
            self._offset = 0
            self._buffer = b""
        if size == self._offset:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(size - self._offset)
        except OSError:
            return []
        self._offset += len(chunk)
        self._buffer += chunk
        *complete, self._buffer = self._buffer.split(b"\n")
        return [decode_log_bytes(line.rstrip(b"\r"), fallback=self._fallback) for line in complete]


def _read_tail_bytes(path: Path, lines: int) -> bytes:
    """Read backwards until *lines* newlines are found, or the file starts.

    Bounded memory: a 2 GB log costs one 64 KB block per line batch, not 2 GB.
    """
    if lines <= 0:
        return b""
    with path.open("rb") as handle:
        handle.seek(0, io.SEEK_END)
        end = handle.tell()
        position = end
        found = 0
        block = b""
        while position > 0 and found <= lines:
            step = min(_TAIL_BLOCK, position)
            position -= step
            handle.seek(position)
            chunk = handle.read(step)
            block = chunk + block
            found = block.count(b"\n")
        return block


def follow_file(
    path: Path,
    *,
    poll_interval: float = 0.25,
    stop_after: float | None = None,
    from_start: bool = False,
    fallback: str = DEFAULT_FALLBACK_ENCODING,
) -> Iterator[str]:
    """Follow a single file.  Used by ``devbox infra logs``.

    Like :meth:`LogReader.follow`, the starting offset is captured eagerly.
    """
    follower = _Follower(path, from_start=from_start, fallback=fallback)
    return _follow_file_loop(follower, poll_interval, stop_after)


def _follow_file_loop(
    follower: _Follower, poll_interval: float, stop_after: float | None
) -> Iterator[str]:
    deadline = None if stop_after is None else time.monotonic() + stop_after
    while True:
        produced = follower.poll()
        yield from produced
        if deadline is not None and time.monotonic() >= deadline:
            return
        if not produced:
            time.sleep(poll_interval)
