"""``devbox ps`` and ``devbox status``."""

from __future__ import annotations

import typer

from devbox.cli.main_state import current_context
from devbox.cli.output import format_duration
from devbox.config.models import Mode


def register(app: typer.Typer) -> None:
    app.command("ps")(ps)
    app.command("status")(status)


def ps() -> None:
    """Show this project's services."""
    ctx = current_context()
    renderer = ctx.renderer
    views = ctx.orchestrator().ps()
    if not views:
        renderer.write("No services are declared.")
        return
    rows = [["", "SERVICE", "STATE", "PID", "PORT", "RUNTIME", "UPTIME"]]
    for view in views:
        rows.append(
            [
                renderer.symbols.ok if view.running else renderer.symbols.pending,
                view.name,
                str(view.state),
                str(view.pid) if view.pid else "-",
                str(view.port) if view.port else "-",
                view.runtime or "-",
                format_duration(view.uptime_seconds) or "-",
            ]
        )
    renderer.table(rows)


def status() -> None:
    """Show runtimes, infrastructure and services in one view."""
    ctx = current_context()
    renderer = ctx.renderer
    from devbox import __version__

    report = ctx.orchestrator().status()

    renderer.write(f"Devbox v{__version__}")
    renderer.write(f"Project: {report.project} ({report.environment})")
    renderer.detail(f"root:   {report.project_root}", indent=0)
    renderer.detail(f"home:   {report.devbox_home}", indent=0)

    if report.runtimes:
        renderer.heading("Runtimes")
        for name, version, installed in report.runtimes:
            label = f"{name} {version}"
            if installed:
                renderer.success(f"{label:<24} installed")
            else:
                renderer.failure(f"{label:<24} missing (run 'devbox install')")

    if report.infrastructure:
        renderer.heading("Infrastructure")
        for item in report.infrastructure:
            where = f"{item.host}:{item.port}" if item.port else item.host
            label = f"{item.name} {item.provider} {item.version}".strip()
            if item.mode is Mode.EXTERNAL:
                renderer.pending(f"{label:<34} external {where}")
            elif item.running:
                shared = f" (used by {len(item.claims)} project(s))" if item.claims else ""
                renderer.success(f"{label:<34} running on {where}{shared}")
            else:
                renderer.pending(f"{label:<34} stopped")

    renderer.heading("Services")
    if report.services:
        for view in report.services:
            label = f"{view.name:<20}"
            if view.running:
                port = f" :{view.port}" if view.port else ""
                renderer.success(f"{label} PID {view.pid}{port}")
            else:
                renderer.pending(
                    f"{label} {view.state}{f' — {view.detail}' if view.detail else ''}"
                )
    else:
        renderer.pending("none declared")

    for warning in report.warnings:
        renderer.warning(warning)
