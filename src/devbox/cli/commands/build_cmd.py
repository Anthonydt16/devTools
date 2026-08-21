"""``devbox build`` — produce a reproducible artifact."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from devbox.build.artifact import Builder, BuildSpec
from devbox.cli.main_state import current_context
from devbox.cli.output import format_size
from devbox.lockfile.io import read_lockfile


def register(app: typer.Typer) -> None:
    app.command("build")(build)


def build(
    version: Annotated[
        str, typer.Option("--version", "-v", help="Artifact version, e.g. 1.4.2.")
    ] = "0.0.0",
    name: Annotated[
        str, typer.Option("--name", help="Application name (default: the project name).")
    ] = "",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output directory (default: ./dist).")
    ] = None,
    include: Annotated[
        list[str] | None, typer.Option("--include", help="Glob to include (repeatable).")
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", help="Extra glob to exclude (repeatable).")
    ] = None,
) -> None:
    """Package this project into dist/<name>-<version>.zip with a manifest.

    Set SOURCE_DATE_EPOCH to make two builds of the same tree byte-identical.
    """
    ctx = current_context()
    renderer = ctx.renderer
    config = ctx.load_config()
    project_root = ctx.project_root()
    from devbox.host.paths import ProjectPaths

    project_paths = ProjectPaths(project_root=project_root)
    lockfile = read_lockfile(project_paths.lock_file)

    from devbox.build.artifact import DEFAULT_EXCLUDES

    spec = BuildSpec(
        application=name or config.project,
        version=version,
        project_root=project_root,
        output_dir=output or project_paths.dist,
        include=tuple(include or ()),
        exclude=(*DEFAULT_EXCLUDES, *(exclude or ())),
        runtimes={r.name: r.version for r in config.required_runtimes()},
    )
    result = Builder(environ=ctx.environ).build(spec, lockfile=lockfile)

    renderer.heading("Build")
    renderer.success(f"{result.artifact_path.name}  {format_size(result.size)}")
    renderer.detail(f"path:      {result.artifact_path}")
    renderer.detail(f"sha256:    {result.sha256}")
    renderer.detail(f"files:     {result.file_count}")
    renderer.detail(f"lock hash: {result.manifest['devbox_lock_hash']}")
    renderer.detail(f"manifest:  {result.manifest_path}")
