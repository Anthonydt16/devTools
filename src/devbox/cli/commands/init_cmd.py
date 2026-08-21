"""``devbox init`` — write a starter ``devbox.yml``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from devbox.cli.main_state import current_context
from devbox.errors import ConfigError

TEMPLATE = """\
version: 1
project: {project}

# Shared infrastructure: one server per machine, used by every project.
# Uncomment what this project needs.
#
# infrastructure:
#   company-postgres:
#     provider: postgres
#     version: "17"
#     mode: managed        # managed | external
#     port: 5432
#
# databases:
#   clients:
#     provider: postgres
#     infrastructure: company-postgres
#     database: clients
#     init:
#       if_missing:
#         command: npm run db:init

services:
  app:
    runtime: {{ node: "{node}" }}
    working_dir: .
    command: npm run dev
    env:
      NODE_ENV: development
    healthcheck:
      http:
        url: "http://127.0.0.1:3000/health"
        timeout: 30
        retries: 10
        start_period: 2

# Per-environment overrides. Non-local environments must use mode: external so
# Devbox never starts a local database server.
#
# environments:
#   production:
#     infrastructure:
#       company-postgres:
#         mode: external
#         url_env: PROD_DATABASE_URL
"""


def register(app: typer.Typer) -> None:
    app.command("init")(init)


def init(
    directory: Annotated[
        Path | None, typer.Argument(help="Where to create devbox.yml (default: here).")
    ] = None,
    node: Annotated[str, typer.Option("--node", help="Node version to pin.")] = "24.6.0",
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing devbox.yml.")
    ] = False,
) -> None:
    """Create a starter devbox.yml in this directory."""
    ctx = current_context()
    root = (directory or ctx.cwd).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "devbox.yml"

    if target.exists() and not force:
        msg = f"{target} already exists"
        raise ConfigError(msg, hint="Pass --force to overwrite it.")

    target.write_text(TEMPLATE.format(project=root.name, node=node), encoding="utf-8", newline="\n")

    renderer = ctx.renderer
    renderer.success(f"created {target}")
    renderer.write()
    renderer.write("Next steps:")
    renderer.detail("1. edit devbox.yml to describe your services", indent=2)
    renderer.detail("2. devbox install    resolve devbox.lock and fetch runtimes", indent=2)
    renderer.detail("3. devbox up         start everything", indent=2)
