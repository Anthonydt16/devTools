"""Running short-lived commands and capturing their output.

Distinct from the supervisor on purpose: a supervisor owns a long-running
service and hands its output to log files; a runner executes something that is
expected to finish — ``initdb``, ``createdb``, ``psql -c``, a build step, an
``init.if_missing`` hook — and needs its stdout and stderr back.

Always an argv list, never ``shell=True``.  The runner is a protocol so that
every argument list Devbox builds can be asserted on in a unit test without
running anything, which is how the database and build layers are covered on a
machine with no PostgreSQL.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from devbox.errors import ProcessError

__all__ = ["CommandResult", "CommandRunner", "RecordedCall", "RecordingRunner", "SubprocessRunner"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a finished command produced."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def command_line(self) -> str:
        from devbox.security.command import quote_for_display

        return quote_for_display(self.argv)

    def raise_for_status(self, *, what: str) -> CommandResult:
        """Turn a non-zero exit into an error carrying the output that explains it."""
        if self.ok:
            return self
        if self.timed_out:
            msg = f"{what} timed out after {self.duration:.1f}s\n  command: {self.command_line()}"
            raise ProcessError(msg)
        detail = (self.stderr or self.stdout).strip()
        tail = "\n  output: " + detail.replace("\n", "\n          ") if detail else ""
        msg = (
            f"{what} failed with exit code {self.returncode}\n"
            f"  command: {self.command_line()}{tail}"
        )
        raise ProcessError(msg)


@runtime_checkable
class CommandRunner(Protocol):
    """Executes a command and waits for it."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """The real runner."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> CommandResult:
        arguments = tuple(str(part) for part in argv)
        if not arguments:
            msg = "refusing to run an empty command"
            raise ProcessError(msg)
        started = self._clock()
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never a shell
                list(arguments),
                cwd=None if cwd is None else str(cwd),
                env=None if env is None else dict(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=stdin,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=arguments,
                returncode=-1,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration=self._clock() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            msg = f"executable not found: {arguments[0]}"
            raise ProcessError(
                msg,
                hint="It is looked up through the PATH built for this command, "
                "not the machine PATH; check the runtime is installed.",
            ) from exc
        except OSError as exc:
            msg = f"cannot run {arguments[0]}: {exc}"
            raise ProcessError(msg) from exc
        return CommandResult(
            argv=arguments,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration=self._clock() - started,
        )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One call captured by :class:`RecordingRunner`."""

    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str]
    stdin: str | None

    def command_line(self) -> str:
        from devbox.security.command import quote_for_display

        return quote_for_display(self.argv)


@dataclass
class RecordingRunner:
    """A runner that records calls and replays scripted answers.

    Not a mock of the *behaviour* under test: it stands in for PostgreSQL, which
    genuinely cannot run in the Linux container.  Everything Devbox is
    responsible for — which arguments, in which order, with which environment,
    and what it does with the output — is asserted for real.
    """

    responses: dict[str, CommandResult] = field(default_factory=dict)
    default: CommandResult | None = None
    calls: list[RecordedCall] = field(default_factory=list)

    def script(self, match: str, result: CommandResult) -> RecordingRunner:
        """Answer *result* whenever the rendered command line contains *match*."""
        self.responses[match] = result
        return self

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout
        arguments = tuple(str(part) for part in argv)
        self.calls.append(
            RecordedCall(
                argv=arguments,
                cwd=None if cwd is None else str(cwd),
                env=dict(env or {}),
                stdin=stdin,
            )
        )
        rendered = " ".join(arguments)
        for match, result in self.responses.items():
            if match in rendered:
                return CommandResult(
                    argv=arguments,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                )
        if self.default is not None:
            return CommandResult(
                argv=arguments,
                returncode=self.default.returncode,
                stdout=self.default.stdout,
                stderr=self.default.stderr,
            )
        return CommandResult(argv=arguments, returncode=0)

    def commands(self) -> list[str]:
        return [call.command_line() for call in self.calls]

    def ran(self, fragment: str) -> bool:
        return any(fragment in line for line in self.commands())
