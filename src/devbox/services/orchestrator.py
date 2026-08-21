"""``devbox up`` / ``down`` / ``ps`` / ``logs`` / ``exec`` / ``run``.

The orchestrator is the only place that knows the *order* of things:

``up``
    verify the lockfile covers the project -> install missing runtimes ->
    topological order over infrastructure, databases and services ->
    for each node, start it and wait for it to be *ready*, not merely started ->
    persist ownership -> report.

``down``
    reverse of the order things were actually started in -> stop services ->
    release this project's claims on shared infrastructure, stopping only what
    nothing else is using and what ``devbox infra up`` did not start.

Everything it needs is injected through :class:`~devbox.services.project.ProjectContext`,
so the whole flow runs against fakes in unit tests and against real processes and
real archives in the integration suite.

Two invariants are enforced here and not delegated:

* nothing starts until the configuration, the lockfile and the dependency graph
  have all been validated — a broken project fails before it has half started;
* a failure during ``up`` stops what *this invocation* started, and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devbox.config.interpolation import (
    InterpolationContext,
    interpolate,
    interpolate_mapping,
)
from devbox.config.models import (
    DatabaseSpec,
    InfrastructureSpec,
    Mode,
    ProjectConfig,
    ServiceSpec,
)
from devbox.database.manager import DatabaseOutcome
from devbox.errors import (
    DevboxError,
    ProcessFailureDetails,
    ServiceStartError,
)
from devbox.graph.dependency import DependencyGraph
from devbox.health.checks import probe_for, wait_for_health
from devbox.infra.manager import InfraStatus, StartResult, StopResult
from devbox.lockfile.io import read_lockfile
from devbox.lockfile.models import Lockfile
from devbox.logs.reader import LogReader, LogStream
from devbox.process.supervisor import LaunchSpec, ProcessRecord, ProcessState, StopOutcome
from devbox.runtime.environment import RuntimeEnvironment, build_environment
from devbox.runtime.manager import InstallOutcome
from devbox.runtime.registry import RuntimeRegistry
from devbox.runtime.resolver import verify_lockfile_covers
from devbox.security.command import quote_for_display, split_command
from devbox.services.events import Event, EventKind, EventScope, EventSink
from devbox.services.project import ProjectContext
from devbox.state.models import ProjectState

__all__ = ["DownReport", "Orchestrator", "ServiceView", "StatusReport", "UpReport"]


def _no_events(event: Event) -> None:
    del event


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ServiceView:
    """One row of ``devbox ps``."""

    name: str
    state: ProcessState
    pid: int | None = None
    port: int | None = None
    runtime: str = ""
    exit_code: int | None = None
    uptime_seconds: float | None = None
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.state is ProcessState.RUNNING


@dataclass(frozen=True, slots=True)
class UpReport:
    """Everything ``devbox up`` did."""

    project: str
    environment: str
    runtimes: tuple[InstallOutcome, ...] = ()
    infrastructure: tuple[StartResult, ...] = ()
    databases: tuple[DatabaseOutcome, ...] = ()
    services: tuple[ServiceView, ...] = ()
    healthy: bool = True

    @property
    def started_services(self) -> tuple[str, ...]:
        return tuple(service.name for service in self.services if service.running)


@dataclass(frozen=True, slots=True)
class DownReport:
    services: tuple[StopOutcome, ...] = ()
    infrastructure: tuple[StopResult, ...] = ()
    released: tuple[str, ...] = ()

    @property
    def refused(self) -> tuple[str, ...]:
        return tuple(item.service_id for item in self.services if item.refused) + tuple(
            item.name for item in self.infrastructure if item.refused
        )


@dataclass(frozen=True, slots=True)
class StatusReport:
    """``devbox status``: the whole picture in one object."""

    project: str
    environment: str
    project_root: str
    devbox_home: str
    runtimes: tuple[tuple[str, str, bool], ...] = ()
    """``(name, version, installed)``."""

    infrastructure: tuple[InfraStatus, ...] = ()
    services: tuple[ServiceView, ...] = ()
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
@dataclass
class Orchestrator:
    """Drives a project through its lifecycle."""

    context: ProjectContext
    registry: RuntimeRegistry | None = None
    on_event: EventSink = _no_events
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    _started_this_run: list[str] = field(default_factory=list, init=False, repr=False)

    # -- shortcuts -----------------------------------------------------------
    @property
    def config(self) -> ProjectConfig:
        return self.context.config

    def _emit(
        self,
        kind: EventKind,
        scope: EventScope,
        name: str,
        message: str,
        detail: str = "",
    ) -> None:
        self.on_event(Event(kind=kind, scope=scope, name=name, message=message, detail=detail))

    # -- validation ----------------------------------------------------------
    def graph(self) -> DependencyGraph:
        graph = DependencyGraph(self.config.all_dependencies())
        graph.validate()
        return graph

    def preflight(self) -> None:
        """Reject a project that could not possibly start, before touching anything."""
        self.graph()
        lockfile = self._lockfile()
        verify_lockfile_covers(
            lockfile, self.config.required_runtimes(), platform=self.context.platform
        )

    def _lockfile(self) -> Lockfile:
        return read_lockfile(self.context.project_paths.lock_file)

    # -- install -------------------------------------------------------------
    def install(self) -> tuple[InstallOutcome, ...]:
        """Install every runtime the lockfile pins for this project."""
        requirements = self.config.required_runtimes()
        if not requirements:
            return ()
        if self.registry is None:
            msg = "no runtime registry configured"
            raise DevboxError(
                msg,
                hint="Pass --registry, or set 'registry' in devbox.yml; see docs/registry.md.",
            )
        lockfile = self._lockfile()
        verify_lockfile_covers(lockfile, requirements, platform=self.context.platform)

        def report(outcome: InstallOutcome) -> None:
            verb = {
                "installed": "installed",
                "repaired": "reinstalled",
                "already-present": "cached",
            }[outcome.action]
            self._emit(
                EventKind.SUCCESS,
                EventScope.RUNTIME,
                str(outcome.runtime),
                verb,
            )

        return self.context.runtimes.ensure_installed(
            requirements, lockfile, self.registry, on_event=report
        )

    # -- up ------------------------------------------------------------------
    def up(self, targets: Sequence[str] | None = None) -> UpReport:
        """Bring the project up, in dependency order, waiting for readiness."""
        self.preflight()
        plan = self.graph().startup_plan(targets)

        runtimes = self.install() if self.registry is not None else ()
        self._verify_runtimes_present()

        state = self.context.load_state()
        infrastructure: list[StartResult] = []
        databases: list[DatabaseOutcome] = []
        services: list[ServiceView] = []
        self._started_this_run = []

        try:
            for name in plan.order:
                if name in self.config.infrastructure:
                    infrastructure.append(self._start_infrastructure(name))
                elif name in self.config.databases:
                    databases.append(self._ensure_database(name))
                else:
                    state, view = self._start_service(name, state, databases)
                    services.append(view)
        except DevboxError:
            self.context.save_state(state)
            self._rollback()
            raise

        self.context.save_state(state)
        return UpReport(
            project=self.context.project_name,
            environment=self.config.environment,
            runtimes=runtimes,
            infrastructure=tuple(infrastructure),
            databases=tuple(databases),
            services=tuple(services),
            healthy=all(service.running for service in services),
        )

    def _verify_runtimes_present(self) -> None:
        """Fail before starting anything if a runtime is missing or damaged."""
        missing: list[str] = []
        for requirement in self.config.required_runtimes():
            ok, reason = self.context.runtimes.verify(requirement.name, requirement.version)
            if not ok:
                missing.append(f"{requirement}: {reason}")
        if not missing:
            return
        msg = "required runtimes are not usable:\n  - " + "\n  - ".join(missing)
        raise DevboxError(msg, hint="Run 'devbox install' to download and install them.")

    def _rollback(self) -> None:
        """Stop only what this invocation started.

        A service that was already running before ``devbox up`` — because another
        target was started earlier — is not this failure's to stop.
        """
        if not self._started_this_run:
            return
        state = self.context.load_state()
        for name in reversed(self._started_this_run):
            record = state.record(name)
            if record is None:
                continue
            self.context.supervisor.stop(record, timeout=5.0)
            state = state.without_process(name)
            self._emit(EventKind.WARNING, EventScope.SERVICE, name, "stopped after the failure")
        self.context.save_state(state)

    # -- infrastructure ------------------------------------------------------
    def _start_infrastructure(self, name: str) -> StartResult:
        spec = self.config.infrastructure_spec(name)
        if spec.mode is Mode.EXTERNAL:
            self._emit(
                EventKind.INFO,
                EventScope.INFRASTRUCTURE,
                name,
                "external, not started by Devbox",
            )
            return self.context.infrastructure.up(spec)

        self._emit(EventKind.PROGRESS, EventScope.INFRASTRUCTURE, name, "starting")
        result = self.context.infrastructure.up(spec, project_id=self.context.project_id)
        where = f"{spec.host}:{spec.port}"
        message = "already running" if result.already_running else "running"
        self._emit(
            EventKind.SUCCESS,
            EventScope.INFRASTRUCTURE,
            name,
            f"{message} on {where}",
            detail="initialised a new data directory" if result.initialised else "",
        )
        return result

    # -- databases -----------------------------------------------------------
    def _ensure_database(self, name: str) -> DatabaseOutcome:
        spec = self.config.databases[name]
        host = self.config.infrastructure_spec(spec.infrastructure)
        outcome = self.context.databases.ensure(
            spec,
            host,
            project_root=self.context.project_root,
            service_environment=dict(self.context.environ),
        )
        self._emit(
            EventKind.SUCCESS,
            EventScope.DATABASE,
            name,
            outcome.render(),
            detail=outcome.detail,
        )
        return outcome

    # -- services ------------------------------------------------------------
    def _start_service(
        self,
        name: str,
        state: ProjectState,
        databases: Sequence[DatabaseOutcome],
    ) -> tuple[ProjectState, ServiceView]:
        service = self.config.service(name)

        existing = state.record(name)
        if existing is not None:
            status = self.context.supervisor.status(existing)
            if status.state is ProcessState.RUNNING:
                self._emit(EventKind.SUCCESS, EventScope.SERVICE, name, "already running")
                return state, self._view(service, existing, status.state)

        environment = self.build_service_environment(service, databases)
        argv, working_dir = self._resolve_invocation(service, databases)

        self._emit(
            EventKind.PROGRESS,
            EventScope.SERVICE,
            name,
            "starting",
            detail=quote_for_display(argv),
        )

        record = self.context.supervisor.start(
            LaunchSpec(
                name=name,
                argv=argv,
                working_dir=working_dir,
                env=environment.as_dict(),
                stdout_path=self.context.project_paths.service_stdout_log(name),
                stderr_path=self.context.project_paths.service_stderr_log(name),
                stop_timeout=service.stop_timeout,
            ),
            project_id=self.context.project_id,
        )
        self._started_this_run.append(name)
        state = state.with_process(record)
        self.context.save_state(state)

        self._await_service(service, record)
        self._emit(
            EventKind.SUCCESS,
            EventScope.SERVICE,
            name,
            f"running (pid {record.pid})" + (f" on port {service.port}" if service.port else ""),
        )
        return state, self._view(service, record, ProcessState.RUNNING)

    def _resolve_invocation(
        self, service: ServiceSpec, databases: Sequence[DatabaseOutcome] = ()
    ) -> tuple[tuple[str, ...], Path]:
        """Interpolate and split a service's command, and resolve its directory.

        ``${...}`` is resolved *before* the command is split, so a substituted
        value containing a space becomes one argument only if it is quoted in
        ``devbox.yml`` — the same rule as any other argument, with no special
        case that could turn a database password into two arguments.
        """
        context = self._interpolation_context(databases)
        command = interpolate(service.command, context, path=f"services.{service.name}.command")
        working_dir = interpolate(
            service.working_dir, context, path=f"services.{service.name}.working_dir"
        )
        return (
            split_command(command, where=f"services.{service.name}.command"),
            self.context.resolve_working_dir(working_dir),
        )

    def _await_service(self, service: ServiceSpec, record: ProcessRecord) -> None:
        """Wait for readiness, and fail with the service's own stderr if it dies."""
        if service.healthcheck is None:
            # No healthcheck: the only signal available is "did it survive the
            # first moment", so check that rather than claiming health we cannot
            # observe.
            status = self.context.supervisor.status(record)
            if status.state is ProcessState.EXITED:
                raise self._start_failure(service, record, status.exit_code)
            return

        def died() -> str | None:
            status = self.context.supervisor.status(record)
            if status.state is ProcessState.RUNNING:
                return None
            code = "" if status.exit_code is None else f" with exit code {status.exit_code}"
            return f"the process exited{code}"

        result = wait_for_health(
            probe_for(service.healthcheck),
            service.healthcheck,
            sleep=self.sleep,
            should_abort=died,
        )
        if result.healthy:
            return

        status = self.context.supervisor.status(record)
        if status.state is not ProcessState.RUNNING:
            raise self._start_failure(service, record, status.exit_code)

        self.context.supervisor.stop(record, timeout=service.stop_timeout)
        raise ServiceStartError(
            service.name,
            f"{service.name} started but never became healthy",
            details=self._failure_details(service, record, None),
            hint=result.render_failure(),
        )

    def _start_failure(
        self, service: ServiceSpec, record: ProcessRecord, exit_code: int | None
    ) -> ServiceStartError:
        details = self._failure_details(service, record, exit_code)
        return ServiceStartError(
            service.name,
            f"{service.name} failed to start",
            details=details,
            hint=f"Full output: {record.stderr_log}",
        )

    def _failure_details(
        self, service: ServiceSpec, record: ProcessRecord, exit_code: int | None
    ) -> ProcessFailureDetails:
        reader = LogReader(
            service=service.name,
            stdout_path=Path(record.stdout_log),
            stderr_path=Path(record.stderr_log),
        )
        return ProcessFailureDetails(
            command=service.command,
            exit_code=exit_code,
            stderr_tail=reader.tail_text(LogStream.STDERR, lines=10),
            stdout_log=record.stdout_log,
            stderr_log=record.stderr_log,
        )

    def build_service_environment(
        self,
        service: ServiceSpec,
        databases: Sequence[DatabaseOutcome] = (),
    ) -> RuntimeEnvironment:
        """Compose one service's environment: runtime PATH plus resolved ``env:``.

        This is where ``${database.clients.url}`` becomes a real connection
        string, and where the per-service ``PATH`` that makes two Node versions
        coexist is built.
        """
        interpolation = self._interpolation_context(databases)
        resolved = interpolate_mapping(
            dict(service.env), interpolation, path=f"services.{service.name}.env"
        )
        return build_environment(
            service.runtime,
            runtime_root=self.context.paths.runtimes,
            base_environment=self.context.environ,
            extra_variables={
                **resolved,
                "DEVBOX_PROJECT": self.context.project_name,
                "DEVBOX_SERVICE": service.name,
                "DEVBOX_ENVIRONMENT": self.config.environment,
            },
            platform=self.context.platform,
        )

    def _interpolation_context(
        self, databases: Sequence[DatabaseOutcome] = ()
    ) -> InterpolationContext:
        by_name = {outcome.name: outcome for outcome in databases}
        database_values: dict[str, Mapping[str, str]] = {}
        for name, spec in self.config.databases.items():
            host = self.config.infrastructure.get(spec.infrastructure)
            if host is None:  # pragma: no cover - config validation rejects this
                continue
            outcome = by_name.get(name)
            url = outcome.url if outcome is not None else self._database_url(spec, host)
            database_values[name] = {
                "url": url,
                "name": spec.database,
                "host": host.host,
                "port": str(host.port or ""),
                "user": spec.username or host.username or "",
                "password": self._password(spec, host),
            }

        infrastructure_values = {
            name: {
                "host": spec.host,
                "port": str(spec.port or ""),
                "url": self._infrastructure_url(spec),
            }
            for name, spec in self.config.infrastructure.items()
        }

        return InterpolationContext(
            databases=database_values,
            infrastructure=infrastructure_values,
            environ=self.context.environ,
            project={
                "name": self.context.project_name,
                "root": str(self.context.project_root),
            },
        )

    def _database_url(self, spec: DatabaseSpec, host: InfrastructureSpec) -> str:
        return self.context.databases.connection_url(spec, host)

    def _password(self, spec: DatabaseSpec, host: InfrastructureSpec) -> str:
        variable = spec.password_env or host.password_env
        return self.context.environ.get(variable, "") if variable else ""

    def _infrastructure_url(self, spec: InfrastructureSpec) -> str:
        if spec.mode is Mode.EXTERNAL:
            return self.context.environ.get(spec.url_env or "", "")
        from devbox.catalogs.providers import provider_descriptor

        descriptor = provider_descriptor(spec.provider)
        if descriptor is None:  # pragma: no cover - config validation rejects this
            return ""
        return descriptor.connection_url(
            host=spec.host,
            port=spec.port or descriptor.default_port,
            database=descriptor.default_database,
            username=spec.username or descriptor.default_username,
        )

    # -- down ----------------------------------------------------------------
    def down(self, targets: Sequence[str] | None = None) -> DownReport:
        """Stop the project, in reverse start order, without touching what is shared."""
        state = self.context.load_state()
        plan = self.graph().startup_plan(targets)

        stopped: list[StopOutcome] = []
        for name in plan.shutdown_order:
            if name not in self.config.services:
                continue
            record = state.record(name)
            if record is None:
                continue
            outcome = self.context.supervisor.stop(
                record, timeout=self.config.service(name).stop_timeout
            )
            stopped.append(outcome)
            kind = EventKind.WARNING if outcome.refused else EventKind.SUCCESS
            message = outcome.detail or ("stopped" if outcome.was_running else "was not running")
            self._emit(kind, EventScope.SERVICE, name, message)
            if not outcome.refused:
                state = state.without_process(name)
        self.context.save_state(state)

        infrastructure: list[StopResult] = []
        for name in plan.shutdown_order:
            if name not in self.config.infrastructure:
                continue
            spec = self.config.infrastructure_spec(name)
            result = self.context.infrastructure.down(spec, project_id=self.context.project_id)
            infrastructure.append(result)
            if result.refused:
                self._emit(EventKind.INFO, EventScope.INFRASTRUCTURE, name, result.detail)
            elif result.was_running:
                self._emit(EventKind.SUCCESS, EventScope.INFRASTRUCTURE, name, "stopped")

        released = self.context.infrastructure.release_project(self.context.project_id)
        return DownReport(
            services=tuple(stopped),
            infrastructure=tuple(infrastructure),
            released=released,
        )

    # -- inspection ----------------------------------------------------------
    def ps(self) -> tuple[ServiceView, ...]:
        state = self.context.load_state()
        views: list[ServiceView] = []
        for name in sorted(self.config.services):
            service = self.config.service(name)
            record = state.record(name)
            if record is None:
                views.append(
                    ServiceView(
                        name=name,
                        state=ProcessState.EXITED,
                        port=service.port,
                        runtime=_runtime_label(service),
                        detail="not started",
                    )
                )
                continue
            status = self.context.supervisor.status(record)
            views.append(self._view(service, record, status.state, status.exit_code, status.detail))
        return tuple(views)

    def _view(
        self,
        service: ServiceSpec,
        record: ProcessRecord,
        state: ProcessState,
        exit_code: int | None = None,
        detail: str = "",
    ) -> ServiceView:
        uptime = None
        if state is ProcessState.RUNNING and record.started_at:
            uptime = max(0.0, self.clock() - record.started_at)
        return ServiceView(
            name=service.name,
            state=state,
            pid=record.pid if state is ProcessState.RUNNING else None,
            port=service.port,
            runtime=_runtime_label(service),
            exit_code=exit_code,
            uptime_seconds=uptime,
            detail=detail,
        )

    def status(self) -> StatusReport:
        runtimes = tuple(
            (r.name, r.version, self.context.runtimes.is_installed(r.name, r.version))
            for r in self.config.required_runtimes()
        )
        infrastructure = self.context.infrastructure.statuses(
            [self.config.infrastructure[name] for name in sorted(self.config.infrastructure)]
        )
        warnings = tuple(w.render() for w in self.context.state.take_warnings())
        return StatusReport(
            project=self.context.project_name,
            environment=self.config.environment,
            project_root=str(self.context.project_root),
            devbox_home=str(self.context.paths.root),
            runtimes=runtimes,
            infrastructure=infrastructure,
            services=self.ps(),
            warnings=warnings,
        )

    def log_reader(self, service_name: str) -> LogReader:
        service = self.config.service(service_name)
        return LogReader(
            service=service.name,
            stdout_path=self.context.project_paths.service_stdout_log(service.name),
            stderr_path=self.context.project_paths.service_stderr_log(service.name),
        )

    # -- exec / run ----------------------------------------------------------
    def exec_argv(
        self, service_name: str, command: Sequence[str]
    ) -> tuple[list[str], Path, dict[str, str]]:
        """Build the argv, working directory and environment for ``devbox exec``.

        Returned rather than executed so the CLI owns process handling and so the
        composition — which is the interesting part — is unit-testable.
        """
        service = self.config.service(service_name)
        argv = split_command(command, where=f"exec {service_name}")
        environment = self.build_service_environment(service)
        _, working_dir = self._resolve_invocation(service)
        resolved = environment.resolve_executable(argv[0])
        return ([resolved or argv[0], *argv[1:]], working_dir, environment.as_dict())

    def run_argv(
        self,
        runtimes: Mapping[str, str],
        command: Sequence[str],
        *,
        working_dir: Path | None = None,
    ) -> tuple[list[str], Path, dict[str, str]]:
        """Same, for ``devbox run --node 18.19.0 npm test`` — no service needed."""
        from devbox.config.models import RuntimeRequirement

        requirements = tuple(
            RuntimeRequirement(name, version) for name, version in sorted(runtimes.items())
        )
        argv = split_command(command, where="run")
        environment = build_environment(
            requirements,
            runtime_root=self.context.paths.runtimes,
            base_environment=self.context.environ,
            platform=self.context.platform,
        )
        resolved = environment.resolve_executable(argv[0])
        return (
            [resolved or argv[0], *argv[1:]],
            working_dir or self.context.project_root,
            environment.as_dict(),
        )


def _runtime_label(service: ServiceSpec) -> str:
    return ", ".join(str(requirement) for requirement in service.runtime)
