"""Choosing a supervisor.

The single place in the codebase where the running platform decides which
implementation is used.  Everything else receives a
:class:`~devbox.process.supervisor.ProcessSupervisor` and does not care.
"""

from __future__ import annotations

from devbox.host.platform_info import is_windows
from devbox.process.posix import PosixSupervisor
from devbox.process.supervisor import ProcessSupervisor

__all__ = ["create_supervisor"]


def create_supervisor(*, prefer: str = "") -> ProcessSupervisor:
    """Return the supervisor for this machine.

    *prefer* forces a specific implementation (``windows``, ``posix``, ``fake``)
    and exists for tests and for ``devbox test``; normal runs leave it empty.
    """
    if prefer == "fake":
        from devbox.process.fake import FakeSupervisor

        return FakeSupervisor()
    if prefer == "posix":
        return PosixSupervisor()
    if prefer == "windows" or (not prefer and is_windows()):
        from devbox.process.windows import WindowsSupervisor

        return WindowsSupervisor()
    return PosixSupervisor()
