"""``devbox test`` — the single command that validates the project.

What it runs, in this order, so that a fast failure comes first:

1. lint (``ruff check``) and formatting;
2. types (``mypy src``);
3. unit tests;
4. integration tests;
5. Windows tests — really run on Windows, explicitly skipped elsewhere *with the
   count and the reason*;
6. configuration checks: every example and fixture ``devbox.yml`` must parse;
7. packaging checks: the console entry point imports and answers ``--version``.

Design points:

* **The exit code is the contract.**  Non-zero when any mandatory check fails,
  zero otherwise.  A CI job needs nothing but ``devbox test``.
* **Skips are reported, never hidden.**  The summary says "14 skipped: requires a
  real Windows host", because a suite that silently skips the interesting half is
  worse than one that fails.
* **No interaction, no cleanup debt.**  Temporary files created by the suite are
  removed; the tests themselves are responsible for their own children and the
  conftest fails them if they leak.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devbox.host.platform_info import current_platform, is_windows
from devbox.process.runner import CommandRunner, SubprocessRunner
from devbox.validation.checks import (
    Check,
    CheckOutcome,
    CheckResult,
    PytestCheck,
    python_module_check,
)

__all__ = ["ResultSink", "ValidationReport", "ValidationSuite", "default_checks"]

ResultSink = Callable[[CheckResult], None]
"""Called as each check finishes, so the CLI can print progress live."""


def default_checks(*, include_slow: bool = True) -> tuple[Check, ...]:
    """The checks a full run performs."""
    integration_args: tuple[str, ...] = () if include_slow else ("-m", "integration and not slow")
    return (
        python_module_check(
            "lint",
            "ruff check .",
            "ruff",
            ("check", ".", "--output-format", "concise"),
            group="static",
        ),
        python_module_check(
            "format",
            "ruff format --check .",
            "ruff",
            ("format", "--check", "."),
            group="static",
        ),
        python_module_check(
            "types",
            "mypy src",
            "mypy",
            ("src",),
            group="static",
        ),
        PytestCheck(
            name="unit",
            description="hermetic tests: no child process, no network",
            group="tests",
            marker="unit",
        ),
        PytestCheck(
            name="integration",
            description="tests that spawn real processes against fake runtimes",
            group="tests",
            marker="integration" if include_slow else "",
            extra_args=integration_args if not include_slow else (),
        ),
        PytestCheck(
            name="windows",
            description="tests that need a real Windows host",
            group="tests",
            marker="windows",
            mandatory=is_windows(),
            skip_when_empty=True,
        ),
        ConfigurationCheck(
            name="config",
            description="every shipped devbox.yml parses and validates",
            group="project",
        ),
        PackagingCheck(
            name="packaging",
            description="the console entry point imports and answers --version",
            group="project",
        ),
    )


@dataclass(frozen=True, slots=True)
class ConfigurationCheck(Check):
    """Parse every ``devbox.yml`` shipped in the repository.

    A broken example is a broken promise: the README tells people to copy it.
    """

    patterns: tuple[str, ...] = ("examples/**/devbox.yml", "docs/**/devbox.yml", "devbox.yml")

    def run(self, runner: CommandRunner, *, cwd: Path, env: Mapping[str, str]) -> CheckResult:
        del runner, env
        from devbox.config.loader import load_config
        from devbox.errors import ConfigError

        started = time.monotonic()
        found: list[Path] = []
        for pattern in self.patterns:
            found.extend(sorted(cwd.glob(pattern)))
        if not found:
            return CheckResult(
                name=self.name,
                outcome=CheckOutcome.SKIPPED,
                mandatory=self.mandatory,
                summary="no devbox.yml found in this tree",
                skip_reasons=((0, "no configuration files to check"),),
            )

        problems: list[str] = []
        for path in found:
            try:
                load_config(path)
            except ConfigError as error:
                problems.append(f"{path}:\n{error}")

        return CheckResult(
            name=self.name,
            outcome=CheckOutcome.FAILED if problems else CheckOutcome.PASSED,
            duration=time.monotonic() - started,
            summary=f"{len(found)} configuration file(s) checked",
            detail="\n\n".join(problems),
            mandatory=self.mandatory,
        )


@dataclass(frozen=True, slots=True)
class PackagingCheck(Check):
    """Prove the packaged entry point actually works.

    ``python -m devbox --version`` exercises exactly the import path PyInstaller
    freezes, so a missing or circular import shows up here rather than in a
    broken ``devbox.exe``.
    """

    def run(self, runner: CommandRunner, *, cwd: Path, env: Mapping[str, str]) -> CheckResult:
        started = time.monotonic()
        result = runner.run(
            (sys.executable, "-m", "devbox", "version"),
            cwd=cwd,
            env=env,
            timeout=120.0,
        )
        ok = result.ok and "Devbox v" in result.stdout
        return CheckResult(
            name=self.name,
            outcome=CheckOutcome.PASSED if ok else CheckOutcome.FAILED,
            duration=time.monotonic() - started,
            summary=result.stdout.strip() or "no output",
            detail="" if ok else (result.stderr or result.stdout).strip(),
            mandatory=self.mandatory,
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The outcome of a whole run."""

    results: tuple[CheckResult, ...]
    duration: float
    platform: str

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.failed)

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.blocks_success)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.outcome is CheckOutcome.SKIPPED)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def total(self, key: str) -> int:
        return sum(result.counts.get(key, 0) for result in self.results)

    def skip_lines(self) -> tuple[str, ...]:
        """Human-readable "N skipped: why" lines, aggregated across checks.

        This is the part the specification insists on: a run that skipped the
        Windows tests must say so, with the count and the reason.
        """
        aggregated: dict[str, int] = {}
        for result in self.results:
            for count, reason in result.skip_reasons:
                aggregated[reason] = aggregated.get(reason, 0) + count
            if result.outcome is CheckOutcome.SKIPPED and not result.skip_reasons:
                aggregated[result.summary or "skipped"] = aggregated.get(result.summary, 0)
        lines: list[str] = []
        for reason, count in sorted(aggregated.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"{count} test(s) skipped: {reason}" if count else f"skipped: {reason}")
        return tuple(lines)

    def exit_code(self) -> int:
        from devbox.errors import ExitCode

        return ExitCode.OK if self.ok else ExitCode.VALIDATION


@dataclass
class ValidationSuite:
    """Runs the checks and produces a report."""

    root: Path
    checks: Sequence[Check] = field(default_factory=lambda: default_checks())
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def child_environment(self) -> dict[str, str]:
        """Environment for the child processes the suite starts.

        ``DEVBOX_HOME`` is redirected into a scratch directory so a validation
        run can never touch the developer's real installed runtimes, and
        ``PYTHONPATH`` points at ``src`` so the suite works in a fresh clone that
        has not been ``pip install -e``'d yet.
        """
        env = dict(self.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
        )
        env["NO_COLOR"] = "1"
        return env

    def run(
        self,
        *,
        only: Sequence[str] = (),
        on_result: ResultSink | None = None,
    ) -> ValidationReport:
        selected = [
            check for check in self.checks if not only or check.name in only or check.group in only
        ]
        env = self.child_environment()
        started = time.monotonic()
        results: list[CheckResult] = []
        for check in selected:
            result = check.run(self.runner, cwd=self.root, env=env)
            results.append(result)
            if on_result is not None:
                on_result(result)
        return ValidationReport(
            results=tuple(results),
            duration=time.monotonic() - started,
            platform=current_platform().tag,
        )
