"""Behaviour shared by the real supervisors.

Log-file handling, working-directory checks, ownership verification and the
"graceful, then forceful" stop loop are identical on Windows and POSIX; only the
signalling primitive differs.  Keeping the shared half here means the Windows
implementation is small enough to review carefully, which matters because it is
the part the Linux test suite cannot execute.
"""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from devbox.errors import ProcessError
from devbox.host.procinfo import ProcessIdentity, process_creation_time
from devbox.process.supervisor import (
    LaunchSpec,
    ProcessRecord,
    ProcessState,
    ProcessStatus,
    StopOutcome,
)

__all__ = ["BaseSupervisor", "LaunchOptions", "StartupExtras"]

_POLL_INTERVAL = 0.05


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """Platform-specific ``Popen`` options.

    A typed object rather than a ``**kwargs`` mapping so the call to ``Popen``
    stays type-checked: an unpacked ``dict[str, object]`` silently defeats the
    overloads and would hide a real mistake.
    """

    start_new_session: bool = False
    """POSIX: make the child a process-group leader so the group can be signalled."""

    creation_flags: int = 0
    """Windows: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP."""


@dataclass(frozen=True, slots=True)
class StartupExtras:
    """Bookkeeping produced by the platform hook and stored in the record."""

    job_name: str = ""
    process_group: int = 0


