"""Fixtures for tests that spawn real processes."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from tests.conftest import minimal_child_environment

from devbox.process import LaunchSpec, ProcessRecord
from devbox.process.base import BaseSupervisor
from devbox.process.factory import create_supervisor
from devbox.process.supervisor import ProcessSupervisor
from devbox.testing.fake_runtime import build_fake_runtime

PROJECT_ID = "integration-project"


@pytest.fixture
def supervisor() -> Iterator[ProcessSupervisor]:
    """A real supervisor for this platform, guaranteed to leave nothing running."""
    instance = create_supervisor()
    started: list[ProcessRecord] = []
    original_start = instance.start

    def tracking_start(spec: LaunchSpec, *, project_id: str) -> ProcessRecord:
        record = original_start(spec, project_id=project_id)
        started.append(record)
        return record

    # Wrapping rather than subclassing keeps the object under test untouched.
    object.__setattr__(instance, "start", tracking_start)
    try:
        yield instance
    finally:
        for record in started:
            try:
                instance.stop(record, timeout=2.0)
            except Exception:
                pass
        if isinstance(instance, BaseSupervisor):
            instance.close()


@pytest.fixture
def fake_node(tmp_path: Path) -> Path:
    """A runnable fake Node 18 install."""
    return build_fake_runtime(
        tmp_path / "runtimes" / "node" / "18.19.0",
        name="node",
        version="18.19.0",
        extra_executables=("npm",),
    )


@pytest.fixture
def launch(tmp_path: Path, fake_node: Path) -> Callable[..., LaunchSpec]:
    """Build a LaunchSpec running the fake runtime with the given arguments."""
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
        env: dict[str, str] | None = None,
        stop_timeout: float = 5.0,
    ) -> LaunchSpec:
        root = runtime or fake_node
        child_env = minimal_child_environment(
            PATH=str(root),
            DEVBOX_FAKE_RUNTIME_VERSION=root.name,
            DEVBOX_FAKE_RUNTIME_NAME=name,
        )
        child_env.update(env or {})
        return LaunchSpec(
            name=name,
            argv=(sys.executable, str(root / "runtime_main.py"), *args),
            working_dir=working_dir or working,
            env=child_env,
            stdout_path=logs / f"{name}.out.log",
            stderr_path=logs / f"{name}.err.log",
            stop_timeout=stop_timeout,
        )

    return _launch
