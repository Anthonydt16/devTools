"""``devbox infra up|down|status|logs`` — the shared servers on this machine.

Deliberately separate from ``devbox up``/``devbox down``: infrastructure started
here belongs to the *machine*, and no project's ``devbox down`` may stop it.
"""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.config.models import Mode
from devbox.errors import InfrastructureError
from devbox.logs.reader import follow_file

infra_app = typer.Typer(
    name="infra",
    help="Manage the shared database servers used by every project on this machine.",
    no_args_is_help=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(infra_app)


def _specs(names: list[str] | None):  # type: ignore[no-untyped-def]
    ctx = current_context()
    config = ctx.load_config()
    if not names:
        return ctx, config, list(config.infrastructure.values())
    unknown = [name for name in names if name not in config.infrastructure]
    if unknown:
        known = ", ".join(sorted(config.infrastructure)) or "<none>"
        msg = f"unknown infrastructure: {', '.join(unknown)}"
        raise InfrastructureError(msg, hint=f"Declared: {known}")
    return ctx, config, [config.infrastructure[name] for name in names]


@infra_app.command("up")
def infra_up(
    names: Annotated[
        list[str] | None, typer.Argument(help="Instances to start (default: all).")
    ] = None,
) -> None:
    """Start shared infrastructure for the whole machine.

    What is started here survives every project's `devbox down`; only
    `devbox infra down` stops it.
    """
    ctx, _config, specs = _specs(names)
    manager = ctx.orchestrator().context.infrastructure
    renderer = ctx.renderer
    renderer.heading("Infrastructure")
    for spec in specs:
        if spec.mode is Mode.EXTERNAL:
            renderer.pending(f"{spec.name:<24} external, not started by Devbox")
            continue
        result = manager.up(spec, global_ownership=True)
        where = f"{spec.host}:{spec.port}"
        state = "already running" if result.already_running else "running"
        renderer.success(f"{spec.name:<24} {state} on {where}")
        if result.initialised:
            renderer.detail("initialised a new data directory")


@infra_app.command("down")
def infra_down(
    names: Annotated[
        list[str] | None, typer.Argument(help="Instances to stop (default: all).")
    ] = None,
) -> None:
    """Stop shared infrastructure. Data directories are never deleted."""
    ctx, _config, specs = _specs(names)
    manager = ctx.orchestrator().context.infrastructure
    renderer = ctx.renderer
    renderer.heading("Infrastructure")
    for spec in specs:
        result = manager.down(spec, force_global=True)
        if result.was_running and result.stopped:
            renderer.success(f"{spec.name:<24} stopped")
        else:
            renderer.pending(f"{spec.name:<24} {result.detail or 'was not running'}")
    renderer.write()
    renderer.detail("Data directories were left untouched.", indent=2)


@infra_app.command("status")
def infra_status(
    names: Annotated[
        list[str] | None, typer.Argument(help="Instances to inspect (default: all).")
    ] = None,
) -> None:
    """Show which shared servers are running and who is using them."""
    ctx, _config, specs = _specs(names)
    manager = ctx.orchestrator().context.infrastructure
    renderer = ctx.renderer
    rows = [["", "NAME", "PROVIDER", "MODE", "STATE", "ADDRESS", "USED BY"]]
    for spec in specs:
        item = manager.status(spec)
        if item.mode is Mode.EXTERNAL:
            marker, state = renderer.symbols.pending, "external"
        elif item.running:
            marker, state = renderer.symbols.ok, "running"
        else:
            marker, state = renderer.symbols.pending, "stopped"
        rows.append(
            [
                marker,
                item.name,
                item.provider,
                str(item.mode),
                state,
                f"{item.host}:{item.port}" if item.port else item.host,
                str(len(item.claims)) if item.claims else "-",
            ]
        )
    renderer.table(rows)


@infra_app.command("logs")
def infra_logs(
    name: Annotated[str, typer.Argument(help="Instance whose logs to show.")],
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing.")] = False,
    tail: Annotated[int, typer.Option("--tail", "-n", help="Last N lines.")] = 50,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Stop following after N seconds.")
    ] = 0.0,
) -> None:
    """Show a shared server's own log output."""
    ctx, _config, specs = _specs([name])
    spec = specs[0]
    manager = ctx.orchestrator().context.infrastructure
    renderer = ctx.renderer
    stdout_path, stderr_path = manager.log_paths(spec)

    from devbox.logs.reader import LogReader

    reader = LogReader(service=spec.name, stdout_path=stdout_path, stderr_path=stderr_path)
    if reader.available() == (False, False):
        renderer.write(f"No logs yet for {spec.name!r}.")
        renderer.detail(f"expected at {stdout_path}", indent=2)
        return
    for line in reader.read(tail=tail or None):
        renderer.write(line.render())
    if follow:
        for text in follow_file(stdout_path, stop_after=timeout or None):
            renderer.write(text)
