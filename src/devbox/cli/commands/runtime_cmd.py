"""``devbox runtime list|install|remove``."""

from __future__ import annotations

from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.cli.output import format_size
from devbox.errors import RuntimeInstallError
from devbox.lockfile.models import LockedRuntime

runtime_app = typer.Typer(
    name="runtime",
    help="Inspect and manage the runtimes installed under %LOCALAPPDATA%\\Devbox.",
    no_args_is_help=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(runtime_app)


@runtime_app.command("list")
def runtime_list() -> None:
    """List every runtime installed for this user."""
    ctx = current_context()
    renderer = ctx.renderer
    from devbox.runtime.manager import RuntimeManager

    manager = RuntimeManager(ctx.paths, platform=ctx.platform)
    installed = manager.list_installed()
    if not installed:
        renderer.write("No runtimes are installed.")
        renderer.detail(f"they would live in {ctx.paths.runtimes}", indent=2)
        return

    rows = [["", "NAME", "VERSION", "STATE", "LOCATION"]]
    for runtime in installed:
        ok, reason = manager.verify(runtime.name, runtime.version)
        rows.append(
            [
                renderer.symbols.ok if ok else renderer.symbols.fail,
                runtime.name,
                runtime.version,
                "ready" if ok else reason,
                str(runtime.root),
            ]
        )
    renderer.table(rows)


@runtime_app.command("install")
def runtime_install(
    name: Annotated[str, typer.Argument(help="Runtime name, e.g. node.")],
    version: Annotated[str, typer.Argument(help="Exact version, e.g. 24.6.0.")],
    force: Annotated[bool, typer.Option("--force", help="Reinstall even if present.")] = False,
) -> None:
    """Download, verify and install one runtime version."""
    ctx = current_context()
    renderer = ctx.renderer
    registry = ctx.require_registry()

    from devbox.runtime.manager import RuntimeManager

    manager = RuntimeManager(ctx.paths, platform=ctx.platform)
    manifest = registry.resolve(name, version, ctx.platform)
    outcome = manager.install_locked(
        LockedRuntime(
            name=name,
            version=version,
            platform=ctx.platform.tag,
            sha256=manifest.sha256,
            source=manifest.source or registry.location,
            url=manifest.url,
            size=manifest.size,
            archive=manifest.archive_name,
        ),
        registry,
        force=force,
    )
    where = "from the cache" if outcome.from_cache else f"({format_size(outcome.downloaded_bytes)})"
    renderer.success(f"{name} {version} {outcome.action} {where}")
    renderer.detail(str(outcome.runtime.root))


@runtime_app.command("remove")
def runtime_remove(
    name: Annotated[str, typer.Argument(help="Runtime name.")],
    version: Annotated[str, typer.Argument(help="Exact version.")],
) -> None:
    """Delete one installed runtime version."""
    ctx = current_context()
    from devbox.runtime.manager import RuntimeManager

    manager = RuntimeManager(ctx.paths, platform=ctx.platform)
    if not manager.remove(name, version):
        msg = f"{name} {version} is not installed"
        raise RuntimeInstallError(msg, hint="Run 'devbox runtime list' to see what is.")
    ctx.renderer.success(f"removed {name} {version}")
