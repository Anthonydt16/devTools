"""Rendering.

Two things make terminal output on Windows harder than it looks, and both are
handled here rather than scattered through the commands:

* **Encoding.** A ``cp1252`` console cannot print ``✓``.  Devbox probes what the
  stream can actually encode once, at start-up, and falls back to ``[OK]`` /
  ``[!!]`` rather than crashing with ``UnicodeEncodeError`` halfway through a
  status table — which is exactly what happens to tools that assume UTF-8.
* **Non-interactive use.** Colour is emitted only when the stream is a TTY,
  ``NO_COLOR`` is unset and ``TERM`` is not ``dumb``, so piping ``devbox ps``
  into a file in CI produces clean text.

No business logic lives here; the renderer only formats values it is given.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO

__all__ = ["Renderer", "Style", "Symbols", "format_duration", "format_size"]


class Style(StrEnum):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


@dataclass(frozen=True, slots=True)
class Symbols:
    """Status markers, chosen to match what the console can encode."""

    ok: str = "✓"
    fail: str = "✗"
    warn: str = "!"
    pending: str = "·"
    arrow: str = "→"

    @classmethod
    def for_stream(cls, stream: TextIO) -> Symbols:
        """Unicode markers when the stream can encode them, ASCII otherwise."""
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            "✓✗·→".encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return cls(ok="[OK]", fail="[!!]", warn="[--]", pending="...", arrow="->")
        return cls()


class Renderer:
    """Writes human-readable output to a stream."""

    def __init__(
        self,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        *,
        colour: bool | None = None,
        quiet: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.quiet = quiet
        env = os.environ if environ is None else environ
        self.colour = _should_colour(self.stream, env) if colour is None else colour
        self.symbols = Symbols.for_stream(self.stream)

    # -- primitives ----------------------------------------------------------
    def _paint(self, text: str, *styles: Style) -> str:
        if not self.colour or not styles:
            return text
        return "".join(styles) + text + Style.RESET

    def write(self, text: str = "") -> None:
        if self.quiet:
            return
        self._safe_write(self.stream, text + "\n")

    def error(self, text: str = "") -> None:
        """Errors are written even in quiet mode: silence is not a diagnosis."""
        self._safe_write(self.error_stream, text + "\n")

    def _safe_write(self, stream: TextIO, text: str) -> None:
        """Write, degrading rather than raising on an unencodable character.

        A service name with an accent must not be able to crash the renderer on a
        legacy code page.
        """
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, errors="replace").decode(encoding))

    # -- structure -----------------------------------------------------------
    def heading(self, text: str) -> None:
        self.write()
        self.write(self._paint(text, Style.BOLD))

    def success(self, text: str, *, indent: int = 2) -> None:
        self.write(" " * indent + self._paint(self.symbols.ok, Style.GREEN) + " " + text)

    def failure(self, text: str, *, indent: int = 2) -> None:
        self.write(" " * indent + self._paint(self.symbols.fail, Style.RED) + " " + text)

    def warning(self, text: str, *, indent: int = 2) -> None:
        self.write(" " * indent + self._paint(self.symbols.warn, Style.YELLOW) + " " + text)

    def pending(self, text: str, *, indent: int = 2) -> None:
        self.write(" " * indent + self._paint(self.symbols.pending, Style.DIM) + " " + text)

    def detail(self, text: str, *, indent: int = 4) -> None:
        self.write(" " * indent + self._paint(text, Style.DIM))

    def table(self, rows: Sequence[Sequence[str]], *, indent: int = 2) -> None:
        """A left-aligned table with columns sized to their contents."""
        if not rows:
            return
        width = max(len(row) for row in rows)
        padded = [[*row, *([""] * (width - len(row)))] for row in rows]
        widths = [max(len(row[column]) for row in padded) for column in range(width)]
        for row in padded:
            cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
            self.write(" " * indent + "  ".join(cells).rstrip())

    # -- errors --------------------------------------------------------------
    def error_block(
        self,
        title: str,
        *,
        fields: Sequence[tuple[str, str]] = (),
        hint: str = "",
        body: str = "",
    ) -> None:
        """The standard failure block, on stderr.

        Shape is fixed so a developer learns where to look once:
        title, then labelled facts, then the captured output, then the next step.
        """
        self.error(self._paint(f"{self.symbols.fail} {title}", Style.RED))
        if fields:
            label_width = max(len(label) for label, _ in fields)
            for label, value in fields:
                if value:
                    self.error(f"  {(label + ':').ljust(label_width + 1)} {value}")
        if body:
            self.error("  stderr:")
            for line in body.splitlines():
                self.error(f"    {line}")
        if hint:
            self.error("")
            self.error(f"  {self._paint(hint, Style.CYAN)}")


def _should_colour(stream: TextIO, environ: Mapping[str, str]) -> bool:
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR"):
        return True
    if environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def format_duration(seconds: float | None) -> str:
    """``45s``, ``12m``, ``3h 5m`` — short enough for a table column."""
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - unreachable, kept for exhaustiveness
