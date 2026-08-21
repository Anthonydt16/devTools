"""Wiring the CLI to the domain.

Everything the commands need — locating the project, loading and validating
``devbox.yml``, building the orchestrator, choosing a registry — happens here, so
each command function stays a thin translation of arguments into one domain call
and a report into terminal output.  That separation is what makes the CLI
testable without a terminal, and what keeps business logic out of ``cli/``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from devbox.cli.output import Renderer
from devbox.config.loader import DEFAULT_ENVIRONMENT, load_config
from devbox.config.models import ProjectConfig
from devbox.errors import ConfigError, DevboxError
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import Platform, current_platform
from devbox.runtime.registry import RuntimeRegistry, registry_from_uri
from devbox.services.events import Event, EventKind
from devbox.services.orchestrator import Orchestrator
from devbox.services.project import ProjectContext

__all__ = ["CliContext", "GlobalOptions", "find_project_root"]

CONFIG_FILENAME = "devbox.yml"
REGISTRY_ENV = "DEVBOX_REGISTRY"


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* looking for ``devbox.yml``.

    Same ergonomics as git: run ``devbox ps`` from ``backend/src`` and it finds
    the project.  Returns ``None`` rather than raising, because some commands
    (``devbox runtime list``, ``devbox infra status``) work fine without a
    project.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


@dataclass
class GlobalOptions:
    """Options accepted before the sub-command."""

    file: Path | None = None
    environment: str = DEFAULT_ENVIRONMENT
    registry: str = ""
    quiet: bool = False
    no_color: bool = False
    debug: bool = False


@dataclass
class CliContext:
    """One command invocation."""

    options: GlobalOptions = field(default_factory=GlobalOptions)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    platform: Platform = field(default_factory=current_platform)
    cwd: Path = field(default_factory=Path.cwd)
    renderer: Renderer = field(default_factory=Renderer)

    @classmethod
    def build(
        cls,
        options: GlobalOptions,
        *,
        environ: Mapping[str, str] | None = None,
        renderer: Renderer | None = None,
        cwd: Path | None = None,
    ) -> CliContext:
        """Assemble a context, deriving the renderer from the global options."""
        resolved_environ = dict(os.environ if environ is None else environ)
        return cls(
            options=options,
            environ=resolved_environ,
            cwd=cwd or Path.cwd(),
            renderer=renderer
            or Renderer(
                colour=False if options.no_color else None,
                quiet=options.quiet,
                environ=resolved_environ,
            ),
        )

    # -- locations -----------------------------------------------------------
    @property
    def paths(self) -> DevboxPaths:
        return DevboxPaths.from_environ(self.environ)

    def config_path(self) -> Path:
        if self.options.file is not None:
            return self.options.file
        root = find_project_root(self.cwd)
        if root is None:
            msg = f"no {CONFIG_FILENAME} found in {self.cwd} or any parent directory"
            raise ConfigError(
                msg,
                hint="Run 'devbox init' to create one, or use --file to point at another path.",
            )
        return root / CONFIG_FILENAME

    def project_root(self) -> Path:
        return self.config_path().parent.resolve()

    # -- domain --------------------------------------------------------------
    def load_config(self) -> ProjectConfig:
        return load_config(self.config_path(), environment=self.options.environment)

    def registry(self) -> RuntimeRegistry | None:
        """The registry to install from, or ``None`` when none is configured."""
        uri = self.options.registry or self.environ.get(REGISTRY_ENV, "")
        return registry_from_uri(uri) if uri else None

    def require_registry(self) -> RuntimeRegistry:
        registry = self.registry()
        if registry is None:
            msg = "no runtime registry configured"
            raise DevboxError(
                msg,
                hint=(
                    f"Pass --registry <url-or-path>, or set {REGISTRY_ENV}. "
                    "See docs/registry.md for how to build one."
                ),
            )
        return registry

    def project_context(self, config: ProjectConfig | None = None) -> ProjectContext:
        return ProjectContext(
            config=config or self.load_config(),
            project_root=self.project_root(),
            paths=self.paths,
            platform=self.platform,
            environ=self.environ,
        )

    def orchestrator(self, config: ProjectConfig | None = None) -> Orchestrator:
        return Orchestrator(
            context=self.project_context(config),
            registry=self.registry(),
            on_event=self.render_event,
        )

    # -- progress ------------------------------------------------------------
    def render_event(self, event: Event) -> None:
        """Turn a domain event into a line of output."""
        text = f"{event.name:<20} {event.message}"
        match event.kind:
            case EventKind.SUCCESS:
                self.renderer.success(text)
            case EventKind.FAILURE:
                self.renderer.failure(text)
            case EventKind.WARNING:
                self.renderer.warning(text)
            case _:
                self.renderer.pending(text)
        if event.detail and self.options.debug:
            self.renderer.detail(event.detail)
