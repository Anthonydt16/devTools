"""Supervisor semantics, exercised through the deterministic implementation.

These assert on the *rules* every supervisor must follow.  Real process
behaviour — trees, signals, log capture — is covered by
``tests/integration/test_process_supervisor.py`` with actual processes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.errors import ProcessError
from devbox.process import LaunchSpec, ProcessState
from devbox.process.fake import FakeSupervisor

pytestmark = pytest.mark.unit

PROJECT = "demo-abc123"


@pytest.fixture
def spec(tmp_path: Path) -> LaunchSpec:
    working = tmp_path / "app"
    working.mkdir()
    return LaunchSpec(
        name="backend",
        argv=("node", "server.js"),
        working_dir=working,
        env={"PATH": "/runtimes/node/18.19.0"},
        stdout_path=tmp_path / "logs" / "backend.out.log",
        stderr_path=tmp_path / "logs" / "backend.err.log",
    )


class TestStart:
    def test_start_returns_an_ownable_record(self, spec: LaunchSpec) -> None:
        record = FakeSupervisor().start(spec, project_id=PROJECT)
        assert record.pid > 0
        assert record.creation_time is not None
        assert record.command == ("node", "server.js")
        assert record.project_id == PROJECT

    def test_missing_working_directory_is_an_error(self, spec: LaunchSpec, tmp_path: Path) -> None:
        broken = LaunchSpec(**{**_fields(spec), "working_dir": tmp_path / "ghost"})
        with pytest.raises(ProcessError, match="working directory does not exist"):
            FakeSupervisor().start(broken, project_id=PROJECT)

    def test_empty_command_is_refused(self, spec: LaunchSpec) -> None:
        broken = LaunchSpec(**{**_fields(spec), "argv": ()})
        with pytest.raises(ProcessError, match="refusing to start an empty command"):
            FakeSupervisor().start(broken, project_id=PROJECT)

    def test_missing_executable_is_reported(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        supervisor.fail_to_start.add("backend")
        with pytest.raises(ProcessError, match="executable not found: node"):
            supervisor.start(spec, project_id=PROJECT)

    def test_each_start_gets_a_distinct_pid(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        first = supervisor.start(spec, project_id=PROJECT)
        second = supervisor.start(
            LaunchSpec(**{**_fields(spec), "name": "frontend"}), project_id=PROJECT
        )
        assert first.pid != second.pid


class TestStatus:
    def test_a_started_process_is_running(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        assert supervisor.status(record).state is ProcessState.RUNNING

    def test_a_process_that_dies_immediately_reports_its_code(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        supervisor.exit_immediately["backend"] = 1
        record = supervisor.start(spec, project_id=PROJECT)
        status = supervisor.status(record)
        assert status.state is ProcessState.EXITED
        assert status.exit_code == 1

    def test_an_exited_process_is_reported_as_exited(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.simulate_exit("backend", 3)
        status = supervisor.status(record)
        assert status.state is ProcessState.EXITED
        assert status.exit_code == 3


class TestStop:
    def test_stop_reports_a_graceful_shutdown(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        outcome = supervisor.stop(record)
        assert outcome.stopped
        assert outcome.was_running
        assert outcome.graceful

    def test_a_process_that_ignores_the_polite_request_is_terminated(
        self, spec: LaunchSpec
    ) -> None:
        supervisor = FakeSupervisor()
        supervisor.ignore_graceful_stop.add("backend")
        record = supervisor.start(spec, project_id=PROJECT)
        outcome = supervisor.stop(record)
        assert outcome.stopped
        assert not outcome.graceful
        assert "terminated the process tree" in outcome.detail

    def test_stopping_twice_is_not_an_error(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.stop(record)
        second = supervisor.stop(record)
        assert second.stopped
        assert not second.was_running
        assert second.detail == "already stopped"

    def test_stopping_an_already_dead_process_is_not_an_error(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.simulate_exit("backend")
        assert supervisor.stop(record).stopped


class TestPidReuse:
    """The rule that keeps Devbox from killing somebody else's program."""

    def test_a_recycled_pid_is_reported_not_signalled(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.simulate_pid_reuse("backend")
        assert supervisor.status(record).state is ProcessState.PID_REUSED

    def test_stopping_a_recycled_pid_is_refused(self, spec: LaunchSpec) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.simulate_pid_reuse("backend")
        outcome = supervisor.stop(record)
        assert outcome.refused
        assert not outcome.stopped
        assert "belongs to another program" in outcome.detail

    def test_a_record_without_a_creation_time_is_never_signalled(self, spec: LaunchSpec) -> None:
        """An old state file must not authorise a kill."""
        from dataclasses import replace

        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        unverifiable = replace(record, creation_time=None)
        outcome = supervisor.stop(unverifiable)
        assert outcome.refused
        assert "no creation time" in outcome.detail

    def test_a_crashed_devbox_leaves_a_stale_record_that_reads_as_exited(
        self, spec: LaunchSpec
    ) -> None:
        supervisor = FakeSupervisor()
        record = supervisor.start(spec, project_id=PROJECT)
        supervisor.simulate_crash("backend")
        assert supervisor.status(record).state is ProcessState.EXITED


def _fields(spec: LaunchSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "argv": spec.argv,
        "working_dir": spec.working_dir,
        "env": spec.env,
        "stdout_path": spec.stdout_path,
        "stderr_path": spec.stderr_path,
        "stop_timeout": spec.stop_timeout,
        "kill_on_supervisor_exit": spec.kill_on_supervisor_exit,
    }
