"""``${...}`` interpolation for environment values.

Two phases, on purpose:

*Validation* (:func:`collect_references`) runs at load time and rejects any
reference that names something the configuration does not declare — before a
single process is started.

*Resolution* (:class:`InterpolationContext`) runs at start-up, once databases and
infrastructure have real hosts, ports and credentials.

Supported references::

    ${database.<name>.url|host|port|user|password|name}
    ${infrastructure.<name>.url|host|port}
    ${env.<VARIABLE>}
    ${project.name|root}

``$$`` is a literal dollar sign.  Anything else is an error rather than a silent
pass-through: a typo in a connection string is much cheaper to find here than in
a service that starts and then fails to connect.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from devbox.errors import ConfigError, ValidationIssue

__all__ = [
    "InterpolationContext",
    "Reference",
    "collect_references",
    "interpolate",
    "validate_references",
]

_REFERENCE_RE = re.compile(r"\$\$|\$\{([^}]*)\}")

DATABASE_FIELDS = ("url", "host", "port", "user", "password", "name")
INFRASTRUCTURE_FIELDS = ("url", "host", "port")
PROJECT_FIELDS = ("name", "root")
NAMESPACES = ("database", "infrastructure", "env", "project")


@dataclass(frozen=True, slots=True)
class Reference:
    """A parsed ``${namespace.target.field}`` expression."""

    raw: str
    namespace: str
    target: str
    field: str

    def __str__(self) -> str:
        return "${" + self.raw + "}"


def _parse(raw: str) -> Reference | None:
    parts = raw.split(".")
    if len(parts) == 2 and parts[0] == "env":
        return Reference(raw=raw, namespace="env", target=parts[1], field="")
    if len(parts) == 2 and parts[0] == "project":
        return Reference(raw=raw, namespace="project", target="", field=parts[1])
    if len(parts) == 3:
        return Reference(raw=raw, namespace=parts[0], target=parts[1], field=parts[2])
    return None


def collect_references(text: str) -> tuple[Reference | str, ...]:
    """Return every reference in *text*; unparseable ones come back as raw strings."""
    found: list[Reference | str] = []
    for match in _REFERENCE_RE.finditer(text):
        if match.group(0) == "$$":
            continue
        raw = match.group(1)
        parsed = _parse(raw)
        found.append(parsed if parsed is not None else raw)
    return tuple(found)


def validate_references(
    text: str,
    *,
    path: str,
    databases: frozenset[str],
    infrastructure: frozenset[str],
) -> list[ValidationIssue]:
    """Check every reference in *text* against what the project declares."""
    issues: list[ValidationIssue] = []
    for item in collect_references(text):
        if isinstance(item, str):
            issues.append(
                ValidationIssue(
                    path,
                    f"invalid interpolation '${{{item}}}': expected "
                    "'${database.<name>.<field>}', '${infrastructure.<name>.<field>}', "
                    "'${env.<VAR>}' or '${project.<field>}'",
                )
            )
            continue
        issues.extend(_validate_one(item, path, databases, infrastructure))
    return issues


def _validate_one(
    ref: Reference,
    path: str,
    databases: frozenset[str],
    infrastructure: frozenset[str],
) -> list[ValidationIssue]:
    if ref.namespace not in NAMESPACES:
        return [
            ValidationIssue(
                path,
                f"unknown interpolation namespace {ref.namespace!r} in {ref}; "
                f"expected one of {', '.join(NAMESPACES)}",
            )
        ]
    if ref.namespace == "env":
        if not ref.target:
            return [ValidationIssue(path, f"{ref} is missing an environment variable name")]
        return []
    if ref.namespace == "project":
        if ref.field not in PROJECT_FIELDS:
            return [
                ValidationIssue(
                    path,
                    f"unknown project field {ref.field!r} in {ref}; "
                    f"expected one of {', '.join(PROJECT_FIELDS)}",
                )
            ]
        return []
    if ref.namespace == "database":
        if ref.target not in databases:
            known = ", ".join(sorted(databases)) or "<none declared>"
            return [
                ValidationIssue(
                    path, f"unknown database {ref.target!r} in {ref} (declared: {known})"
                )
            ]
        if ref.field not in DATABASE_FIELDS:
            return [
                ValidationIssue(
                    path,
                    f"unknown database field {ref.field!r} in {ref}; "
                    f"expected one of {', '.join(DATABASE_FIELDS)}",
                )
            ]
        return []
    if ref.target not in infrastructure:
        known = ", ".join(sorted(infrastructure)) or "<none declared>"
        return [
            ValidationIssue(
                path, f"unknown infrastructure {ref.target!r} in {ref} (declared: {known})"
            )
        ]
    if ref.field not in INFRASTRUCTURE_FIELDS:
        return [
            ValidationIssue(
                path,
                f"unknown infrastructure field {ref.field!r} in {ref}; "
                f"expected one of {', '.join(INFRASTRUCTURE_FIELDS)}",
            )
        ]
    return []


@dataclass(frozen=True, slots=True)
class InterpolationContext:
    """Concrete values available to ``${...}`` at start-up."""

    databases: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    infrastructure: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    environ: Mapping[str, str] = field(default_factory=dict)
    project: Mapping[str, str] = field(default_factory=dict)

    def lookup(self, ref: Reference) -> str:
        match ref.namespace:
            case "env":
                value = self.environ.get(ref.target)
                if value is None:
                    msg = f"{ref} refers to environment variable {ref.target!r}, which is not set"
                    raise ConfigError(
                        msg,
                        hint=(
                            f"Set it before running Devbox: $env:{ref.target}='...' (PowerShell)."
                        ),
                    )
                return value
            case "project":
                return self.project.get(ref.field, "")
            case "database":
                return self._from_table(self.databases, ref, "database")
            case _:
                return self._from_table(self.infrastructure, ref, "infrastructure")

    @staticmethod
    def _from_table(
        table: Mapping[str, Mapping[str, str]],
        ref: Reference,
        label: str,
    ) -> str:
        entry = table.get(ref.target)
        if entry is None:
            known = ", ".join(sorted(table)) or "<none>"
            msg = f"{ref} refers to unknown {label} {ref.target!r} (available: {known})"
            raise ConfigError(msg)
        value = entry.get(ref.field)
        if value is None:
            msg = (
                f"{ref} refers to field {ref.field!r} of {label} {ref.target!r}, "
                "which has no value in this environment"
            )
            raise ConfigError(
                msg,
                hint=(
                    "An 'external' dependency only exposes what its url_env provides; "
                    "check docs/devbox-yml.md."
                ),
            )
        return value


def interpolate(text: str, context: InterpolationContext, *, path: str = "") -> str:
    """Replace every ``${...}`` in *text*.  Raises on an unresolvable reference."""

    def replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        raw = match.group(1)
        ref = _parse(raw)
        if ref is None:
            location = f"{path}: " if path else ""
            msg = f"{location}invalid interpolation '${{{raw}}}'"
            raise ConfigError(msg)
        return context.lookup(ref)

    return _REFERENCE_RE.sub(replace, text)


def interpolate_mapping(
    values: Mapping[str, str],
    context: InterpolationContext,
    *,
    path: str = "",
) -> dict[str, str]:
    return {
        key: interpolate(value, context, path=f"{path}.{key}" if path else key)
        for key, value in values.items()
    }
