"""``devbox up`` and ``devbox down``."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.cli.output import format_duration


def register(app: typer.Typer) -> None:
    app.command("up")(up)
    app.command("down")(down)


def up(
    services: Annotated[
        list[str] | None,
        typer.Argument(help="Services to start (default: everything)."),
    ] = None,
    no_install: Annotated[
        bool, typer.Option("--no-install", help="Fail instead of installing missing runtimes.")
    ] = False,
) -> None:
    """Start infrastructure, databases and services, then wait until they are healthy."""
    ctx = current_context()
    renderer = ctx.renderer
    orchestrator = ctx.orchestrator()
    if no_install:
        orchestrator.registry = None

    renderer.write(
        f"Project: {orchestrator.context.project_name} ({orchestrator.config.environment})"
    )
    report = orchestrator.up(services or None)

    renderer.heading("Services")
    rows = [
        [
            renderer.symbols.ok if view.running else renderer.symbols.fail,
            view.name,
            str(view.state),
            f"PID {view.pid}" if view.pid else "",
            f":{view.port}" if view.port else "",
        ]
        for view in report.services
    ]
    renderer.table(rows)
    renderer.write()
    renderer.write("All services healthy" if report.healthy else "Some services are not healthy")


def down(
    services: Annotated[
        list[str] | None,
        typer.Argument(help="Services to stop (default: everything in this project)."),
    ] = None,
) -> None:
    """Stop this project. Shared infrastructure other projects use stays up."""
    ctx = current_context()
    renderer = ctx.renderer
    orchestrator = ctx.orchestrator()

    renderer.heading("Stopping")
    report = orchestrator.down(services or None)

    kept = [item for item in report.infrastructure if item.refused]
    if kept:
        renderer.write()
        renderer.write("Left running (shared):")
        for item in kept:
            renderer.detail(f"{item.name}: {item.detail}")
    if not report.services and not report.infrastructure:
        renderer.write("Nothing was running.")


def _uptime(seconds: float | None) -> str:
    return format_duration(seconds)
