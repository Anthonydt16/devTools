"""Fixtures for the Windows-only suite.

Everything here is skipped on Linux by ``tests/conftest.py`` with an explicit
reason, and really executed by the ``windows-latest`` GitHub Actions job.  No
result from this directory is ever reported as verified from a Linux run.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from devbox.process.supervisor import LaunchSpec, ProcessRecord
from devbox.testing.fake_runtime import build_fake_runtime

pytestmark = pytest.mark.windows

PROJECT_ID = "windows-suite"


@pytest.fixture
def windows_supervisor() -> Iterator[object]:
    """A real :class:`WindowsSupervisor`, guaranteed to leave nothing running."""
    from devbox.process.windows import WindowsSupervisor

    supervisor = WindowsSupervisor()
    started: list[ProcessRecord] = []
    original = supervisor.start

    def tracking(spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        record = original(spec, project_id=project_id)
        started.append(record)
        return record

    object.__setattr__(supervisor, "start", tracking)
    try:
        yield supervisor
    finally:
        for record in started:
            try:
                supervisor.stop(record, timeout=5.0)
            except Exception:
                pass
        supervisor.close()


@pytest.fixture
def fake_node(tmp_path: Path) -> Path:
    return build_fake_runtime(
        tmp_path / "runtimes" / "node" / "18.19.0",
        name="node",
        version="18.19.0",
        extra_executables=("npm",),
    )


@pytest.fixture
def launch(tmp_path: Path, fake_node: Path) -> Callable[..., LaunchSpec]:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    working = tmp_path / "work"
    working.mkdir(parents=True, exist_ok=True)

    def _launch(
        name: str,
        args: Sequence[str],
        *,
        runtime: Path | None = None,
        working_dir: Path | None = None,
    ) -> LaunchSpec:
        root = runtime or fake_node
        return LaunchSpec(
            name=name,
            argv=(sys.executable, str(root / "runtime_main.py"), *args),
            working_dir=working_dir or working,
            env={
                "PATH": str(root),
                "SystemRoot": "C:\\Windows",
                "DEVBOX_FAKE_RUNTIME_VERSION": "18.19.0",
                "DEVBOX_FAKE_RUNTIME_NAME": name,
            },
            stdout_path=logs / f"{name}.out.log",
            stderr_path=logs / f"{name}.err.log",
            stop_timeout=5.0,
        )

    return _launch
