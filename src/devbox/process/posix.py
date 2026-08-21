"""POSIX supervisor — process groups.

Not a fallback and not a stub: this is what makes the whole orchestrator
integration-testable in a Linux container with no Node, no PostgreSQL and no
network.  ``up``, ``ps``, ``logs``, ``exec`` and ``down`` all run their real code
paths against real processes here; only the Job Object primitive differs from
Windows.

The child is started with ``start_new_session=True``, so it becomes the leader of
a new process group whose id equals its PID.  Signalling the *group* reaches
every descendant — the POSIX equivalent of a Job Object — so ``devbox down``
takes the whole tree with it, not just the process Devbox launched.
"""

from __future__ import annotations

import os
import signal
import subprocess

from devbox.process.base import BaseSupervisor, LaunchOptions, StartupExtras
from devbox.process.supervisor import LaunchSpec, ProcessRecord, SupervisorCapabilities

__all__ = ["PosixSupervisor"]


class PosixSupervisor(BaseSupervisor):
    """Supervises processes with POSIX process groups."""

    @property
    def name(self) -> str:
        return "posix"

    @property
    def capabilities(self) -> SupervisorCapabilities:
        return SupervisorCapabilities(
            kills_process_tree=True,
            reports_exit_code_across_invocations=False,
            graceful_signal="SIGTERM to the process group",
            notes=(
                "A descendant that calls setsid() for itself escapes the group, "
                "exactly as it would escape a Windows Job Object it broke away from.",
            ),
        )

    def _launch_options(self, spec: LaunchSpec) -> LaunchOptions:
        del spec
        return LaunchOptions(start_new_session=True)

    def _after_start(self, spec: LaunchSpec, process: subprocess.Popen[bytes]) -> StartupExtras:
        del spec
        # start_new_session makes the child a group leader: pgid == pid.
        return StartupExtras(process_group=process.pid)

    def _signal_graceful(self, record: ProcessRecord) -> bool:
        return self._signal_group(record, signal.SIGTERM)

    def _terminate_tree(self, record: ProcessRecord) -> None:
        self._signal_group(record, signal.SIGKILL)

    def _signal_group(self, record: ProcessRecord, signum: int) -> bool:
        """Signal the whole group, falling back to the single PID.

        Ownership has already been verified by the caller; the fallback exists
        because a process group can disappear between the check and the signal.
        """
        group = record.process_group or record.pid
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            try:
                os.kill(record.pid, signum)
            except (ProcessLookupError, PermissionError):
                return False
            return True
        except PermissionError:
            return False
        return True
