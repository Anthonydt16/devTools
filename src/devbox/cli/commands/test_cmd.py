"""``devbox test`` — run the whole validation suite."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.cli.output import format_duration
from devbox.errors import ValidationSuiteError
from devbox.validation.checks import CheckOutcome, CheckResult
from devbox.validation.suite import ValidationSuite, default_checks


def register(app: typer.Typer) -> None:
    app.command("test")(test)


def test(
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Run only these checks or groups (repeatable)."),
    ] = None,
    fast: Annotated[bool, typer.Option("--fast", help="Skip tests marked slow.")] = False,
    root: Annotated[
        Path | None, typer.Option("--root", help="Repository root (default: detected).")
    ] = None,
) -> None:
    """Run every check: lint, types, unit, integration, Windows, config, packaging.

    Exits non-zero if any mandatory check fails. Reports skipped tests and why.
    """
    ctx = current_context()
    renderer = ctx.renderer
    repository = (root or _detect_root(ctx.cwd)).resolve()

    suite = ValidationSuite(
        root=repository,
        checks=default_checks(include_slow=not fast),
        environ=ctx.environ,
    )

    renderer.write(f"Devbox validation suite — {repository}")
    renderer.write()

    def show(result: CheckResult) -> None:
        label = f"{result.name:<12}"
        timing = f"({format_duration(result.duration)})" if result.duration >= 1 else ""
        line = f"{label} {result.summary} {timing}".rstrip()
        match result.outcome:
            case CheckOutcome.PASSED:
                renderer.success(line)
            case CheckOutcome.SKIPPED:
                renderer.pending(line)
            case _:
                renderer.failure(line) if result.mandatory else renderer.warning(line)

    report = suite.run(only=only or (), on_result=show)

    renderer.heading("Summary")
    totals = [
        f"{report.total('passed')} passed",
        f"{report.total('failed')} failed",
        f"{report.total('skipped')} skipped",
    ]
    renderer.write(f"  tests:  {', '.join(totals)}")
    renderer.write(f"  checks: {len(report.results)} run, {len(report.failed)} failed")
    renderer.write(f"  time:   {format_duration(report.duration)}")

    skip_lines = report.skip_lines()
    if skip_lines:
        renderer.write()
        renderer.write("  Skipped:")
        for line in skip_lines:
            renderer.detail(line, indent=4)

    if report.ok:
        renderer.write()
        renderer.success("all mandatory checks passed", indent=0)
        return

    renderer.write()
    for result in report.blocking:
        renderer.error_block(
            f"{result.name} failed",
            fields=[("Check", result.summary)],
            body=result.detail,
        )
    names = ", ".join(result.name for result in report.blocking)
    msg = f"validation failed: {names}"
    raise ValidationSuiteError(msg, hint="Fix the checks above and run 'devbox test' again.")


def _detect_root(start: Path) -> Path:
    """Find the repository root by looking for ``pyproject.toml``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return current
