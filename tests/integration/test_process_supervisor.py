"""Real process supervision, against real child processes.

Everything here spawns actual OS processes using the fake runtime, so it covers
the code paths ``devbox up`` and ``devbox down`` take — log capture, exit codes,
graceful shutdown, process trees — on a machine with no Node installed.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from devbox.errors import ProcessError
from devbox.logs import LogReader, LogStream
from devbox.process import LaunchSpec, ProcessState, ProcessSupervisor
from devbox.process.supervisor import ProcessRecord

pytestmark = [pytest.mark.integration]

PROJECT_ID = "integration-project"


def wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def port_is_open(port: int, *, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def http_ok(port: int, path: str = "/health") -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
            return bool(200 <= response.status < 400)
    except (urllib.error.URLError, OSError):
        return False


class TestLifecycle:
    def test_a_service_starts_and_is_reported_running(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = supervisor.start(
            launch("api", ["sleep", "--seconds", "30"]), project_id=PROJECT_ID
        )
        assert record.pid > 0
        assert record.creation_time is not None, "ownership must be provable"
        assert supervisor.status(record).state is ProcessState.RUNNING

    def test_a_service_that_exits_is_reported_with_its_code(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = supervisor.start(
            launch("api", ["fail", "--code", "7", "--message", "db unreachable"]),
            project_id=PROJECT_ID,
        )
        status = supervisor.wait(record, timeout=10)
        assert status.state is ProcessState.EXITED
        assert status.exit_code == 7

    def test_stop_ends_the_process(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = supervisor.start(
            launch("api", ["sleep", "--seconds", "60"]), project_id=PROJECT_ID
        )
        outcome = supervisor.stop(record, timeout=5)
        assert outcome.stopped
        assert outcome.was_running
        assert supervisor.status(record).state is ProcessState.EXITED

    def test_stopping_an_already_stopped_service_is_not_an_error(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = supervisor.start(
            launch("api", ["sleep", "--seconds", "60"]), project_id=PROJECT_ID
        )
        supervisor.stop(record, timeout=5)
        second = supervisor.stop(record, timeout=5)
        assert second.stopped
        assert not second.was_running

    def test_restart_produces_a_new_pid(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        spec = launch("api", ["sleep", "--seconds", "60"])
        first = supervisor.start(spec, project_id=PROJECT_ID)
        supervisor.stop(first, timeout=5)
        second = supervisor.start(spec, project_id=PROJECT_ID)
        assert second.pid != first.pid
        supervisor.stop(second, timeout=5)

    def test_a_process_that_dies_immediately_is_not_a_start_error(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        """Starting succeeded; the caller decides what a fast exit means."""
        record = supervisor.start(launch("one-shot", ["crash"]), project_id=PROJECT_ID)
        assert supervisor.wait(record, timeout=10).state is ProcessState.EXITED

    def test_a_missing_executable_is_an_actionable_error(
        self, supervisor: ProcessSupervisor, tmp_path: Path
    ) -> None:
        spec = LaunchSpec(
            name="ghost",
            argv=(str(tmp_path / "not-installed"),),
            working_dir=tmp_path,
            env={},
            stdout_path=tmp_path / "o.log",
            stderr_path=tmp_path / "e.log",
        )
        with pytest.raises(ProcessError) as caught:
            supervisor.start(spec, project_id=PROJECT_ID)
        assert "executable not found" in str(caught.value)
        assert caught.value.hint is not None

    def test_a_missing_working_directory_is_an_actionable_error(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec], tmp_path: Path
    ) -> None:
        spec = launch("api", ["sleep"], working_dir=tmp_path / "does-not-exist")
        with pytest.raises(ProcessError) as caught:
            supervisor.start(spec, project_id=PROJECT_ID)
        assert "working directory does not exist" in str(caught.value)


class TestPorts:
    def test_a_service_binds_its_port_and_answers_a_healthcheck(
        self,
        supervisor: ProcessSupervisor,
        launch: Callable[..., LaunchSpec],
        free_port: Callable[[], int],
    ) -> None:
        port = free_port()
        record = supervisor.start(
            launch("api", ["serve", "--port", str(port), "--max-seconds", "30"]),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_is_open(port)), "the service never bound its port"
        assert http_ok(port)
        supervisor.stop(record, timeout=5)
        assert wait_until(lambda: not port_is_open(port)), "the port was not released"


class TestLogs:
    def test_stdout_and_stderr_are_captured_separately(
        self,
        supervisor: ProcessSupervisor,
        launch: Callable[..., LaunchSpec],
        free_port: Callable[[], int],
    ) -> None:
        port = free_port()
        spec = launch("api", ["serve", "--port", str(port), "--max-seconds", "20"])
        record = supervisor.start(spec, project_id=PROJECT_ID)
        assert wait_until(lambda: port_is_open(port))
        supervisor.stop(record, timeout=5)

        reader = LogReader("api", spec.stdout_path, spec.stderr_path)
        out = [line.text for line in reader.read(streams=[LogStream.STDOUT])]
        err = [line.text for line in reader.read(streams=[LogStream.STDERR])]
        assert any("listening on" in line for line in out)
        assert any("startup diagnostics on stderr" in line for line in err)
        assert not any("startup diagnostics" in line for line in out)

    def test_logs_survive_the_process(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        spec = launch("api", ["fail", "--code", "3", "--message", "connection refused"])
        record = supervisor.start(spec, project_id=PROJECT_ID)
        supervisor.wait(record, timeout=10)
        reader = LogReader("api", spec.stdout_path, spec.stderr_path)
        assert "connection refused" in reader.tail_text(LogStream.STDERR, lines=5)

    def test_a_restart_appends_rather_than_erasing_history(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        spec = launch("api", ["fail", "--code", "1", "--message", "first run"])
        supervisor.wait(supervisor.start(spec, project_id=PROJECT_ID), timeout=10)
        supervisor.wait(supervisor.start(spec, project_id=PROJECT_ID), timeout=10)
        reader = LogReader("api", spec.stdout_path, spec.stderr_path)
        assert reader.tail_text(LogStream.STDERR, lines=10).count("first run") == 2


class TestProcessTree:
    """``devbox down`` must take grandchildren with it."""

    @pytest.mark.slow
    def test_stopping_a_service_kills_its_children(
        self,
        supervisor: ProcessSupervisor,
        launch: Callable[..., LaunchSpec],
        free_port: Callable[[], int],
    ) -> None:
        parent_port = free_port()
        child_port = free_port()
        record = supervisor.start(
            launch(
                "api",
                [
                    "spawn-child",
                    "--port",
                    str(parent_port),
                    "--child-port",
                    str(child_port),
                    "--max-seconds",
                    "60",
                ],
            ),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_is_open(parent_port)), "parent never started"
        assert wait_until(lambda: port_is_open(child_port)), "child never started"

        supervisor.stop(record, timeout=5)

        assert wait_until(lambda: not port_is_open(parent_port)), "parent survived the stop"
        assert wait_until(lambda: not port_is_open(child_port), timeout=15), (
            "the grandchild survived: the process tree was not stopped"
        )


class TestOwnership:
    def test_a_record_with_a_wrong_creation_time_is_never_signalled(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        """Non-regression #7: a recycled PID must not be killed."""
        from dataclasses import replace

        record = supervisor.start(
            launch("api", ["sleep", "--seconds", "30"]), project_id=PROJECT_ID
        )
        forged = replace(
            record, creation_time=(record.creation_time or 0.0) + 5000.0, service_id="other"
        )

        outcome = supervisor.stop(forged, timeout=2)

        assert outcome.refused, "Devbox signalled a process it could not prove it owned"
        assert not outcome.stopped
        assert supervisor.status(record).state is ProcessState.RUNNING, (
            "the real process must be untouched"
        )
        supervisor.stop(record, timeout=5)

    def test_a_stale_record_for_a_dead_pid_reads_as_exited(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec], tmp_path: Path
    ) -> None:
        """What Devbox finds after it was killed and the service already died."""
        record = supervisor.start(launch("api", ["crash"]), project_id=PROJECT_ID)
        supervisor.wait(record, timeout=10)
        fresh = ProcessRecord(
            project_id=PROJECT_ID,
            service_id="api",
            pid=record.pid,
            command=record.command,
            working_dir=record.working_dir,
            started_at=record.started_at,
            creation_time=record.creation_time,
        )
        # A brand-new supervisor, as a later `devbox ps` invocation would have.
        from devbox.process.factory import create_supervisor

        assert create_supervisor().status(fresh).state in (
            ProcessState.EXITED,
            ProcessState.PID_REUSED,
        )


