"""The ``devbox`` command-line application.

Typer builds the parser from type hints — the same source of truth the rest of
the codebase uses — and it is a single pure-Python dependency, which matters
because ``devbox.exe`` is produced with PyInstaller.  See docs/architecture.md
for the comparison with Click and argparse.

Two rules hold everywhere in this package:

* **No business logic.**  A command parses arguments, makes one domain call, and
  renders the result.  Anything more belongs in ``services/``.
* **No traceback for the user.**  Every :class:`~devbox.errors.DevboxError` is
  rendered as a message, a set of facts and a next step, and mapped to the exit
  code the error class carries.  ``--debug`` restores the traceback for us.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from devbox import __version__
from devbox.cli.context import CliContext, GlobalOptions
from devbox.cli.main_state import current_context, set_context
from devbox.cli.output import Renderer
from devbox.errors import DevboxError, ExitCode, ServiceStartError

# Typer vendors its own Click; importing the exception from there keeps the
# translation correct whichever build of Typer is installed.
try:  # pragma: no cover - one branch per Typer packaging
    from typer._click.exceptions import ClickException as _ClickException
except ImportError:  # pragma: no cover
    from click.exceptions import (  # type: ignore[import-not-found, no-redef, unused-ignore]
        ClickException as _ClickException,
    )

__all__ = ["app", "main", "run"]

app = typer.Typer(
    name="devbox",
    help=(
        "User-space runtime and process orchestrator for Windows 11 — "
        "docker-compose ergonomics without Docker, WSL, Hyper-V or admin rights."
    ),
    add_completion=False,
    no_args_is_help=True,
)

context = current_context
"""The context built by the root callback; see :mod:`devbox.cli.main_state`."""


@app.callback()
def root(
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="Path to devbox.yml (default: found by walking up)."),
    ] = None,
    environment: Annotated[
        str,
        typer.Option("--environment", "-e", help="Environment overlay to apply."),
    ] = "local",
    registry: Annotated[
        str,
        typer.Option("--registry", help="Runtime registry URL or directory."),
    ] = "",
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress progress output.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colour.")] = False,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show tracebacks and extra detail.")
    ] = False,
) -> None:
    """Global options, applied to every sub-command."""
    set_context(
        CliContext.build(
            GlobalOptions(
                file=file,
                environment=environment,
                registry=registry,
                quiet=quiet,
                no_color=no_color,
                debug=debug,
            )
        )
    )


@app.command()
def version() -> None:
    """Print the Devbox version."""
    context().renderer.write(f"Devbox v{__version__}")


# Imported here rather than at the top so the command modules can import the
# shared CLI state without importing this module back.
from devbox.cli.commands import register_all  # noqa: E402

register_all(app)


def render_error(error: DevboxError, renderer: Renderer, *, debug: bool) -> None:
    """Render a domain error the way the specification asks for."""
    if isinstance(error, ServiceStartError) and error.details is not None:
        details = error.details
        renderer.error_block(
            f"{error.service} failed to start",
            fields=[
                ("Command", details.command),
                ("Exit code", "" if details.exit_code is None else str(details.exit_code)),
                ("Logs", details.stderr_log or ""),
            ],
            body=details.stderr_tail,
            hint=error.hint or "",
        )
        return
    renderer.error_block(str(error), hint=error.hint or "")
    if debug:
        import traceback

        traceback.print_exception(error, file=renderer.error_stream)


def run(argv: list[str] | None = None) -> int:
    """Run the CLI and return an exit code.  Never raises for a domain error.

    ``standalone_mode=False`` hands control of exit codes and error rendering to
    Devbox instead of the parser, which is what makes them reliable in CI and
    assertable in tests.  The cost is that parser errors have to be translated
    here rather than being printed by Click.
    """
    arguments = sys.argv[1:] if argv is None else argv
    try:
        app(args=arguments, standalone_mode=False)
    except _ClickException as parser_error:
        # A usage error (unknown command, missing argument, bad option) — the
        # parser already knows the right exit code; render it and use it.
        renderer = context().renderer
        renderer.error(f"{parser_error.format_message()}")
        renderer.error("")
        renderer.error("Run 'devbox --help' to see the available commands.")
        return int(getattr(parser_error, "exit_code", ExitCode.USAGE))
    except DevboxError as error:
        ctx = context()
        render_error(error, ctx.renderer, debug=ctx.options.debug)
        return error.exit_code
    except typer.Exit as exit_signal:
        return int(exit_signal.exit_code)
    except KeyboardInterrupt:
        context().renderer.error("interrupted")
        return 130
    except Exception as unexpected:
        ctx = context()
        if ctx.options.debug:
            raise
        ctx.renderer.error_block(
            f"internal error: {unexpected}",
            hint="This is a bug in Devbox. Re-run with --debug for the traceback.",
        )
        return ExitCode.FAILURE
    return ExitCode.OK


def main() -> NoReturn:
    """Console-script entry point."""
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m devbox`
    main()
