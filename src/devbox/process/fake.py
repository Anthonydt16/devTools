"""Deterministic in-memory supervisor.

No OS process is created.  Used by unit tests that are about *orchestration* —
start order, healthcheck waiting, error propagation, state reconciliation — where
spawning a real process would only add latency and flakiness.

It is a genuine implementation of the protocol, not a mock: it enforces the same
rules the real supervisors do (a stopped process cannot be stopped twice, a
recycled PID is refused, the working directory must exist), so a test that passes
here is testing behaviour rather than a recording of calls.

Anything that must prove *real* process behaviour — process trees, log capture,
signal handling, PATH resolution — uses :class:`~devbox.process.posix.PosixSupervisor`
in ``tests/integration/`` instead.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field

from devbox.errors import ProcessError
from devbox.process.supervisor import (
    LaunchSpec,
    ProcessRecord,
    ProcessState,
    ProcessStatus,
    StopOutcome,
    SupervisorCapabilities,
)

__all__ = ["FakeProcess", "FakeSupervisor"]


@dataclass(slots=True)
class FakeProcess:
    """A simulated process."""

    record: ProcessRecord
    state: ProcessState = ProcessState.RUNNING
    exit_code: int | None = None
    graceful_stop: bool = True
    """False simulates a process that ignores the polite stop request."""

    stdout: str = ""
    stderr: str = ""
    signals: list[str] = field(default_factory=list)


class FakeSupervisor:
    """In-memory :class:`~devbox.process.supervisor.ProcessSupervisor`."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        first_pid: int = 10_000,
    ) -> None:
        counter = itertools.count(0.0, 1.0) if clock is None else None
        self._clock: Callable[[], float] = clock or (lambda: next(counter))  # type: ignore[arg-type]
        self._pids = itertools.count(first_pid)
        # Stands in for the OS process table: pid -> creation time.  Ownership is
        # decided by comparing against it, exactly as the real supervisors do.
        self._live: dict[int, float] = {}
        self.processes: dict[str, FakeProcess] = {}
        self.started: list[LaunchSpec] = []
        self.fail_to_start: set[str] = set()
        self.exit_immediately: dict[str, int] = {}
        self.ignore_graceful_stop: set[str] = set()

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> SupervisorCapabilities:
        return SupervisorCapabilities(
            kills_process_tree=True,
            reports_exit_code_across_invocations=True,
            graceful_signal="simulated",
        )

    # -- protocol ------------------------------------------------------------
    def start(self, spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        if not spec.argv:
            msg = f"{spec.name}: refusing to start an empty command"
            raise ProcessError(msg)
        if not spec.working_dir.is_dir():
            msg = f"{spec.name}: working directory does not exist: {spec.working_dir}"
            raise ProcessError(msg)
        if spec.name in self.fail_to_start:
            msg = f"{spec.name}: executable not found: {spec.argv[0]}"
            raise ProcessError(msg)

        pid = next(self._pids)
        now = self._clock()
        record = ProcessRecord(
            project_id=project_id,
            service_id=spec.name,
            pid=pid,
            command=spec.argv,
            working_dir=str(spec.working_dir),
            started_at=now,
            creation_time=float(pid),
            stdout_log=str(spec.stdout_path),
            stderr_log=str(spec.stderr_path),
            supervisor=self.name,
            process_group=pid,
        )
        self._live[pid] = float(pid)
        process = FakeProcess(record=record)
        if spec.name in self.exit_immediately:
            process.state = ProcessState.EXITED
            process.exit_code = self.exit_immediately[spec.name]
            self._live.pop(pid, None)
        if spec.name in self.ignore_graceful_stop:
            process.graceful_stop = False
        self.processes[spec.name] = process
        self.started.append(spec)
        return record

    def status(self, record: ProcessRecord) -> ProcessStatus:
        state, detail = self._verify(record)
        process = self.processes.get(record.service_id)
        if state is ProcessState.RUNNING and process is not None:
            state = process.state
        return ProcessStatus(
            service_id=record.service_id,
            state=state,
            pid=record.pid,
            exit_code=process.exit_code if process is not None else None,
            started_at=record.started_at,
            record=record,
            detail=detail,
        )

    def _verify(self, record: ProcessRecord) -> tuple[ProcessState, str]:
        """Same ownership rule as the real supervisors: PID *and* creation time."""
        live = self._live.get(record.pid)
        if live is None:
            return ProcessState.EXITED, ""
        if record.creation_time is None:
            return (
                ProcessState.PID_REUSED,
                f"pid {record.pid} is alive but the record has no creation time",
            )
        if live != record.creation_time:
            return (
                ProcessState.PID_REUSED,
                f"pid {record.pid} is alive but belongs to another program now",
            )
        return ProcessState.RUNNING, ""

    def stop(self, record: ProcessRecord, *, timeout: float | None = None) -> StopOutcome:
        del timeout
        state, detail = self._verify(record)
        if state is ProcessState.PID_REUSED:
            return StopOutcome(
                service_id=record.service_id,
                stopped=False,
                was_running=False,
                refused=True,
                detail=detail,
            )
        process = self.processes.get(record.service_id)
        if process is None:
            return StopOutcome(
                service_id=record.service_id,
                stopped=True,
                was_running=False,
                graceful=True,
                detail="no such process",
            )
        if state is ProcessState.EXITED or process.state in (
            ProcessState.EXITED,
            ProcessState.STOPPED,
        ):
            return StopOutcome(
                service_id=record.service_id,
                stopped=True,
                was_running=False,
                graceful=True,
                exit_code=process.exit_code,
                detail="already stopped",
            )
        graceful = process.graceful_stop
        process.signals.append("graceful" if graceful else "terminate")
        process.state = ProcessState.STOPPED
        process.exit_code = 0 if graceful else -1
        self._live.pop(record.pid, None)
        return StopOutcome(
            service_id=record.service_id,
            stopped=True,
            was_running=True,
            graceful=graceful,
            exit_code=process.exit_code,
            detail="" if graceful else "terminated the process tree",
        )

    def wait(self, record: ProcessRecord, *, timeout: float) -> ProcessStatus:
        del timeout
        return self.status(record)

    def close(self) -> None:
        self.processes.clear()
        self._live.clear()

    # -- test controls -------------------------------------------------------
    def simulate_exit(self, service_id: str, code: int = 0) -> None:
        """Make a running fake process look like it exited on its own."""
        process = self.processes[service_id]
        process.state = ProcessState.EXITED
        process.exit_code = code
        self._live.pop(process.record.pid, None)

    def simulate_pid_reuse(self, service_id: str) -> None:
        """Hand the service's PID to an unrelated program.

        This is what a reboot looks like from Devbox's point of view: the number
        is alive again, but it is somebody else's process.  Every code path that
        would signal it must refuse.
        """
        process = self.processes[service_id]
        self._live[process.record.pid] = process.record.creation_time_or_zero + 999.0

    def simulate_crash(self, service_id: str) -> None:
        """The process is gone and Devbox never observed the exit.

        This is the state left behind when Devbox itself is killed.
        """
        process = self.processes.pop(service_id)
        self._live.pop(process.record.pid, None)