class TestArgumentIntegrity:
    """Non-regression #8: no shell, so nothing is re-interpreted."""

    def test_special_characters_reach_the_child_unaltered(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec], tmp_path: Path
    ) -> None:
        dangerous = [
            "hello world",
            "&& rm -rf /",
            "; shutdown /s",
            '"quoted"',
            "C:\\Program Files\\My App\\bin",
            "$(whoami)",
            "`id`",
            "%PATH%",
            "caf\u00e9 \u00e9\u00e0\u00fc",
        ]
        spec = launch("echo", ["echo-args", *dangerous])
        record = supervisor.start(spec, project_id=PROJECT_ID)
        supervisor.wait(record, timeout=10)

        reader = LogReader("echo", spec.stdout_path, spec.stderr_path)
        printed = [line.text for line in reader.read(streams=[LogStream.STDOUT])]
        assert printed == dangerous

    def test_a_working_directory_with_spaces_works(
        self, supervisor: ProcessSupervisor, launch: Callable[..., LaunchSpec], tmp_path: Path
    ) -> None:
        working = tmp_path / "My Projects" / "customer portal"
        working.mkdir(parents=True)
        spec = launch("api", ["echo-args", "ok"], working_dir=working)
        record = supervisor.start(spec, project_id=PROJECT_ID)
        assert supervisor.wait(record, timeout=10).exit_code == 0
