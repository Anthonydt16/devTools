"""Persisting state to ``%LOCALAPPDATA%\\Devbox\\state``.

JSON, one file per project plus one for shared infrastructure.  Written
atomically (temp file then ``replace``), so a machine that loses power mid-write
finds either the old file or the new one — never half of either.

A file that *is* corrupt is quarantined rather than deleted: Devbox renames it to
``<name>.corrupt-<timestamp>`` and continues with empty state, reporting what it
did.  Losing the ability to stop a service is bad; silently deleting evidence of
why is worse.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devbox.errors import StateError
from devbox.host.paths import DevboxPaths
from devbox.process.supervisor import ProcessRecord
from devbox.state.models import STATE_VERSION, InfraRecord, InfraState, ProjectState

__all__ = ["StateStore", "StateWarning"]


@dataclass(frozen=True, slots=True)
class StateWarning:
    """Something recoverable that the user should still hear about."""

    message: str
    path: str = ""

    def render(self) -> str:
        return f"{self.message} ({self.path})" if self.path else self.message


@dataclass
class StateStore:
    """Reads and writes Devbox state."""

    paths: DevboxPaths
    clock: Callable[[], float] = time.time
    warnings: list[StateWarning] = field(default_factory=list)

    # -- locations -----------------------------------------------------------
    @property
    def projects_dir(self) -> Path:
        return self.paths.state / "projects"

    def project_file(self, project_id: str) -> Path:
        from devbox.security.paths import resolve_within

        return resolve_within(self.projects_dir, f"{project_id}.json", what="project state file")

    @property
    def infra_file(self) -> Path:
        return self.paths.state / "infrastructure.json"

    def ensure(self) -> StateStore:
        self.paths.ensure()
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        return self

    # -- projects ------------------------------------------------------------
    def load_project(self, project_id: str) -> ProjectState:
        """Read a project's state; unreadable state is quarantined, not fatal."""
        path = self.project_file(project_id)
        document = self._read_json(path)
        if document is None:
            return ProjectState(project_id=project_id)
        try:
            return _project_from_document(document, project_id)
        except (KeyError, TypeError, ValueError) as exc:
            self._quarantine(path, f"project state is not usable ({exc})")
            return ProjectState(project_id=project_id)

    def save_project(self, state: ProjectState) -> None:
        self.ensure()
        document = {
            "state_version": STATE_VERSION,
            "project_id": state.project_id,
            "project_name": state.project_name,
            "project_root": state.project_root,
            "environment": state.environment,
            "updated_at": self.clock(),
            "processes": {
                service_id: _record_to_document(record)
                for service_id, record in sorted(state.processes.items())
            },
        }
        self._write_json(self.project_file(state.project_id), document)

    def record_process(self, state: ProjectState, record: ProcessRecord) -> ProjectState:
        updated = state.with_process(record)
        self.save_project(updated)
        return updated

    def forget_process(self, state: ProjectState, service_id: str) -> ProjectState:
        updated = state.without_process(service_id)
        self.save_project(updated)
        return updated

    def all_projects(self) -> tuple[ProjectState, ...]:
        if not self.projects_dir.is_dir():
            return ()
        states: list[ProjectState] = []
        for path in sorted(self.projects_dir.glob("*.json")):
            states.append(self.load_project(path.stem))
        return tuple(states)

    def delete_project(self, project_id: str) -> None:
        self.project_file(project_id).unlink(missing_ok=True)

    # -- infrastructure ------------------------------------------------------
    def load_infra(self) -> InfraState:
        document = self._read_json(self.infra_file)
        if document is None:
            return InfraState()
        try:
            return _infra_from_document(document)
        except (KeyError, TypeError, ValueError) as exc:
            self._quarantine(self.infra_file, f"infrastructure state is not usable ({exc})")
            return InfraState()

    def save_infra(self, state: InfraState) -> None:
        self.ensure()
        document = {
            "state_version": STATE_VERSION,
            "updated_at": self.clock(),
            "instances": {
                name: _infra_record_to_document(record)
                for name, record in sorted(state.instances.items())
            },
        }
        self._write_json(self.infra_file, document)

    # -- low level -----------------------------------------------------------
    def _read_json(self, path: Path) -> Mapping[str, object] | None:
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"cannot read state file {path}: {exc}"
            raise StateError(msg) from exc
        if not raw.strip():
            self._quarantine(path, "state file is empty")
            return None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._quarantine(path, f"state file is not valid JSON ({exc.msg})")
            return None
        if not isinstance(document, dict):
            self._quarantine(path, "state file does not contain an object")
            return None
        version = document.get("state_version", STATE_VERSION)
        if version != STATE_VERSION:
            self._quarantine(path, f"state file uses unsupported version {version!r}")
            return None
        return document

    def _write_json(self, path: Path, document: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            msg = f"cannot write state file {path}: {exc}"
            raise StateError(
                msg,
                hint="Check that %LOCALAPPDATA%\\Devbox is writable and has free space.",
            ) from exc

    def _quarantine(self, path: Path, reason: str) -> None:
        """Move a damaged state file aside and carry on with empty state."""
        stamp = int(self.clock())
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.replace(target)
        except OSError:
            target = path
        self.warnings.append(
            StateWarning(
                message=f"{reason}; it was moved aside and Devbox continued with empty state",
                path=str(target),
            )
        )

    def take_warnings(self) -> tuple[StateWarning, ...]:
        """Return and clear accumulated warnings, for the CLI to display once."""
        collected = tuple(self.warnings)
        self.warnings.clear()
        return collected

    def cleanup_temporary_files(self) -> int:
        """Remove ``*.tmp`` left behind by an interrupted write.  Returns the count."""
        removed = 0
        for directory in (self.paths.state, self.projects_dir):
            if not directory.is_dir():
                continue
            for leftover in directory.glob("*.tmp"):
                leftover.unlink(missing_ok=True)
                removed += 1
        return removed


# ---------------------------------------------------------------------------
# (de)serialisation
# ---------------------------------------------------------------------------
def _record_to_document(record: ProcessRecord) -> dict[str, object]:
    return {
        "service_id": record.service_id,
        "pid": record.pid,
        "creation_time": record.creation_time,
        "command": list(record.command),
        "working_dir": record.working_dir,
        "started_at": record.started_at,
        "stdout_log": record.stdout_log,
        "stderr_log": record.stderr_log,
        "supervisor": record.supervisor,
        "job_name": record.job_name,
        "process_group": record.process_group,
        "exit_code": record.exit_code,
        "stopped_at": record.stopped_at,
    }


def _record_from_document(
    document: Mapping[str, object], project_id: str, service_id: str
) -> ProcessRecord:
    return ProcessRecord(
        project_id=project_id,
        service_id=str(document.get("service_id") or service_id),
        pid=_as_int(document, "pid"),
        creation_time=_as_optional_float(document, "creation_time"),
        command=tuple(_as_str_list(document, "command")),
        working_dir=str(document.get("working_dir", "")),
        started_at=float(_as_optional_float(document, "started_at") or 0.0),
        stdout_log=str(document.get("stdout_log", "")),
        stderr_log=str(document.get("stderr_log", "")),
        supervisor=str(document.get("supervisor", "")),
        job_name=str(document.get("job_name", "")),
        process_group=_as_int(document, "process_group", default=0),
        exit_code=_as_optional_int(document, "exit_code"),
        stopped_at=_as_optional_float(document, "stopped_at"),
    )


def _project_from_document(document: Mapping[str, object], project_id: str) -> ProjectState:
    raw_processes = document.get("processes") or {}
    if not isinstance(raw_processes, dict):
        msg = "'processes' must be an object"
        raise TypeError(msg)
    processes: dict[str, ProcessRecord] = {}
    for service_id, entry in raw_processes.items():
        if not isinstance(entry, dict):
            msg = f"process entry {service_id!r} must be an object"
            raise TypeError(msg)
        processes[str(service_id)] = _record_from_document(entry, project_id, str(service_id))
    return ProjectState(
        project_id=str(document.get("project_id") or project_id),
        project_name=str(document.get("project_name", "")),
        project_root=str(document.get("project_root", "")),
        environment=str(document.get("environment", "")),
        processes=processes,
        updated_at=float(_as_optional_float(document, "updated_at") or 0.0),
    )


def _infra_record_to_document(record: InfraRecord) -> dict[str, object]:
    return {
        "provider": record.provider,
        "pid": record.pid,
        "port": record.port,
        "host": record.host,
        "data_dir": record.data_dir,
        "log_dir": record.log_dir,
        "version": record.version,
        "creation_time": record.creation_time,
        "started_at": record.started_at,
        "ownership": record.ownership,
        "claims": list(record.claims),
    }


def _infra_from_document(document: Mapping[str, object]) -> InfraState:
    raw = document.get("instances") or {}
    if not isinstance(raw, dict):
        msg = "'instances' must be an object"
        raise TypeError(msg)
    instances: dict[str, InfraRecord] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            msg = f"instance entry {name!r} must be an object"
            raise TypeError(msg)
        instances[str(name)] = InfraRecord(
            instance=str(name),
            provider=str(entry.get("provider", "")),
            pid=_as_int(entry, "pid"),
            port=_as_int(entry, "port", default=0),
            host=str(entry.get("host", "127.0.0.1")),
            data_dir=str(entry.get("data_dir", "")),
            log_dir=str(entry.get("log_dir", "")),
            version=str(entry.get("version", "")),
            creation_time=_as_optional_float(entry, "creation_time"),
            started_at=float(_as_optional_float(entry, "started_at") or 0.0),
            ownership=str(entry.get("ownership", "project")),
            claims=tuple(_as_str_list(entry, "claims")),
        )
    return InfraState(
        instances=instances,
        updated_at=float(_as_optional_float(document, "updated_at") or 0.0),
    )


def _as_int(document: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    value = document.get(key)
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key!r} must be an integer, got {value!r}"
        raise TypeError(msg)
    return value


def _as_optional_int(document: Mapping[str, object], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key!r} must be an integer or null, got {value!r}"
        raise TypeError(msg)
    return value


def _as_optional_float(document: Mapping[str, object], key: str) -> float | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{key!r} must be a number or null, got {value!r}"
        raise TypeError(msg)
    return float(value)


def _as_str_list(document: Mapping[str, object], key: str) -> Sequence[str]:
    value = document.get(key) or []
    if not isinstance(value, list):
        msg = f"{key!r} must be a list, got {value!r}"
        raise TypeError(msg)
    return [str(item) for item in value]


def iter_records(state: ProjectState) -> Iterator[ProcessRecord]:
    for service_id in state.service_ids():
        record = state.record(service_id)
        if record is not None:
            yield record
