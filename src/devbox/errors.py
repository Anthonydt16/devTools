"""Business error hierarchy for Devbox.

Every error the user can legitimately hit is modelled here.  Each class carries
a stable :attr:`exit_code` so the CLI can translate an exception into a reliable
process exit status without a chain of ``isinstance`` checks, and an optional
:attr:`hint` rendered as an actionable next step.

Python tracebacks are *never* shown to the end user unless ``--debug`` is set;
the CLI renders ``str(error)`` plus the hint instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "BuildError",
    "ConfigError",
    "DatabaseError",
    "DependencyGraphError",
    "DevboxError",
    "ExitCode",
    "HealthcheckError",
    "InfrastructureError",
    "IntegrityError",
    "LockfileError",
    "ProcessError",
    "RegistryError",
    "RuntimeInstallError",
    "SecurityError",
    "ServiceStartError",
    "StateError",
    "UnsupportedPlatformError",
    "ValidationIssue",
    "ValidationSuiteError",
]


class ExitCode:
    """Process exit codes.

    ``0``/``1``/``2`` follow POSIX and Click conventions (``2`` is emitted by the
    CLI parser itself for usage errors).  Everything above ``2`` is a Devbox
    domain failure so CI can branch on the cause.
    """

    OK = 0
    FAILURE = 1
    USAGE = 2
    CONFIG = 3
    LOCKFILE = 4
    REGISTRY = 5
    SERVICE = 6
    HEALTHCHECK = 7
    INFRASTRUCTURE = 8
    STATE = 9
    SECURITY = 10
    BUILD = 11
    VALIDATION = 12
    UNSUPPORTED_PLATFORM = 13
    DATABASE = 14


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single validation problem, located by a dotted path into the document."""

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class DevboxError(Exception):
    """Base class for every expected Devbox failure.

    ``DevboxError`` is what the CLI catches and renders.  Anything that escapes
    it is a bug and will surface as a traceback.
    """

    exit_code: int = ExitCode.FAILURE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(DevboxError):
    """``devbox.yml`` is missing, malformed, or semantically invalid.

    When several problems are found they are all reported at once so the user
    does not have to fix them one round-trip at a time.
    """

    exit_code = ExitCode.CONFIG

    def __init__(
        self,
        message: str,
        *,
        issues: Sequence[ValidationIssue] = (),
        source: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        self.source = source

    def __str__(self) -> str:
        prefix = f"{self.source}: " if self.source else ""
        if not self.issues:
            return f"{prefix}{self.message}"
        lines = [f"{prefix}{self.message}"]
        lines.extend(f"  - {issue.render()}" for issue in self.issues)
        return "\n".join(lines)


class LockfileError(DevboxError):
    """``devbox.lock`` is missing, unreadable, or inconsistent with ``devbox.yml``."""

    exit_code = ExitCode.LOCKFILE


class RegistryError(DevboxError):
    """The runtime registry is unreachable or returned something unusable."""

    exit_code = ExitCode.REGISTRY


class IntegrityError(RegistryError):
    """A downloaded artifact failed SHA256 verification.

    Raised *before* anything is extracted or executed.
    """

    exit_code = ExitCode.SECURITY

    def __init__(self, message: str, *, expected: str, actual: str) -> None:
        super().__init__(message, hint="The artifact was discarded and nothing was executed.")
        self.expected = expected
        self.actual = actual


class SecurityError(DevboxError):
    """A refused operation: path traversal, unsafe archive member, unsafe command."""

    exit_code = ExitCode.SECURITY


class RuntimeInstallError(DevboxError):
    """A runtime could not be installed or is not usable once installed."""

    exit_code = ExitCode.REGISTRY


class ProcessError(DevboxError):
    """A supervised process could not be started, inspected, or stopped."""

    exit_code = ExitCode.SERVICE


@dataclass(frozen=True, slots=True)
class ProcessFailureDetails:
    """Everything needed to render the standard ``failed to start`` block."""

    command: str
    exit_code: int | None = None
    stderr_tail: str = ""
    stdout_log: str | None = None
    stderr_log: str | None = None


class ServiceStartError(ProcessError):
    """A service exited or never became healthy during ``devbox up``."""

    exit_code = ExitCode.SERVICE

    def __init__(
        self,
        service: str,
        message: str,
        *,
        details: ProcessFailureDetails | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.service = service
        self.details = details


class HealthcheckError(DevboxError):
    """A healthcheck never passed within its retry budget."""

    exit_code = ExitCode.HEALTHCHECK


class DependencyGraphError(ConfigError):
    """``depends_on`` references an unknown node or forms a cycle."""

    exit_code = ExitCode.CONFIG


class InfrastructureError(DevboxError):
    """Shared infrastructure (PostgreSQL, MySQL, ...) could not be managed."""

    exit_code = ExitCode.INFRASTRUCTURE


class DatabaseError(DevboxError):
    """A logical database could not be inspected, created, or initialised."""

    exit_code = ExitCode.DATABASE


class StateError(DevboxError):
    """The on-disk state directory is missing, corrupt, or not owned by us."""

    exit_code = ExitCode.STATE


class BuildError(DevboxError):
    """``devbox build`` failed to produce a valid artifact."""

    exit_code = ExitCode.BUILD


class ValidationSuiteError(DevboxError):
    """``devbox test`` reported at least one failing mandatory check."""

    exit_code = ExitCode.VALIDATION


class UnsupportedPlatformError(DevboxError):
    """The requested operation has no implementation for the current platform."""

    exit_code = ExitCode.UNSUPPORTED_PLATFORM


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Optional structured context attached to a rendered error block."""

    fields: dict[str, str] = field(default_factory=dict)

    def rows(self) -> list[tuple[str, str]]:
        return [(key, value) for key, value in self.fields.items() if value]
