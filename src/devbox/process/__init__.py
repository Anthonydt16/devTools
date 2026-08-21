"""Process supervision: start, inspect and stop services and their children."""

from devbox.process.factory import create_supervisor
from devbox.process.supervisor import (
    LaunchSpec,
    ProcessRecord,
    ProcessState,
    ProcessStatus,
    ProcessSupervisor,
    StopOutcome,
)

__all__ = [
    "LaunchSpec",
    "ProcessRecord",
    "ProcessState",
    "ProcessStatus",
    "ProcessSupervisor",
    "StopOutcome",
    "create_supervisor",
]
