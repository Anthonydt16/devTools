"""Managing shared infrastructure instances.

The rules this class exists to enforce, in order of importance:

1. **An ``external`` instance is never started.**  A production configuration
   points at a real server; Devbox may health-check it and inject its URL, and
   that is all.  Non-regression test #9.
2. **``devbox down`` on a project never stops shared infrastructure it does not
   exclusively own.**  An instance started by ``devbox infra up`` belongs to the
   machine; an instance another project still claims stays up.
   Non-regression test #2.
3. **Data directories are never deleted.**  Stopping an instance stops a
   process; the databases in it belong to every project on the machine.
4. **Already up is success, already down is success.**  ``devbox infra up`` twice
   in a row must be a no-op, not an error, because CI scripts run it
   unconditionally.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from devbox.config.models import InfrastructureSpec, Mode
from devbox.errors import InfrastructureError
from devbox.health.checks import probe_for, wait_for_health
from devbox.host.paths import DevboxPaths
from devbox.host.procinfo import process_creation_time
from devbox.infra.provider import InfraContext, InfraProvider, get_provider
from devbox.process.runner import CommandRunner, SubprocessRunner
from devbox.process.supervisor import LaunchSpec, ProcessRecord, ProcessState, ProcessSupervisor
from devbox.runtime.manager import RuntimeManager
from devbox.state.models import InfraOwnership, InfraRecord, InfraState
from devbox.state.store import StateStore

__all__ = ["InfraManager", "InfraStatus", "StartResult", "StopResult"]


@dataclass(frozen=True, slots=True)
class InfraStatus:
    """What one instance is doing right now."""

    name: str
    provider: str
    mode: Mode
    running: bool
    host: str
    port: int | None
    pid: int | None = None
    version: str = ""
    ownership: str = ""
    claims: tuple[str, ...] = ()
    data_dir: str = ""
    detail: str = ""

    @property
    def is_external(self) -> bool:
        return self.mode is Mode.EXTERNAL

    def render(self) -> str:
        where = f"{self.host}:{self.port}" if self.port else self.host
        if self.is_external:
            return f"{self.name} external {where}"
        state = "running" if self.running else "stopped"
        return f"{self.name} {state} {where}"


@dataclass(frozen=True, slots=True)
class StartResult:
    """What ``up`` actually did."""

    name: str
    started: bool
    already_running: bool
    skipped_external: bool = False
    initialised: bool = False
    record: InfraRecord | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StopResult:
    """What ``down`` actually did — including a refusal and its reason."""

    name: str
    stopped: bool
    was_running: bool
    refused: bool = False
    detail: str = ""


class InfraManager:
    """Owns the lifecycle of shared infrastructure instances."""

    def __init__(
        self,
        paths: DevboxPaths,
        *,
        supervisor: ProcessSupervisor,
        state: StateStore,
        runtimes: RuntimeManager,
        runner: CommandRunner | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.paths = paths
        self._supervisor = supervisor
        self._state = state
        self._runtimes = runtimes
        self._runner = runner or SubprocessRunner()
        self._clock = clock
        self._sleep = sleep

    # -- context -------------------------------------------------------------
    def context_for(self, spec: InfrastructureSpec) -> InfraContext:
        """Build everything the provider needs, including its runtime PATH."""
        from devbox.config.models import RuntimeRequirement
        from devbox.runtime.environment import build_environment

        data_dir = self.paths.infra_data_dir(spec.provider, spec.name)
        log_dir = self.paths.infra_logs_dir(spec.provider, spec.name)
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        descriptor = get_provider(spec.provider).descriptor
        requirements: list[RuntimeRequirement] = []
        runtime_root: Path | None = None
        if descriptor.runtime_kind and spec.version:
            requirements.append(RuntimeRequirement(descriptor.runtime_kind, spec.version))
            runtime_root = self._runtimes.install_root(descriptor.runtime_kind, spec.version)

        environment = build_environment(
            requirements,
            runtime_root=self.paths.runtimes,
            extra_variables={"PGDATA": str(data_dir)} if spec.provider == "postgres" else None,
        )
        return InfraContext(
            spec=spec,
            data_dir=data_dir,
            log_dir=log_dir,
            runtime_root=runtime_root,
            environment=environment.as_dict(),
            runner=self._runner,
        )

    # -- status --------------------------------------------------------------
    def status(self, spec: InfrastructureSpec) -> InfraStatus:
        if spec.mode is Mode.EXTERNAL:
            return InfraStatus(
                name=spec.name,
                provider=spec.provider,
                mode=spec.mode,
                running=False,
                host=spec.host,
                port=spec.port,
                version=spec.version,
                detail="external: Devbox never starts or stops this",
            )
        record = self._state.load_infra().get(spec.name)
        running = record is not None and self._is_alive(record)
        return InfraStatus(
            name=spec.name,
            provider=spec.provider,
            mode=spec.mode,
            running=running,
            host=spec.host,
            port=spec.port,
            pid=record.pid if record is not None and running else None,
            version=spec.version,
            ownership=record.ownership if record is not None else "",
            claims=record.claims if record is not None else (),
            data_dir=str(self.paths.infra_data_dir(spec.provider, spec.name)),
        )

    def statuses(self, specs: Sequence[InfrastructureSpec]) -> tuple[InfraStatus, ...]:
        return tuple(self.status(spec) for spec in specs)

    def _is_alive(self, record: InfraRecord) -> bool:
        """Verify the recorded PID *and* its creation time, like every other check."""
        process_record = _as_process_record(record)
        return self._supervisor.status(process_record).state is ProcessState.RUNNING

    # -- start ---------------------------------------------------------------
    def up(
        self,
        spec: InfrastructureSpec,
        *,
        project_id: str = "",
        global_ownership: bool = False,
        wait: bool = True,
    ) -> StartResult:
        """Start an instance if it is not already running, and claim it.

        *global_ownership* marks the instance as started by ``devbox infra up``,
        which no project's ``devbox down`` may then stop.
        """
        if spec.mode is Mode.EXTERNAL:
            # The single most important line in this file.
            return StartResult(
                name=spec.name,
                started=False,
                already_running=False,
                skipped_external=True,
                detail="external: Devbox does not start this",
            )

        state = self._state.load_infra()
        existing = state.get(spec.name)
        if existing is not None and self._is_alive(existing):
            claimed = self._claim(state, existing, project_id, global_ownership)
            return StartResult(
                name=spec.name,
                started=False,
                already_running=True,
                record=claimed,
                detail="already running",
            )

        provider = get_provider(spec.provider)
        context = self.context_for(spec)
        initialised = False
        if provider.needs_initialisation(context):
            provider.initialise(context)
            initialised = True

        record = self._launch(provider, context, spec)
        if wait:
            self._wait_until_ready(provider, context, spec, record)

        state = self._state.load_infra()
        stored = InfraRecord(
            instance=spec.name,
            provider=spec.provider,
            pid=record.pid,
            port=context.port,
            host=spec.host,
            data_dir=str(context.data_dir),
            log_dir=str(context.log_dir),
            version=spec.version,
            creation_time=record.creation_time,
            started_at=self._clock(),
            ownership=InfraOwnership.GLOBAL if global_ownership else InfraOwnership.PROJECT,
        )
        stored = self._claim(state, stored, project_id, global_ownership)
        return StartResult(
            name=spec.name,
            started=True,
            already_running=False,
            initialised=initialised,
            record=stored,
        )

    def _launch(
        self, provider: InfraProvider, context: InfraContext, spec: InfrastructureSpec
    ) -> ProcessRecord:
        argv = tuple(str(part) for part in provider.start_command(context))
        return self._supervisor.start(
            LaunchSpec(
                name=f"infra:{spec.name}",
                argv=argv,
                working_dir=context.data_dir,
                env=context.environment,
                stdout_path=context.log_dir / f"{spec.name}.out.log",
                stderr_path=context.log_dir / f"{spec.name}.err.log",
                stop_timeout=30.0,
            ),
            project_id="devbox-infrastructure",
        )

    def _wait_until_ready(
        self,
        provider: InfraProvider,
        context: InfraContext,
        spec: InfrastructureSpec,
        record: ProcessRecord,
    ) -> None:
        healthcheck = spec.healthcheck or provider.healthcheck(context)

        def died() -> str | None:
            status = self._supervisor.status(record)
            if status.state is ProcessState.RUNNING:
                return None
            code = "" if status.exit_code is None else f" with exit code {status.exit_code}"
            return f"the server process exited{code}"

        result = wait_for_health(
            probe_for(healthcheck), healthcheck, sleep=self._sleep, should_abort=died
        )
        if result.healthy:
            return

        self._supervisor.stop(record, timeout=10.0)
        log = context.log_dir / f"{spec.name}.err.log"
        msg = f"{spec.name} ({spec.provider}) did not become ready: {result.render_failure()}"
        raise InfrastructureError(
            msg,
            hint=f"Look at {log} for the server's own diagnostics.",
        )

    def _claim(
        self,
        state: InfraState,
        record: InfraRecord,
        project_id: str,
        global_ownership: bool,
    ) -> InfraRecord:
        updated = record
        if global_ownership:
            updated = replace(record, ownership=InfraOwnership.GLOBAL)
        if project_id:
            updated = updated.with_claim(project_id)
        self._state.save_infra(state.with_instance(updated))
        return updated

    # -- stop ----------------------------------------------------------------
    def down(
        self,
        spec: InfrastructureSpec,
        *,
        project_id: str = "",
        force_global: bool = False,
    ) -> StopResult:
        """Stop an instance, respecting who owns it.

        Called from ``devbox down`` with a *project_id* — in which case the
        ownership rules apply — or from ``devbox infra down`` with
        *force_global*, which is the only way to stop a globally owned instance.
        """
        if spec.mode is Mode.EXTERNAL:
            return StopResult(
                name=spec.name,
                stopped=False,
                was_running=False,
                detail="external: Devbox does not stop this",
            )

        state = self._state.load_infra()
        record = state.get(spec.name)
        if record is None:
            return StopResult(name=spec.name, stopped=True, was_running=False, detail="not running")

        if not force_global and project_id:
            allowed, reason = record.may_be_stopped_by_project(project_id)
            released = record.without_claim(project_id)
            self._state.save_infra(state.with_instance(released))
            if not allowed:
                return StopResult(
                    name=spec.name,
                    stopped=False,
                    was_running=True,
                    refused=True,
                    detail=f"left running: {reason}",
                )
            record = released

        if not self._is_alive(record):
            self._state.save_infra(self._state.load_infra().without_instance(spec.name))
            return StopResult(
                name=spec.name, stopped=True, was_running=False, detail="was not running"
            )

        self._graceful_stop(spec, record)
        outcome = self._supervisor.stop(_as_process_record(record), timeout=30.0)
        self._state.save_infra(self._state.load_infra().without_instance(spec.name))
        # The data directory is deliberately left alone: it holds every project's
        # databases and is not this operation's to delete.
        return StopResult(
            name=spec.name,
            stopped=outcome.stopped,
            was_running=True,
            refused=outcome.refused,
            detail=outcome.detail,
        )

    def _graceful_stop(self, spec: InfrastructureSpec, record: InfraRecord) -> None:
        """Ask the server to shut down cleanly before signalling it.

        PostgreSQL that is signalled rather than checkpointed spends the next
        start-up in recovery; a developer sees "database is starting up" and
        assumes Devbox broke something.
        """
        provider = get_provider(spec.provider)
        argv = provider.stop_command(self.context_for(spec))
        if argv is None:
            return
        result = self._runner.run(
            [str(part) for part in argv],
            env=dict(self.context_for(spec).environment),
            timeout=45.0,
        )
        if result.ok:
            # Give the server a moment to release its port before the caller
            # decides whether the process is gone.
            self._sleep(0.2)
        del record

    # -- claims --------------------------------------------------------------
    def release_project(self, project_id: str) -> tuple[str, ...]:
        """Drop a project's claims without stopping anything.  Returns the names."""
        state = self._state.load_infra()
        released: list[str] = []
        for name, record in state.instances.items():
            if project_id in record.claims:
                state = state.with_instance(record.without_claim(project_id))
                released.append(name)
        if released:
            self._state.save_infra(state)
        return tuple(sorted(released))

    def all_instances(self) -> tuple[InfraRecord, ...]:
        state = self._state.load_infra()
        return tuple(state.instances[name] for name in sorted(state.instances))

    def log_paths(self, spec: InfrastructureSpec) -> tuple[Path, Path]:
        log_dir = self.paths.infra_logs_dir(spec.provider, spec.name)
        return log_dir / f"{spec.name}.out.log", log_dir / f"{spec.name}.err.log"


def _as_process_record(record: InfraRecord) -> ProcessRecord:
    """Adapt an infrastructure record to the supervisor's process record."""
    return ProcessRecord(
        project_id="devbox-infrastructure",
        service_id=f"infra:{record.instance}",
        pid=record.pid,
        command=(),
        working_dir=record.data_dir,
        started_at=record.started_at,
        creation_time=record.creation_time,
        process_group=record.pid,
    )


def refresh_creation_time(record: InfraRecord) -> InfraRecord:
    """Fill in a missing creation time for a record written by an older build."""
    if record.creation_time is not None:
        return record
    return replace(record, creation_time=process_creation_time(record.pid))
