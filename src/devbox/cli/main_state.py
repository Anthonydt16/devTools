"""Access to the current CLI context.

A tiny indirection so command modules do not import ``cli.main`` (which imports
them), and so tests can inject a prepared context.
"""

from __future__ import annotations

from devbox.cli.context import CliContext, GlobalOptions

__all__ = ["current_context", "reset_context", "set_context"]

_STATE: dict[str, CliContext] = {}


def current_context() -> CliContext:
    existing = _STATE.get("context")
    return existing if existing is not None else CliContext.build(GlobalOptions())


def set_context(value: CliContext) -> None:
    _STATE["context"] = value


def reset_context() -> None:
    _STATE.pop("context", None)
