"""Windows supervisor — Job Objects.

Windows has no ``killpg``.  ``npm run dev`` starts ``node``, which starts a
bundler and a watcher; terminating the ``npm`` PID leaves all of them running and
holding port 3000.  A Job Object is the OS-supported way to treat that whole tree
as one unit, so that is what Devbox uses.

Design points, all of which matter and none of which are obvious:

**The job is named, not anonymous.**
    ``devbox up`` starts services detached and exits.  A later ``devbox down`` is
    a *different* process, so it must be able to reach the job again:
    ``OpenJobObjectW`` on ``Local\\Devbox-<project>-<service>``.  The ``Local\\``
    namespace scopes the name to the current logon session, so nothing on
    another user's desktop can open it.  The name is stored in the state record.

**``KILL_ON_JOB_CLOSE`` is off by default.**
    With it on, the tree would die the moment ``devbox up`` exits — which is
    precisely what detached services must not do.  It is switched on only for
    foreground mode (``devbox up --attach``), where Ctrl-C should take everything
    down with it.

**Graceful stop is only possible in foreground mode, and that is a trade-off,
not an omission.**
    ``GenerateConsoleCtrlEvent`` — what ``os.kill(pid, CTRL_BREAK_EVENT)`` calls
    — signals process groups attached to the *caller's* console.  A detached
    service is created with ``CREATE_NO_WINDOW`` precisely so that it has no
    console shared with Devbox and survives the terminal being closed, which
    also means no console control event can reach it.  Sending one anyway would
    at best do nothing and at worst succeed without delivering, making every
    ``devbox down`` wait the full ``stop_timeout`` for a signal nobody received.

    So: a detached service is stopped with ``TerminateJobObject``.  Graceful
    shutdown where it actually matters — a database mid-checkpoint — is done by
    the provider's own stop command (``pg_ctl stop -m fast``,
    ``mysqladmin shutdown``), which is both available and more correct than a
    console event.  In foreground mode the child shares Devbox's console, and
    then Ctrl-Break is sent first.

**The assignment race.**
    ``subprocess.Popen`` does not expose the primary thread handle, so a process
    cannot be created suspended and resumed after being assigned to the job.  A
    descendant spawned between ``CreateProcess`` and ``AssignProcessToJobObject``
    would escape.  The window is microseconds; no interpreter or package manager
    has finished start-up in that time.  Recorded in docs/limitations.md.

This module cannot execute in the Linux development container.  It is exercised
by the ``windows-latest`` CI job (``tests/windows/``) and nowhere else — no
result from it is ever reported as verified from a mock.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from collections.abc import Callable

from devbox.host import winapi
from devbox.host.platform_info import is_windows
from devbox.process.base import BaseSupervisor, LaunchOptions, StartupExtras
from devbox.process.supervisor import LaunchSpec, ProcessRecord, SupervisorCapabilities
from devbox.security.command import creation_flags

__all__ = ["WindowsSupervisor", "job_object_name"]

_JOB_NAME_PREFIX = "Local\\Devbox"
MAX_OBJECT_NAME = 255
"""Win32 object names are bounded by MAX_PATH; 255 leaves room for the prefix."""

_MAX_SERVICE_PART = 40


def job_object_name(project_id: str, service_id: str) -> str:
    """Deterministic per-service Job Object name, valid as a Win32 object name.

    Three things have to hold at once, and the third is easy to miss:

    * **legal** — project and service names may contain characters that are
      illegal in an object name, and a ``\\`` would change the namespace
      entirely, so the project is hashed and the service is sanitised;
    * **deterministic** — a later ``devbox down`` must derive the same name from
      the same project, or it cannot reach the job;
    * **bounded** — an object name longer than MAX_PATH makes
      ``CreateJobObjectW`` fail.  A long service name is therefore truncated and
      given a short digest of its full value, so two long names that share a
      prefix still get two different jobs.
    """
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    safe_service = "".join(char if char.isalnum() or char in "-_." else "_" for char in service_id)
    if len(safe_service) > _MAX_SERVICE_PART:
        suffix = hashlib.sha256(service_id.encode("utf-8")).hexdigest()[:8]
        safe_service = f"{safe_service[: _MAX_SERVICE_PART - 9]}-{suffix}"
    # Prefix (13) + digest (16) + separators + service (<= 40) is always well
    # inside MAX_OBJECT_NAME; the test pins that so a future change cannot
    # quietly break it.
    return f"{_JOB_NAME_PREFIX}-{digest}-{safe_service}"


class WindowsSupervisor(BaseSupervisor):
    """Supervises process trees with Windows Job Objects."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        super().__init__(clock=clock)
        self._jobs: dict[str, winapi.JobObject] = {}
        self._project_id = ""
        # Services started in foreground mode share Devbox's console and can
        # therefore be Ctrl-Break'd; detached ones cannot.  See the module
        # docstring.
        self._console_attached: set[str] = set()

    @property
    def name(self) -> str:
        return "windows"

    @property
    def capabilities(self) -> SupervisorCapabilities:
        return SupervisorCapabilities(
            kills_process_tree=True,
            reports_exit_code_across_invocations=False,
            graceful_signal="CTRL_BREAK_EVENT to the process group",
            notes=(
                "A descendant created with CREATE_BREAKAWAY_FROM_JOB escapes the job; "
                "Devbox does not grant that flag to anything it starts.",
            ),
        )

    def start(self, spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        self._project_id = project_id
        return super().start(spec, project_id=project_id)

    def _launch_options(self, spec: LaunchSpec) -> LaunchOptions:
        foreground = spec.kill_on_supervisor_exit
        if foreground:
            self._console_attached.add(spec.name)
        else:
            self._console_attached.discard(spec.name)
        return LaunchOptions(
            creation_flags=creation_flags(
                new_process_group=True,
                own_console=not foreground,
            )
        )

    def _after_start(self, spec: LaunchSpec, process: subprocess.Popen[bytes]) -> StartupExtras:
        """Create the job and put the child in it, as early as possible."""
        name = job_object_name(self._project_id, spec.name)
        job = winapi.JobObject.create(
            name,
            kill_on_close=spec.kill_on_supervisor_exit,
            object_name=name,
        )
        try:
            job.assign_pid(process.pid)
        except Exception:
            # Never leave a started process unsupervised: if it cannot be put in
            # a job, stop it rather than leaking an unkillable tree.
            job.close()
            process.kill()
            process.wait(timeout=10)
            raise
        self._jobs[spec.name] = job
        return StartupExtras(job_name=name, process_group=process.pid)

    def _signal_graceful(self, record: ProcessRecord) -> bool:
        """Ctrl-Break the process group, when the process can actually receive it.

        Returning ``False`` makes the caller terminate the job immediately
        instead of waiting ``stop_timeout`` for an event that was never
        delivered — which is what would happen for every detached service, since
        those deliberately have no console shared with Devbox.
        """
        if not is_windows():  # pragma: no cover - guard for accidental use
            return False
        if record.service_id not in self._console_attached:
            return False
        try:
            os.kill(record.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except (OSError, ValueError, AttributeError):
            return False
        return True

    def _terminate_tree(self, record: ProcessRecord) -> None:
        """Terminate every process in the job — including grandchildren."""
        job = self._jobs.get(record.service_id)
        if job is not None:
            job.terminate()
            return

        # Different Devbox invocation: reopen the job by name.
        reopened = winapi.JobObject.open(record.job_name) if record.job_name else None
        if reopened is not None:
            try:
                reopened.terminate()
            finally:
                reopened.close()
            return

        # Last resort: the job is gone (rebooted, or the record predates it).
        # Terminate the single process; descendants are reported, not pretended
        # away, by the caller re-running status afterwards.
        self._terminate_single(record)

    @staticmethod
    def _terminate_single(record: ProcessRecord) -> None:
        try:
            os.kill(record.pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        for job in self._jobs.values():
            job.close()
        self._jobs.clear()
        self._console_attached.clear()
        super().close()
