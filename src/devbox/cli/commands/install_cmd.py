"""``devbox install`` — resolve ``devbox.lock`` and fetch the runtimes it pins."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.lockfile.io import dump_lockfile, read_lockfile, write_lockfile
from devbox.runtime.resolver import resolve_lockfile


def register(app: typer.Typer) -> None:
    app.command("install")(install)


def install(
    force: Annotated[
        bool, typer.Option("--force", help="Re-query the registry for every runtime.")
    ] = False,
    lock_only: Annotated[
        bool, typer.Option("--lock-only", help="Write devbox.lock without installing.")
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Fail if devbox.lock would change (for CI)."),
    ] = False,
) -> None:
    """Resolve devbox.lock and install the runtimes this project needs."""
    ctx = current_context()
    renderer = ctx.renderer
    config = ctx.load_config()
    orchestrator = ctx.orchestrator(config)
    lock_path = orchestrator.context.project_paths.lock_file

    requirements = config.required_runtimes()
    if not requirements:
        renderer.write("No runtimes are declared; nothing to install.")
        return

    registry = ctx.require_registry()
    existing = read_lockfile(lock_path)
    resolution = resolve_lockfile(
        config,
        registry=registry,
        platform=ctx.platform,
        existing=existing,
        force=force,
    )

    if check:
        if dump_lockfile(resolution.lockfile) != dump_lockfile(existing):
            from devbox.errors import LockfileError

            msg = "devbox.lock is out of date"
            raise LockfileError(
                msg,
                hint="Run 'devbox install' and commit the updated devbox.lock.",
            )
        renderer.success("devbox.lock is up to date")
        return

    if resolution.changed:
        write_lockfile(lock_path, resolution.lockfile)
    renderer.heading("Lockfile")
    renderer.success(resolution.summary())
    for entry in resolution.added:
        renderer.detail(f"+ {entry.name} {entry.version}  {entry.sha256[:16]}…")
    for entry in resolution.removed:
        renderer.detail(f"- {entry.name} {entry.version}")

    if lock_only:
        return

    renderer.heading("Runtimes")
    orchestrator.install()
