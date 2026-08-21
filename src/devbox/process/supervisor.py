"""The process supervision contract.

Devbox starts services **detached**: ``devbox up`` returns once everything is
healthy, and a later ``devbox ps``, ``devbox logs`` or ``devbox down`` — a
different OS process, possibly days later — must be able to find and stop them.
Two consequences shape this module:

* a supervisor is driven by a :class:`ProcessRecord` read back from disk, not by
  an in-memory handle it happens to still hold;
* every record carries the process *creation time*, so "PID 8132" can be proven
  to still be the process Devbox started rather than whatever inherited that
  number after a reboot.

Three implementations satisfy the protocol: :mod:`devbox.process.windows`
(Job Objects), :mod:`devbox.process.posix` (process groups — how the integration
suite runs on Linux) and :mod:`devbox.process.fake` (deterministic, no OS
process at all).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from devbox.host.procinfo import ProcessIdentity

__all__ = [
    "LaunchSpec",
    "ProcessRecord",
    "ProcessState",
    "ProcessStatus",
    "ProcessSupervisor",
    "StopOutcome",
]


class ProcessState(StrEnum):
    """What Devbox can prove about a recorded process, right now."""

    RUNNING = "running"
    """Alive, and the creation time matches the record."""

    EXITED = "exited"
    """The PID is gone."""

    STOPPED = "stopped"
    """Devbox stopped it during this invocation."""

    PID_REUSED = "pid-reused"
    """The PID is alive but belongs to a *different* process now.

    Devbox refuses to touch it.  This is the state that keeps ``devbox down``
    from killing an unrelated application after a reboot."""

    UNKNOWN = "unknown"
    """No record, or ownership could not be established."""

    @property
    def is_live(self) -> bool:
        return self is ProcessState.RUNNING

    @property
    def is_ours(self) -> bool:
        """False for anything Devbox must not signal."""
        return self in (ProcessState.RUNNING, ProcessState.STOPPED)


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything needed to start one process.

    ``argv`` is always a list — Devbox never builds a command line for a shell.
    ``env`` is the *complete* environment handed to the child, already carrying
    the per-service ``PATH``; the supervisor does not merge anything into it, so
    what a service sees is exactly what the caller decided.
    """

    name: str
    argv: tuple[str, ...]
    working_dir: Path
    env: Mapping[str, str]
    stdout_path: Path
    stderr_path: Path
    stop_timeout: float = 10.0
    kill_on_supervisor_exit: bool = False
    """Foreground mode: tear the tree down when Devbox itself exits."""

    def command_line(self) -> str:
        from devbox.security.command import quote_for_display

        return quote_for_display(self.argv)


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """A durable claim of ownership over one OS process.

    Persisted to ``%LOCALAPPDATA%\\Devbox\\state``.  Everything here is needed to
    find the process again from a *different* Devbox invocation, and to prove it
    is still the one we started.
    """

    project_id: str
    service_id: str
    pid: int
    command: tuple[str, ...]
    working_dir: str
    started_at: float
    creation_time: float | None = None
    stdout_log: str = ""
    stderr_log: str = ""
    supervisor: str = ""
    job_name: str = ""
    """Windows only: the named Job Object holding this process tree, so a later
    invocation can terminate the whole tree rather than a single PID."""

    process_group: int = 0
    """POSIX only: the process group id used to signal the tree."""

    exit_code: int | None = None
    stopped_at: float | None = None

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(pid=self.pid, creation_time=self.creation_time)

    @property
    def key(self) -> str:
        return f"{self.project_id}/{self.service_id}"

    @property
    def creation_time_or_zero(self) -> float:
        return 0.0 if self.creation_time is None else self.creation_time

    def command_line(self) -> str:
        from devbox.security.command import quote_for_display

        return quote_for_display(self.command)

    def with_exit(self, code: int | None, when: float) -> ProcessRecord:
        return replace(self, exit_code=code, stopped_at=when)


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    """The answer to "what is this service doing?"."""

    service_id: str
    state: ProcessState
    pid: int | None = None
    exit_code: int | None = None
    started_at: float | None = None
    record: ProcessRecord | None = None
    detail: str = ""

    @property
    def is_running(self) -> bool:
        return self.state.is_live

    @classmethod
    def unknown(cls, service_id: str, detail: str = "") -> ProcessStatus:
        return cls(service_id=service_id, state=ProcessState.UNKNOWN, detail=detail)


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """What a stop request actually did — reported verbatim, never guessed."""

    service_id: str
    stopped: bool
    was_running: bool
    graceful: bool = False
    exit_code: int | None = None
    detail: str = ""
    refused: bool = False
    """True when Devbox declined to signal the PID because it is not ours."""


@runtime_checkable
class ProcessSupervisor(Protocol):
    """Starts, inspects and stops supervised processes."""

    @property
    def name(self) -> str:
        """Short identifier persisted in records (``windows``/``posix``/``fake``)."""
        ...

    def start(self, spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        """Start a process and return the record proving we own it.

        Raises :class:`~devbox.errors.ProcessError` if the process cannot be
        launched at all (missing executable, bad working directory).  A process
        that starts and then immediately exits is *not* an error here — the
        caller decides, because a one-shot command is allowed to do exactly that.
        """
        ...

    def status(self, record: ProcessRecord) -> ProcessStatus:
        """Establish what the recorded process is doing, verifying ownership."""
        ...

    def stop(self, record: ProcessRecord, *, timeout: float | None = None) -> StopOutcome:
        """Stop the process **and its children**, gracefully then forcefully.

        Must refuse — and say so — when the PID no longer belongs to us.
        """
        ...

    def wait(self, record: ProcessRecord, *, timeout: float) -> ProcessStatus:
        """Block until the process exits or *timeout* elapses."""
        ...


@dataclass(slots=True)
class SupervisorCapabilities:
    """What a given supervisor can actually guarantee, for honest reporting."""

    kills_process_tree: bool = False
    reports_exit_code_across_invocations: bool = False
    graceful_signal: str = ""
    notes: Sequence[str] = field(default_factory=tuple)
