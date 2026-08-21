"""``devbox run`` — run a command under explicitly chosen runtime versions."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.process.runner import SubprocessRunner


def register(app: typer.Typer) -> None:
    app.command(
        "run",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(run_command)


def run_command(
    command: Annotated[
        list[str] | None, typer.Argument(help="Command and arguments to run.")
    ] = None,
    node: Annotated[str, typer.Option("--node", help="Node version to use.")] = "",
    python: Annotated[str, typer.Option("--python", help="Python version to use.")] = "",
    timeout: Annotated[float, typer.Option("--timeout", help="Give up after N seconds.")] = 3600.0,
) -> None:
    """Run a command under a chosen runtime version, without needing a service.

    `devbox run --node 18.19.0 npm test` and `devbox run --node 24.6.0 npm test`
    test the same code on both versions, one after the other, with nothing
    installed globally and nothing to switch.
    """
    ctx = current_context()
    renderer = ctx.renderer
    if not command:
        from devbox.errors import ConfigError

        msg = "no command given"
        raise ConfigError(msg, hint="Usage: devbox run --node <version> <command> [args...]")

    runtimes = {name: value for name, value in (("node", node), ("python", python)) if value}
    if not runtimes:
        from devbox.errors import ConfigError

        msg = "no runtime version given"
        raise ConfigError(
            msg,
            hint="Pass at least one, e.g. --node 24.6.0.",
        )

    orchestrator = ctx.orchestrator()
    missing = [
        f"{name} {version}"
        for name, version in runtimes.items()
        if not orchestrator.context.runtimes.is_installed(name, version)
    ]
    if missing:
        from devbox.errors import RuntimeInstallError

        msg = f"runtime not installed: {', '.join(missing)}"
        raise RuntimeInstallError(
            msg,
            hint=f"Run 'devbox runtime install {missing[0].replace(' ', ' ')}'.",
        )

    argv, working_dir, environment = orchestrator.run_argv(runtimes, command)
    result = SubprocessRunner().run(argv, cwd=working_dir, env=environment, timeout=timeout)

    if result.stdout:
        renderer.write(result.stdout.rstrip("\n"))
    if result.stderr:
        renderer.error(result.stderr.rstrip("\n"))
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
