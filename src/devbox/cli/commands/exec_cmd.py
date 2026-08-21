"""``devbox exec`` — run a command inside a service's environment."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.process.runner import SubprocessRunner


def register(app: typer.Typer) -> None:
    app.command(
        "exec",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(exec_command)


def exec_command(
    service: Annotated[str, typer.Argument(help="Service whose environment to use.")],
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Command and arguments to run."),
    ] = None,
    timeout: Annotated[float, typer.Option("--timeout", help="Give up after N seconds.")] = 600.0,
) -> None:
    """Run a command with this service's runtimes, PATH and environment.

    `devbox exec legacy-api node --version` prints the version pinned for that
    service, whatever any other service — or the machine — has installed.
    """
    ctx = current_context()
    renderer = ctx.renderer
    if not command:
        from devbox.errors import ConfigError

        msg = "no command given"
        raise ConfigError(msg, hint="Usage: devbox exec <service> <command> [args...]")

    argv, working_dir, environment = ctx.orchestrator().exec_argv(service, command)
    result = SubprocessRunner().run(argv, cwd=working_dir, env=environment, timeout=timeout)

    if result.stdout:
        renderer.write(result.stdout.rstrip("\n"))
    if result.stderr:
        renderer.error(result.stderr.rstrip("\n"))
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
