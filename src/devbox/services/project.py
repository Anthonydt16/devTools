"""Everything one project needs, assembled once.

``ProjectContext`` is the composition root: it resolves the paths, the project
identity, the supervisor, the state store, the runtime manager, the
infrastructure manager and the database manager, and hands the assembled object
to the orchestrator.  Every collaborator has a working default and every one of
them can be replaced, which is what lets the orchestrator be tested end to end
with fakes and then run unchanged against the real thing.

A plain class rather than a dataclass on purpose: the constructor takes optional
collaborators and stores non-optional ones, and expressing that honestly is worth
more than the generated ``__repr__``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from devbox.config.models import ProjectConfig
from devbox.database.manager import DatabaseManager
from devbox.host.paths import DevboxPaths, ProjectPaths
from devbox.host.platform_info import Platform, current_platform
from devbox.infra.manager import InfraManager
from devbox.process.factory import create_supervisor
from devbox.process.runner import CommandRunner, SubprocessRunner
from devbox.process.supervisor import ProcessSupervisor
from devbox.runtime.manager import RuntimeManager
from devbox.state.models import ProjectState, project_id_for
from devbox.state.store import StateStore

__all__ = ["ProjectContext"]


class ProjectContext:
    """A project plus the machinery that acts on it."""

    __slots__ = (
        "config",
        "databases",
        "environ",
        "infrastructure",
        "paths",
        "platform",
        "project_root",
        "runner",
        "runtimes",
        "state",
        "supervisor",
    )

    def __init__(
        self,
        config: ProjectConfig,
        project_root: Path,
        paths: DevboxPaths,
        *,
        platform: Platform | None = None,
        environ: Mapping[str, str] | None = None,
        supervisor: ProcessSupervisor | None = None,
        state: StateStore | None = None,
        runtimes: RuntimeManager | None = None,
        infrastructure: InfraManager | None = None,
        databases: DatabaseManager | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        # Importing the infra package registers the built-in providers; doing it
        # here rather than at module import keeps the dependency direction clean.
        import devbox.infra  # noqa: F401

        self.config = config
        self.project_root = project_root.resolve()
        self.paths = paths.ensure()
        self.platform = platform or current_platform()
        self.environ: Mapping[str, str] = dict(os.environ if environ is None else environ)

        self.runner: CommandRunner = runner or SubprocessRunner()
        self.supervisor: ProcessSupervisor = supervisor or create_supervisor()
        self.state: StateStore = state or StateStore(paths=self.paths).ensure()
        self.runtimes: RuntimeManager = runtimes or RuntimeManager(
            self.paths, platform=self.platform
        )
        self.infrastructure: InfraManager = infrastructure or InfraManager(
            self.paths,
            supervisor=self.supervisor,
            state=self.state,
            runtimes=self.runtimes,
            runner=self.runner,
        )
        self.databases: DatabaseManager = databases or DatabaseManager(
            runner=self.runner,
            context_factory=self.infrastructure.context_for,
            environ=self.environ,
        )
        self.project_paths.ensure()

    # -- identity ------------------------------------------------------------
    @property
    def project_id(self) -> str:
        return project_id_for(self.project_root)

    @property
    def project_name(self) -> str:
        return self.config.project

    @property
    def project_paths(self) -> ProjectPaths:
        return ProjectPaths(project_root=self.project_root)

    def load_state(self) -> ProjectState:
        state = self.state.load_project(self.project_id)
        return replace(
            state,
            project_name=self.project_name,
            project_root=str(self.project_root),
            environment=self.config.environment,
        )

    def save_state(self, state: ProjectState) -> None:
        self.state.save_project(state)

    def resolve_working_dir(self, relative: str) -> Path:
        """Resolve a ``working_dir`` from ``devbox.yml`` against the project root."""
        return (self.project_root / relative).resolve()