class BaseSupervisor(ABC):
    """Common machinery; subclasses provide the platform-specific parts."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._handles: dict[str, subprocess.Popen[bytes]] = {}
        self._exits: dict[str, int] = {}

    # -- to implement --------------------------------------------------------
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def _launch_options(self, spec: LaunchSpec) -> LaunchOptions:
        """Platform-specific ``Popen`` options (process group, creation flags)."""

    @abstractmethod
    def _after_start(self, spec: LaunchSpec, process: subprocess.Popen[bytes]) -> StartupExtras:
        """Hook run immediately after launch; returns extra record fields.

        This is where Windows assigns the process to its Job Object.
        """

    @abstractmethod
    def _signal_graceful(self, record: ProcessRecord) -> bool:
        """Ask the tree to stop politely.  Returns False if the request could not
        be delivered (in which case the caller escalates immediately)."""

    @abstractmethod
    def _terminate_tree(self, record: ProcessRecord) -> None:
        """Forcefully terminate the process **and every descendant**."""

    # -- protocol ------------------------------------------------------------
    def start(self, spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        working_dir = spec.working_dir
        if not working_dir.is_dir():
            msg = f"{spec.name}: working directory does not exist: {working_dir}"
            raise ProcessError(
                msg,
                hint="Check 'working_dir' in devbox.yml; it is relative to the project root.",
            )
        if not spec.argv:
            msg = f"{spec.name}: refusing to start an empty command"
            raise ProcessError(msg)

        options = self._launch_options(spec)
        stdout, stderr = self._open_logs(spec)
        try:
            process = subprocess.Popen(  # noqa: S603 - argv list, never a shell
                list(spec.argv),
                cwd=str(working_dir),
                env=dict(spec.env),
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=options.start_new_session,
                creationflags=options.creation_flags,
            )
        except FileNotFoundError as exc:
            msg = f"{spec.name}: executable not found: {spec.argv[0]}"
            raise ProcessError(
                msg,
                hint=(
                    "The command is resolved through the PATH built for this service. "
                    "Run 'devbox status' to check the runtime is installed."
                ),
            ) from exc
        except PermissionError as exc:
            msg = f"{spec.name}: not executable: {spec.argv[0]} ({exc})"
            raise ProcessError(msg) from exc
        except OSError as exc:
            msg = f"{spec.name}: cannot start {spec.command_line()}: {exc}"
            raise ProcessError(msg) from exc
        finally:
            # The child holds its own duplicated descriptors; ours are done.
            stdout.close()
            stderr.close()

        extra = self._after_start(spec, process)
        self._handles[spec.name] = process

        return ProcessRecord(
            project_id=project_id,
            service_id=spec.name,
            pid=process.pid,
            command=spec.argv,
            working_dir=str(working_dir),
            started_at=self._clock(),
            creation_time=process_creation_time(process.pid),
            stdout_log=str(spec.stdout_path),
            stderr_log=str(spec.stderr_path),
            supervisor=self.name,
            job_name=extra.job_name,
            process_group=extra.process_group,
        )

    def status(self, record: ProcessRecord) -> ProcessStatus:
        handle = self._handles.get(record.service_id)
        if handle is not None and handle.pid == record.pid:
            code = handle.poll()
            if code is not None:
                self._exits[record.service_id] = code
                return ProcessStatus(
                    service_id=record.service_id,
                    state=ProcessState.EXITED,
                    pid=record.pid,
                    exit_code=code,
                    started_at=record.started_at,
                    record=record,
                )

        state, detail = self._verify(record)
        return ProcessStatus(
            service_id=record.service_id,
            state=state,
            pid=record.pid,
            exit_code=self._exits.get(record.service_id, record.exit_code),
            started_at=record.started_at,
            record=record,
            detail=detail,
        )

    def stop(self, record: ProcessRecord, *, timeout: float | None = None) -> StopOutcome:
        budget = record_timeout(record, timeout)
        state, detail = self._verify(record)

        if state is ProcessState.EXITED:
            return StopOutcome(
                service_id=record.service_id,
                stopped=True,
                was_running=False,
                graceful=True,
                exit_code=self._exits.get(record.service_id, record.exit_code),
                detail="already stopped",
            )
        if state is ProcessState.PID_REUSED:
            return StopOutcome(
                service_id=record.service_id,
                stopped=False,
                was_running=False,
                refused=True,
                detail=detail,
            )

        graceful = self._signal_graceful(record)
        if graceful and self._wait_for_exit(record, budget):
            return StopOutcome(
                service_id=record.service_id,
                stopped=True,
                was_running=True,
                graceful=True,
                exit_code=self._collect_exit(record),
            )

        self._terminate_tree(record)
        if not self._wait_for_exit(record, 5.0):
            msg = f"{record.service_id}: process {record.pid} is still alive after being terminated"
            raise ProcessError(
                msg,
                hint="Another program may be holding the process; check Task Manager.",
            )
        return StopOutcome(
            service_id=record.service_id,
            stopped=True,
            was_running=True,
            graceful=False,
            exit_code=self._collect_exit(record),
            detail=(
                f"did not exit within {budget:g}s, terminated the process tree"
                if graceful
                else "terminated the process tree"
            ),
        )

    def wait(self, record: ProcessRecord, *, timeout: float) -> ProcessStatus:
        self._wait_for_exit(record, timeout)
        return self.status(record)

    # -- helpers -------------------------------------------------------------
    def _verify(self, record: ProcessRecord) -> tuple[ProcessState, str]:
        """Decide whether the recorded PID is still the process we started."""
        from devbox.host.procinfo import process_is_alive

        if not process_is_alive(record.pid):
            return ProcessState.EXITED, ""
        identity = ProcessIdentity(pid=record.pid, creation_time=record.creation_time)
        if identity.matches_live_process():
            return ProcessState.RUNNING, ""
        if not identity.ownership_is_verifiable:
            return (
                ProcessState.PID_REUSED,
                f"pid {record.pid} is alive but the state record has no creation time, "
                "so ownership cannot be proven; refusing to signal it",
            )
        return (
            ProcessState.PID_REUSED,
            f"pid {record.pid} is alive but was created at a different time than the "
            "one Devbox started; it now belongs to another program",
        )

    def _wait_for_exit(self, record: ProcessRecord, timeout: float) -> bool:
        from devbox.host.procinfo import process_is_alive

        handle = self._handles.get(record.service_id)
        if handle is not None and handle.pid == record.pid:
            try:
                self._exits[record.service_id] = handle.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            return True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not process_is_alive(record.pid):
                return True
            time.sleep(_POLL_INTERVAL)
        return not process_is_alive(record.pid)

    def _collect_exit(self, record: ProcessRecord) -> int | None:
        return self._exits.get(record.service_id)

    @staticmethod
    def _open_logs(spec: LaunchSpec) -> tuple[BinaryIO, BinaryIO]:
        """Open the log files the child writes into directly.

        The child owns duplicated descriptors, so output keeps flowing after
        Devbox exits — which is what makes ``devbox logs`` work on a service
        started by an earlier invocation.  Append mode means a restart adds to
        the history instead of erasing it.
        """
        for path in (spec.stdout_path, spec.stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stdout = spec.stdout_path.open("ab")
            try:
                stderr = spec.stderr_path.open("ab")
            except OSError:
                stdout.close()
                raise
        except OSError as exc:
            msg = f"{spec.name}: cannot open log files: {exc}"
            raise ProcessError(msg) from exc
        return stdout, stderr

    def close(self) -> None:
        """Release Popen bookkeeping.  Does not stop anything."""
        self._handles.clear()


def record_timeout(record: ProcessRecord, timeout: float | None) -> float:
    del record
    return 10.0 if timeout is None else max(timeout, 0.0)
