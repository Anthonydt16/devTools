"""The individual checks ``devbox test`` runs.

Each check is a small object that knows how to run itself and how to report what
happened — including *why* it was skipped, which is the part most test runners
lose.  "14 Windows tests skipped: not running on Windows" is information; a
silent green is not.

Checks are data-driven so the suite can be filtered (``--only lint``), so a check
that is unavailable degrades to a skip rather than a failure, and so the whole
suite is testable without running itself recursively.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from devbox.process.runner import CommandResult, CommandRunner, SubprocessRunner

__all__ = [
    "Check",
    "CheckOutcome",
    "CheckResult",
    "CommandCheck",
    "PytestCheck",
    "parse_pytest_summary",
]


class CheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_failure(self) -> bool:
        return self is CheckOutcome.FAILED


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one check did."""

    name: str
    outcome: CheckOutcome
    duration: float = 0.0
    summary: str = ""
    detail: str = ""
    mandatory: bool = True
    skip_reasons: tuple[tuple[int, str], ...] = ()
    """``(count, reason)`` pairs, so the report can say what was skipped and why."""

    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.outcome.is_failure

    @property
    def blocks_success(self) -> bool:
        """A non-mandatory check that fails is reported but does not fail the run."""
        return self.failed and self.mandatory


@dataclass(frozen=True, slots=True)
class Check:
    """A named unit of validation."""

    name: str
    description: str
    mandatory: bool = True
    group: str = "general"

    def run(self, runner: CommandRunner, *, cwd: Path, env: Mapping[str, str]) -> CheckResult:
        raise NotImplementedError  # pragma: no cover - abstract


@dataclass(frozen=True, slots=True)
class CommandCheck(Check):
    """A check that runs a command and passes when it exits zero."""

    argv: tuple[str, ...] = ()
    requires: str = ""
    """A module that must be importable, or the check is skipped with a reason."""

    timeout: float = 900.0
    summarise: Callable[[CommandResult], str] | None = None

    def run(self, runner: CommandRunner, *, cwd: Path, env: Mapping[str, str]) -> CheckResult:
        if self.requires and not _module_available(self.requires):
            return CheckResult(
                name=self.name,
                outcome=CheckOutcome.SKIPPED,
                mandatory=self.mandatory,
                summary=f"{self.requires} is not installed",
                skip_reasons=((1, f"{self.requires} is not installed"),),
            )
        started = time.monotonic()
        result = runner.run(self.argv, cwd=cwd, env=env, timeout=self.timeout)
        duration = time.monotonic() - started
        summary = self.summarise(result) if self.summarise else _first_useful_line(result)
        return CheckResult(
            name=self.name,
            outcome=CheckOutcome.PASSED if result.ok else CheckOutcome.FAILED,
            duration=duration,
            summary=summary,
            detail="" if result.ok else _tail(result),
            mandatory=self.mandatory,
        )


_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed|deselected)")
_SKIP_RE = re.compile(r"^SKIPPED\s+\[(\d+)\]\s+(.*)$", re.MULTILINE)


def parse_pytest_summary(output: str) -> tuple[dict[str, int], tuple[tuple[int, str], ...]]:
    """Extract counts and skip reasons from pytest's own output.

    Parsing text rather than adding a JSON-report plugin keeps ``devbox test``
    dependent on nothing but pytest itself, which is what makes it runnable in a
    fresh clone with ``pip install -e ".[dev]"`` and no extra step.
    """
    counts: dict[str, int] = {}
    for line in reversed(output.splitlines()):
        matches = _SUMMARY_RE.findall(line)
        if matches and (
            "passed" in line or "failed" in line or "error" in line or "skipped" in line
        ):
            for number, word in matches:
                key = "error" if word.startswith("error") else word
                counts[key] = counts.get(key, 0) + int(number)
            break

    reasons: list[tuple[int, str]] = []
    for count, raw in _SKIP_RE.findall(output):
        reason = raw.split(": ", 1)[-1].strip().strip("'\"")
        reasons.append((int(count), reason))
    return counts, tuple(reasons)


@dataclass(frozen=True, slots=True)
class PytestCheck(Check):
    """A check that runs part of the pytest suite and reports its counts."""

    marker: str = ""
    paths: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    timeout: float = 1800.0
    skip_when_empty: bool = False
    """Report "no tests matched" as a skip rather than a failure — used for the
    Windows selection, which legitimately matches nothing on Linux."""

    def argv(self) -> tuple[str, ...]:
        arguments: list[str] = [sys.executable, "-m", "pytest", "-q", "-rs", "--color=no"]
        if self.marker:
            arguments += ["-m", self.marker]
        arguments += list(self.paths)
        arguments += list(self.extra_args)
        return tuple(arguments)

    def run(self, runner: CommandRunner, *, cwd: Path, env: Mapping[str, str]) -> CheckResult:
        if not _module_available("pytest"):
            return CheckResult(
                name=self.name,
                outcome=CheckOutcome.SKIPPED,
                mandatory=self.mandatory,
                summary="pytest is not installed",
                skip_reasons=(
                    (1, "pytest is not installed; install with pip install -e '.[dev]'"),
                ),
            )
        started = time.monotonic()
        result = runner.run(self.argv(), cwd=cwd, env=env, timeout=self.timeout)
        duration = time.monotonic() - started
        output = result.stdout + "\n" + result.stderr
        counts, reasons = parse_pytest_summary(output)

        # pytest exits 5 when no test matched the selection.
        if result.returncode == 5:
            outcome = CheckOutcome.SKIPPED if self.skip_when_empty else CheckOutcome.FAILED
            return CheckResult(
                name=self.name,
                outcome=outcome,
                duration=duration,
                mandatory=self.mandatory,
                summary="no tests matched this selection",
                skip_reasons=((0, f"no tests matched -m {self.marker!r}"),),
                counts=counts,
            )

        return CheckResult(
            name=self.name,
            outcome=CheckOutcome.PASSED if result.ok else CheckOutcome.FAILED,
            duration=duration,
            summary=_render_counts(counts),
            detail="" if result.ok else _tail(result, lines=40),
            mandatory=self.mandatory,
            skip_reasons=reasons,
            counts=counts,
        )


def _render_counts(counts: Mapping[str, int]) -> str:
    order = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
    parts = [f"{counts[key]} {key}" for key in order if counts.get(key)]
    return ", ".join(parts) or "no tests"


def _module_available(module: str) -> bool:
    import importlib.util

    if shutil.which(module) is not None:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _first_useful_line(result: CommandResult) -> str:
    for line in (result.stdout + result.stderr).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return "ok"


def _tail(result: CommandResult, *, lines: int = 25) -> str:
    text = (
        result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr
    ).strip()
    if not text:
        return f"exit code {result.returncode}, no output"
    collected = text.splitlines()
    return "\n".join(collected[-lines:])


def default_runner() -> CommandRunner:
    return SubprocessRunner()


def python_module_check(
    name: str,
    description: str,
    module: str,
    arguments: Sequence[str],
    *,
    mandatory: bool = True,
    group: str = "general",
    requires: str = "",
) -> CommandCheck:
    """Build a check that runs ``python -m <module> ...`` with this interpreter.

    Using ``sys.executable`` rather than a bare ``ruff``/``mypy`` on PATH means
    ``devbox test`` uses the tools from the *active virtualenv*, which is what a
    developer expects and what makes CI reproducible.
    """
    return CommandCheck(
        name=name,
        description=description,
        mandatory=mandatory,
        group=group,
        argv=(sys.executable, "-m", module, *arguments),
        requires=requires or module,
    )
