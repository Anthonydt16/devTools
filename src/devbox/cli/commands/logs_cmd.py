"""``devbox logs``."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.logs.reader import LogStream


def register(app: typer.Typer) -> None:
    app.command("logs")(logs)


def logs(
    service: Annotated[str, typer.Argument(help="Service whose logs to show.")],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new lines (Ctrl-C to stop).")
    ] = False,
    tail: Annotated[
        int, typer.Option("--tail", "-n", help="Show only the last N lines per stream.")
    ] = 0,
    stdout_only: Annotated[bool, typer.Option("--stdout", help="Only stdout.")] = False,
    stderr_only: Annotated[bool, typer.Option("--stderr", help="Only stderr.")] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Stop following after N seconds (0 = until Ctrl-C)."),
    ] = 0.0,
) -> None:
    """Show a service's output. stdout and stderr are captured separately."""
    ctx = current_context()
    renderer = ctx.renderer
    orchestrator = ctx.orchestrator()
    reader = orchestrator.log_reader(service)

    streams = [LogStream.STDOUT, LogStream.STDERR]
    if stdout_only and not stderr_only:
        streams = [LogStream.STDOUT]
    elif stderr_only and not stdout_only:
        streams = [LogStream.STDERR]

    has_stdout, has_stderr = reader.available()
    if not has_stdout and not has_stderr:
        renderer.write(f"No logs yet for {service!r}.")
        renderer.detail(f"expected at {reader.stdout_path}", indent=2)
        return

    for line in reader.read(streams=streams, tail=tail or None):
        renderer.write(line.render())

    if not follow:
        return

    try:
        for line in reader.follow(
            streams=streams,
            stop_after=timeout or None,
        ):
            renderer.write(line.render())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        renderer.write()
