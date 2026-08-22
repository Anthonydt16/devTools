"""Running short-lived commands: output capture, exit codes, timeouts, errors."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests.conftest import minimal_child_environment

from devbox.errors import ProcessError
from devbox.process.runner import CommandResult, RecordingRunner, SubprocessRunner

pytestmark = pytest.mark.unit


class TestSubprocessRunner:
    @pytest.mark.integration
    def test_output_is_captured_and_separated(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
        )
        assert result.ok
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"

    @pytest.mark.integration
    def test_a_non_zero_exit_is_reported_not_raised(self) -> None:
        result = SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(3)"])
        assert not result.ok
        assert result.returncode == 3

    @pytest.mark.integration
    def test_raise_for_status_carries_the_output_that_explains_it(self) -> None:
        result = SubprocessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('bad flag\\n'); raise SystemExit(2)",
            ]
        )
        with pytest.raises(ProcessError) as caught:
            result.raise_for_status(what="initdb for company-postgres")
        message = str(caught.value)
        assert "initdb for company-postgres failed with exit code 2" in message
        assert "bad flag" in message

    @pytest.mark.integration
    def test_a_timeout_is_reported_as_such(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.4
        )
        assert result.timed_out
        assert not result.ok
        with pytest.raises(ProcessError, match="timed out"):
            result.raise_for_status(what="a slow command")

    @pytest.mark.integration
    def test_stdin_can_be_supplied(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            stdin="hello",
        )
        assert result.stdout.strip() == "HELLO"

    @pytest.mark.integration
    def test_the_working_directory_is_honoured(self, tmp_path: Path) -> None:
        directory = tmp_path / "My Work"
        directory.mkdir()
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import os; print(os.getcwd())"], cwd=directory
        )
        assert result.stdout.strip() == str(directory)

    @pytest.mark.integration
    def test_the_environment_is_replaced_not_merged(self) -> None:
        result = SubprocessRunner().run(
            [sys.executable, "-c", "import os; print(os.environ.get('DEVBOX_MARKER', 'absent'))"],
            env=minimal_child_environment(DEVBOX_MARKER="present"),
        )
        assert result.stdout.strip() == "present"

    def test_a_missing_executable_is_an_actionable_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProcessError) as caught:
            SubprocessRunner().run([str(tmp_path / "definitely-not-here")])
        assert "executable not found" in str(caught.value)
        assert caught.value.hint is not None

    def test_an_empty_command_is_refused(self) -> None:
        with pytest.raises(ProcessError, match="refusing to run an empty command"):
            SubprocessRunner().run([])

    def test_a_successful_result_raises_nothing(self) -> None:
        result = CommandResult(argv=("true",), returncode=0)
        assert result.raise_for_status(what="anything") is result


class TestRecordingRunner:
    def test_calls_are_recorded_with_their_context(self, tmp_path: Path) -> None:
        runner = RecordingRunner()
        runner.run(["createdb", "clients"], cwd=tmp_path, env={"PGPASSWORD": "x"})
        assert runner.calls[0].argv == ("createdb", "clients")
        assert runner.calls[0].cwd == str(tmp_path)
        assert runner.calls[0].env == {"PGPASSWORD": "x"}

    def test_scripted_answers_match_on_a_fragment(self) -> None:
        runner = RecordingRunner()
        runner.script("SHOW DATABASES", CommandResult(argv=(), returncode=0, stdout="a\nb\n"))
        result = runner.run(["mysql", "--execute", "SHOW DATABASES"])
        assert result.stdout == "a\nb\n"

    def test_an_unscripted_call_succeeds_by_default(self) -> None:
        assert RecordingRunner().run(["anything"]).ok

    def test_a_default_result_applies_to_everything_unscripted(self) -> None:
        runner = RecordingRunner(default=CommandResult(argv=(), returncode=1, stderr="nope"))
        result = runner.run(["anything"])
        assert result.returncode == 1
        assert result.stderr == "nope"

    def test_commands_can_be_asserted_on(self) -> None:
        runner = RecordingRunner()
        runner.run(["psql", "--command", "SELECT 1"])
        assert runner.ran("psql")
        assert not runner.ran("mysql")
        assert runner.commands() == ["psql --command 'SELECT 1'"]
