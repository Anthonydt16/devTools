"""CLI sub-commands.

Each module exposes ``register(app)`` and adds its commands to the Typer
application.  The order below is the order they appear in ``devbox --help``, so
it follows the order a user meets them: create a project, install, run it,
inspect it, then the machine-wide and packaging commands.
"""

from __future__ import annotations

import typer

from devbox.cli.commands import (
    build_cmd,
    exec_cmd,
    infra_cmd,
    init_cmd,
    install_cmd,
    logs_cmd,
    run_cmd,
    runtime_cmd,
    status_cmd,
    test_cmd,
    up_down_cmd,
)

__all__ = ["register_all"]

_MODULES = (
    init_cmd,
    install_cmd,
    up_down_cmd,
    status_cmd,
    logs_cmd,
    exec_cmd,
    run_cmd,
    infra_cmd,
    runtime_cmd,
    build_cmd,
    test_cmd,
)


def register_all(app: typer.Typer) -> None:
    """Attach every sub-command to *app*."""
    for module in _MODULES:
        module.register(app)
