"""The pieces `devbox test` is built from.

Parsing pytest's own output is the part most likely to rot, so it is pinned
against real output shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.process.runner import CommandResult, RecordingRunner
from devbox.validation.checks import (
    CheckOutcome,
    CommandCheck,
    PytestCheck,
    parse_pytest_summary,
    python_module_check,
)
from devbox.validation.suite import ConfigurationCheck, PackagingCheck, ValidationSuite

pytestmark = pytest.mark.unit


class TestPytestOutputParsing:
    def test_a_passing_run(self) -> None:
        counts, reasons = parse_pytest_summary("....\n4 passed in 0.12s\n")
        assert counts == {"passed": 4}
        assert reasons == ()

    def test_a_mixed_run(self) -> None:
        counts, _ = parse_pytest_summary("2 failed, 10 passed, 3 skipped in 1.20s\n")
        assert counts == {"failed": 2, "passed": 10, "skipped": 3}

    def test_errors_are_counted(self) -> None:
        counts, _ = parse_pytest_summary("1 failed, 2 errors in 0.5s\n")
        assert counts["error"] == 2

    def test_skip_reasons_are_extracted_with_their_counts(self) -> None:
        output = (
            "sssss\n"
            "=========================== short test summary info ====================\n"
            "SKIPPED [14] tests/conftest.py:41: requires a real Windows host (running on linux)\n"
            "SKIPPED [2] tests/other.py:7: needs a database\n"
            "16 skipped in 0.08s\n"
        )
        counts, reasons = parse_pytest_summary(output)
        assert counts == {"skipped": 16}
        assert reasons == (
            (14, "requires a real Windows host (running on linux)"),
            (2, "needs a database"),
        )

    def test_empty_output_yields_nothing(self) -> None:
        assert parse_pytest_summary("") == ({}, ())

    def test_the_last_summary_line_wins(self) -> None:
        output = "1 passed in 0.1s\n\n=== rerun ===\n3 passed in 0.2s\n"
        counts, _ = parse_pytest_summary(output)
        assert counts == {"passed": 3}


class TestCommandCheck:
    def test_a_successful_command_passes(self, tmp_path: Path) -> None:
        runner = RecordingRunner(default=CommandResult(argv=(), returncode=0, stdout="fine"))
        check = CommandCheck(name="lint", description="d", argv=("ruff", "check"), requires="")
        result = check.run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.PASSED
        assert result.summary == "fine"

    def test_a_failing_command_carries_its_output(self, tmp_path: Path) -> None:
        runner = RecordingRunner(
            default=CommandResult(argv=(), returncode=1, stderr="E501 line too long")
        )
        check = CommandCheck(name="lint", description="d", argv=("ruff",), requires="")
        result = check.run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.FAILED
        assert "E501" in result.detail

    def test_a_missing_tool_is_a_skip_with_a_reason(self, tmp_path: Path) -> None:
        check = CommandCheck(
            name="types", description="d", argv=("x",), requires="a_module_that_does_not_exist"
        )
        result = check.run(RecordingRunner(), cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.SKIPPED
        assert "not installed" in result.skip_reasons[0][1]

    def test_a_module_check_uses_this_interpreter(self) -> None:
        import sys

        check = python_module_check("lint", "d", "ruff", ("check",))
        assert check.argv[:3] == (sys.executable, "-m", "ruff")


class TestPytestCheck:
    def test_the_marker_becomes_a_selection(self) -> None:
        argv = PytestCheck(name="unit", description="d", marker="unit").argv()
        assert "-m" in argv
        assert "unit" in argv
        assert "-rs" in argv, "skip reasons must be requested"

    def test_no_tests_matched_can_be_a_skip(self, tmp_path: Path) -> None:
        runner = RecordingRunner(default=CommandResult(argv=(), returncode=5))
        check = PytestCheck(name="windows", description="d", marker="windows", skip_when_empty=True)
        result = check.run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.SKIPPED

    def test_no_tests_matched_is_otherwise_a_failure(self, tmp_path: Path) -> None:
        runner = RecordingRunner(default=CommandResult(argv=(), returncode=5))
        check = PytestCheck(name="unit", description="d", marker="unit")
        result = check.run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.FAILED

    def test_counts_are_reported(self, tmp_path: Path) -> None:
        runner = RecordingRunner(
            default=CommandResult(argv=(), returncode=0, stdout="10 passed, 2 skipped in 1s")
        )
        check = PytestCheck(name="unit", description="d", marker="unit")
        result = check.run(runner, cwd=tmp_path, env={})
        assert result.counts == {"passed": 10, "skipped": 2}
        assert "10 passed" in result.summary


class TestConfigurationCheck:
    def test_a_valid_configuration_passes(self, tmp_path: Path) -> None:
        (tmp_path / "devbox.yml").write_text("version: 1\n", encoding="utf-8")
        result = ConfigurationCheck(name="config", description="d").run(
            RecordingRunner(), cwd=tmp_path, env={}
        )
        assert result.outcome is CheckOutcome.PASSED

    def test_an_invalid_configuration_fails_with_its_message(self, tmp_path: Path) -> None:
        (tmp_path / "devbox.yml").write_text(
            "version: 1\nservices:\n  a:\n    command: 3\n", encoding="utf-8"
        )
        result = ConfigurationCheck(name="config", description="d").run(
            RecordingRunner(), cwd=tmp_path, env={}
        )
        assert result.outcome is CheckOutcome.FAILED
        assert "expected a string" in result.detail

    def test_no_configuration_is_a_skip(self, tmp_path: Path) -> None:
        result = ConfigurationCheck(name="config", description="d").run(
            RecordingRunner(), cwd=tmp_path, env={}
        )
        assert result.outcome is CheckOutcome.SKIPPED

    def test_examples_are_included(self, tmp_path: Path) -> None:
        example = tmp_path / "examples" / "demo"
        example.mkdir(parents=True)
        (example / "devbox.yml").write_text("version: 1\n", encoding="utf-8")
        result = ConfigurationCheck(name="config", description="d").run(
            RecordingRunner(), cwd=tmp_path, env={}
        )
        assert result.outcome is CheckOutcome.PASSED
        assert "1 configuration file(s)" in result.summary


class TestPackagingCheck:
    def test_it_passes_when_the_entry_point_answers(self, tmp_path: Path) -> None:
        runner = RecordingRunner(
            default=CommandResult(argv=(), returncode=0, stdout="Devbox v0.1.0\n")
        )
        result = PackagingCheck(name="packaging", description="d").run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.PASSED

    def test_it_fails_when_the_output_is_wrong(self, tmp_path: Path) -> None:
        runner = RecordingRunner(default=CommandResult(argv=(), returncode=0, stdout="???"))
        result = PackagingCheck(name="packaging", description="d").run(runner, cwd=tmp_path, env={})
        assert result.outcome is CheckOutcome.FAILED


class TestSuite:
    def test_only_filters_by_name_and_by_group(self, tmp_path: Path) -> None:
        checks = [
            python_module_check("lint", "d", "ruff", ("--version",), group="static"),
            python_module_check("types", "d", "mypy", ("--version",), group="static"),
            PytestCheck(name="unit", description="d", group="tests", marker="unit"),
        ]
        suite = ValidationSuite(root=tmp_path, checks=checks, runner=RecordingRunner())
        assert len(suite.run(only=["static"]).results) == 2
        assert len(suite.run(only=["unit"]).results) == 1

    def test_results_are_reported_as_they_finish(self, tmp_path: Path) -> None:
        seen: list[str] = []
        suite = ValidationSuite(
            root=tmp_path,
            checks=[python_module_check("lint", "d", "ruff", ("--version",))],
            runner=RecordingRunner(),
        )
        suite.run(on_result=lambda result: seen.append(result.name))
        assert seen == ["lint"]

    def test_the_child_environment_is_isolated_and_points_at_src(self, tmp_path: Path) -> None:
        suite = ValidationSuite(root=tmp_path, checks=[], environ={"PATH": "/usr/bin"})
        env = suite.child_environment()
        assert str(tmp_path / "src") in env["PYTHONPATH"]
        assert env["NO_COLOR"] == "1"

    def test_an_existing_pythonpath_is_preserved(self, tmp_path: Path) -> None:
        suite = ValidationSuite(root=tmp_path, checks=[], environ={"PYTHONPATH": "/existing"})
        assert "/existing" in suite.child_environment()["PYTHONPATH"]

    def test_skip_lines_aggregate_across_checks(self, tmp_path: Path) -> None:
        runner = RecordingRunner(
            default=CommandResult(
                argv=(),
                returncode=0,
                stdout="SKIPPED [3] a.py:1: needs Windows\n3 skipped in 0.1s\n",
            )
        )
        suite = ValidationSuite(
            root=tmp_path,
            checks=[
                PytestCheck(name="a", description="d", marker="a"),
                PytestCheck(name="b", description="d", marker="b"),
            ],
            runner=runner,
        )
        report = suite.run()
        assert report.skip_lines() == ("6 test(s) skipped: needs Windows",)
        assert report.total("skipped") == 6
