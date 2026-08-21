"""On-disk state.

Devbox starts services detached, so the only thing linking one invocation to the
next is this state.  Two rules govern it:

* **it is a claim, not a fact** — every record is re-verified against the OS
  before it is trusted, because the machine may have rebooted since;
* **it never authorises a kill on its own** — a PID is only actionable when its
  recorded creation time still matches the live process.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from devbox.process.supervisor import ProcessRecord

__all__ = [
    "STATE_VERSION",
    "InfraOwnership",
    "InfraRecord",
    "ProjectState",
    "project_id_for",
]

STATE_VERSION = 1

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def project_id_for(project_root: Path) -> str:
    """A stable identifier for a project directory.

    Derived from the *resolved absolute path*, so two clones of the same
    repository in two folders are two projects that can run side by side, and so
    the same folder keeps its identity across reboots and drive letters that do
    not change.  A readable slug is prefixed to make the state directory
    browsable when something goes wrong.
    """
    resolved = project_root.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    slug = _SLUG_RE.sub("-", resolved.name.lower()).strip("-") or "project"
    return f"{slug[:32]}-{digest}"


@dataclass(frozen=True, slots=True)
class ProjectState:
    """Everything Devbox remembers about one project between invocations."""

    project_id: str
    project_name: str = ""
    project_root: str = ""
    environment: str = ""
    processes: Mapping[str, ProcessRecord] = field(default_factory=dict)
    updated_at: float = 0.0
    state_version: int = STATE_VERSION

    def with_process(self, record: ProcessRecord) -> ProjectState:
        processes = dict(self.processes)
        processes[record.service_id] = record
        return replace(self, processes=processes)

    def without_process(self, service_id: str) -> ProjectState:
        processes = dict(self.processes)
        processes.pop(service_id, None)
        return replace(self, processes=processes)

    def record(self, service_id: str) -> ProcessRecord | None:
        return self.processes.get(service_id)

    def service_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.processes))


class InfraOwnership:
    """Who started a shared infrastructure instance.

    The distinction is the whole point of shared infrastructure: an instance
    started by ``devbox infra up`` belongs to the machine, and no project's
    ``devbox down`` may stop it.
    """

    GLOBAL = "global"
    """Started by ``devbox infra up``: only ``devbox infra down`` stops it."""

    PROJECT = "project"
    """Started on demand by ``devbox up``: stops when the last project releases it."""


@dataclass(frozen=True, slots=True)
class InfraRecord:
    """A running shared infrastructure instance."""

    instance: str
    provider: str
    pid: int
    port: int
    host: str = "127.0.0.1"
    data_dir: str = ""
    log_dir: str = ""
    version: str = ""
    creation_time: float | None = None
    started_at: float = 0.0
    ownership: str = InfraOwnership.PROJECT
    claims: tuple[str, ...] = ()
    """Project ids currently relying on this instance."""

    @property
    def is_global(self) -> bool:
        return self.ownership == InfraOwnership.GLOBAL

    def with_claim(self, project_id: str) -> InfraRecord:
        if project_id in self.claims:
            return self
        return replace(self, claims=tuple(sorted({*self.claims, project_id})))

    def without_claim(self, project_id: str) -> InfraRecord:
        return replace(self, claims=tuple(c for c in self.claims if c != project_id))

    def may_be_stopped_by_project(self, project_id: str) -> tuple[bool, str]:
        """Decide whether a project's ``devbox down`` may stop this instance.

        Returns ``(allowed, reason)``.  This single method is what non-regression
        test #2 exercises: shared infrastructure must survive a project shutting
        down.
        """
        if self.is_global:
            return False, "started by 'devbox infra up'; stop it with 'devbox infra down'"
        remaining = [claim for claim in self.claims if claim != project_id]
        if remaining:
            plural = "s" if len(remaining) > 1 else ""
            return False, f"still used by {len(remaining)} other project{plural}"
        return True, ""


@dataclass(frozen=True, slots=True)
class InfraState:
    """All shared infrastructure instances known to this machine."""

    instances: Mapping[str, InfraRecord] = field(default_factory=dict)
    updated_at: float = 0.0
    state_version: int = STATE_VERSION

    def get(self, instance: str) -> InfraRecord | None:
        return self.instances.get(instance)

    def with_instance(self, record: InfraRecord) -> InfraState:
        instances = dict(self.instances)
        instances[record.instance] = record
        return replace(self, instances=instances)

    def without_instance(self, instance: str) -> InfraState:
        instances = dict(self.instances)
        instances.pop(instance, None)
        return replace(self, instances=instances)
