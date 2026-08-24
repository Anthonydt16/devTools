"""Windows process supervision: Job Objects, trees, PID identity.

This is the half of Devbox the Linux container cannot execute.  Everything here
runs for real on the ``windows-latest`` CI job.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from devbox.host import winapi
from devbox.host.procinfo import ProcessIdentity, process_creation_time, process_is_alive
from devbox.process.supervisor import LaunchSpec, ProcessState
from devbox.process.windows import WindowsSupervisor, job_object_name

pytestmark = pytest.mark.windows

PROJECT_ID = "windows-suite"


def wait_until(predicate: Callable[[], bool], *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestWin32Bindings:
    def test_process_creation_time_is_readable_for_our_own_process(self) -> None:
        """No elevation: PROCESS_QUERY_LIMITED_INFORMATION is enough."""
        import os

        created = process_creation_time(os.getpid())
        assert created is not None
        assert created > 0

    def test_our_own_process_is_alive(self) -> None:
        import os

        assert process_is_alive(os.getpid())

    def test_a_pid_that_cannot_exist_is_not_alive(self) -> None:
        assert not process_is_alive(0x7FFFFFF0)

    def test_a_job_object_can_be_created_and_closed(self) -> None:
        job = winapi.JobObject.create("devbox-test", kill_on_close=False)
        try:
            assert job.name == "devbox-test"
        finally:
            job.close()

    def test_a_named_job_can_be_reopened(self) -> None:
        """This is what lets a later `devbox down` stop a tree it did not start."""
        name = job_object_name("test-project", "svc")
        job = winapi.JobObject.create(name, kill_on_close=False, object_name=name)
        try:
            reopened = winapi.JobObject.open(name)
            assert reopened is not None
            reopened.close()
        finally:
            job.close()

    def test_reopening_a_job_that_does_not_exist_returns_none(self) -> None:
        assert winapi.JobObject.open("Local\\Devbox-does-not-exist-12345") is None

    def test_job_names_are_deterministic_and_legal(self) -> None:
        first = job_object_name("C:\\projects\\my app", "backend")
        second = job_object_name("C:\\projects\\my app", "backend")
        assert first == second
        assert first.startswith("Local\\Devbox-")
        assert "\\" not in first.removeprefix("Local\\")

    def test_job_names_sanitise_service_names(self) -> None:
        name = job_object_name("p", "back end/api")
        assert "/" not in name
        assert " " not in name.removeprefix("Local\\")


class TestProcessLifecycle:
    def test_a_process_starts_and_reports_running(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = windows_supervisor.start(
            launch("api", ["sleep", "--seconds", "30"]), project_id=PROJECT_ID
        )
        assert record.pid > 0
        assert record.creation_time is not None
        assert record.job_name.startswith("Local\\Devbox-")
        assert windows_supervisor.status(record).state is ProcessState.RUNNING

    def test_stop_ends_the_process(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = windows_supervisor.start(
            launch("api", ["sleep", "--seconds", "60"]), project_id=PROJECT_ID
        )
        outcome = windows_supervisor.stop(record, timeout=5)
        assert outcome.stopped
        assert windows_supervisor.status(record).state is ProcessState.EXITED

    def test_exit_codes_are_reported(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = windows_supervisor.start(
            launch("api", ["fail", "--code", "7", "--message", "boom"]), project_id=PROJECT_ID
        )
        status = windows_supervisor.wait(record, timeout=20)
        assert status.state is ProcessState.EXITED
        assert status.exit_code == 7

    def test_a_service_binds_its_port_and_releases_it_on_stop(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        port = free_port()
        record = windows_supervisor.start(
            launch("api", ["serve", "--port", str(port), "--max-seconds", "60"]),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_open(port))
        windows_supervisor.stop(record, timeout=10)
        assert wait_until(lambda: not port_open(port))


class TestJobObjectTree:
    """The reason Job Objects exist: Windows has no killpg."""

    @pytest.mark.slow
    def test_stopping_a_service_kills_its_grandchildren(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        parent_port = free_port()
        child_port = free_port()
        record = windows_supervisor.start(
            launch(
                "api",
                [
                    "spawn-child",
                    "--port",
                    str(parent_port),
                    "--child-port",
                    str(child_port),
                    "--max-seconds",
                    "120",
                ],
            ),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_open(parent_port)), "parent never started"
        assert wait_until(lambda: port_open(child_port)), "child never started"

        windows_supervisor.stop(record, timeout=10)

        assert wait_until(lambda: not port_open(parent_port)), "the parent survived"
        assert wait_until(lambda: not port_open(child_port), timeout=25), (
            "the grandchild survived: the Job Object did not take the tree down"
        )

    @pytest.mark.slow
    def test_a_later_invocation_can_stop_a_tree_it_did_not_start(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        """`devbox up` exits; `devbox down` is a different process.

        It reaches the tree by reopening the named Job Object recorded in state.
        """
        port = free_port()
        record = windows_supervisor.start(
            launch("api", ["serve", "--port", str(port), "--max-seconds", "120"]),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_open(port))

        fresh = WindowsSupervisor()
        try:
            outcome = fresh.stop(record, timeout=10)
            assert outcome.stopped
            assert wait_until(lambda: not port_open(port))
        finally:
            fresh.close()

    @pytest.mark.slow
    def test_descendant_fallback_never_targets_an_unrelated_process(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        parent_port = free_port()
        child_port = free_port()
        unrelated_port = free_port()
        tree = windows_supervisor.start(
            launch(
                "tree",
                [
                    "spawn-child",
                    "--port",
                    str(parent_port),
                    "--child-port",
                    str(child_port),
                    "--max-seconds",
                    "120",
                ],
            ),
            project_id=PROJECT_ID,
        )
        unrelated = windows_supervisor.start(
            launch(
                "unrelated",
                ["serve", "--port", str(unrelated_port), "--max-seconds", "120"],
            ),
            project_id=PROJECT_ID,
        )
        assert wait_until(lambda: port_open(parent_port))
        assert wait_until(lambda: port_open(child_port))
        assert wait_until(lambda: port_open(unrelated_port))
        assert tree.creation_time is not None

        text = Path(tree.stdout_log).read_text(encoding="utf-8")
        match = re.search(r"spawned child pid (\d+)", text)
        assert match is not None
        child_pid = int(match.group(1))
        captured = winapi.capture_descendant_processes(tree.pid, tree.creation_time)
        try:
            captured_pids = {process.pid for process in captured}
            assert child_pid in captured_pids
            assert unrelated.pid not in captured_pids
            for process in captured:
                process.terminate()
        finally:
            for process in captured:
                process.close()

        assert wait_until(lambda: not port_open(child_port))
        assert process_is_alive(unrelated.pid), "an unrelated process was targeted"


class TestPidReuse:
    """Non-regression #7 on the platform where it matters."""

    def test_a_record_whose_creation_time_does_not_match_is_refused(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        from dataclasses import replace

        record = windows_supervisor.start(
            launch("api", ["sleep", "--seconds", "30"]), project_id=PROJECT_ID
        )
        forged = replace(
            record,
            service_id="somebody-else",
            creation_time=(record.creation_time or 0.0) + 5000.0,
        )

        outcome = windows_supervisor.stop(forged, timeout=5)

        assert outcome.refused, "Devbox signalled a process it could not prove it owned"
        assert windows_supervisor.status(record).state is ProcessState.RUNNING

    def test_identity_matches_a_live_process(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = windows_supervisor.start(
            launch("api", ["sleep", "--seconds", "30"]), project_id=PROJECT_ID
        )
        assert ProcessIdentity(record.pid, record.creation_time).matches_live_process()

    def test_identity_does_not_match_after_the_process_exits(
        self, windows_supervisor: WindowsSupervisor, launch: Callable[..., LaunchSpec]
    ) -> None:
        record = windows_supervisor.start(launch("api", ["crash"]), project_id=PROJECT_ID)
        windows_supervisor.wait(record, timeout=20)
        assert not ProcessIdentity(record.pid, record.creation_time).matches_live_process()


class TestWindowsQuoting:
    """Non-regression #8, on the platform whose quoting rules are the problem."""

    def test_arguments_with_spaces_and_metacharacters_survive(
        self,
        windows_supervisor: WindowsSupervisor,
        launch: Callable[..., LaunchSpec],
        tmp_path: Path,
    ) -> None:
        from devbox.logs.reader import LogReader, LogStream

        dangerous = [
            "hello world",
            "&& del /q C:\\",
            "| more",
            '"quoted"',
            "C:\\Program Files\\My App\\bin",
            "%PATH%",
            "^caret",
            "caf\u00e9",
        ]
        spec = launch("echo", ["echo-args", *dangerous])
        record = windows_supervisor.start(spec, project_id=PROJECT_ID)
        windows_supervisor.wait(record, timeout=20)

        reader = LogReader("echo", spec.stdout_path, spec.stderr_path)
        printed = [line.text for line in reader.read(streams=[LogStream.STDOUT])]
        assert printed == dangerous

    def test_a_working_directory_with_spaces_works(
        self,
        windows_supervisor: WindowsSupervisor,
        launch: Callable[..., LaunchSpec],
        tmp_path: Path,
    ) -> None:
        working = tmp_path / "My Projects" / "customer portal"
        working.mkdir(parents=True)
        record = windows_supervisor.start(
            launch("api", ["echo-args", "ok"], working_dir=working), project_id=PROJECT_ID
        )
        assert windows_supervisor.wait(record, timeout=20).exit_code == 0
