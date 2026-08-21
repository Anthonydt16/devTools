"""Load, merge and validate ``devbox.yml``.

Pipeline::

    read bytes -> parse YAML -> apply environment overlay -> validate -> ProjectConfig

Every problem is reported at once, located by dotted path, and *before* anything
is started.  ``devbox up`` on an invalid file must never leave a half-started
project behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import yaml

from devbox.catalogs.providers import known_providers, provider_descriptor
from devbox.catalogs.runtime_kinds import known_runtime_kinds
from devbox.config.interpolation import validate_references
from devbox.config.models import (
    DEFAULT_HOST,
    SCHEMA_VERSION,
    DatabaseInit,
    DatabaseSpec,
    Healthcheck,
    HttpHealthcheck,
    InfrastructureSpec,
    Mode,
    ProjectConfig,
    RuntimeRequirement,
    ServiceSpec,
    TcpHealthcheck,
)
from devbox.config.reader import Node, Reader
from devbox.errors import ConfigError

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "load_config",
    "merge_environment",
    "parse_config",
    "parse_yaml_document",
]

DEFAULT_ENVIRONMENT: Final = "local"

_ROOT_KEYS: Final = (
    "version",
    "project",
    "infrastructure",
    "databases",
    "services",
    "environments",
)
_SERVICE_KEYS: Final = (
    "runtime",
    "working_dir",
    "command",
    "env",
    "depends_on",
    "healthcheck",
    "port",
    "stop_timeout",
)
_INFRA_KEYS: Final = (
    "provider",
    "version",
    "mode",
    "host",
    "port",
    "url_env",
    "username",
    "password_env",
    "healthcheck",
    "options",
)
_DATABASE_KEYS: Final = (
    "provider",
    "infrastructure",
    "database",
    "username",
    "password_env",
    "init",
)
_HEALTHCHECK_KINDS: Final = ("http", "tcp")
_TIMING_KEYS: Final = ("interval", "timeout", "retries", "start_period")


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
def load_config(
    path: Path,
    *,
    environment: str = DEFAULT_ENVIRONMENT,
    project_name: str | None = None,
) -> ProjectConfig:
    """Read and validate ``devbox.yml`` from *path*."""
    if not path.is_file():
        msg = f"no configuration file at {path}"
        raise ConfigError(
            msg,
            hint="Run 'devbox init' to create one, or pass --file to point at another location.",
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise ConfigError(msg) from exc
    document = parse_yaml_document(text, source=str(path))
    return parse_config(
        document,
        environment=environment,
        source=str(path),
        project_name=project_name or path.parent.resolve().name,
    )


def parse_yaml_document(text: str, *, source: str) -> Mapping[str, object]:
    """Parse YAML safely and guarantee a mapping at the top level."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).replace("\n", "\n  ")
        msg = f"invalid YAML: {detail}"
        raise ConfigError(msg, source=source) from exc
    if document is None:
        msg = "the file is empty"
        raise ConfigError(msg, source=source, hint="A minimal file needs 'version: 1'.")
    if not isinstance(document, Mapping):
        msg = f"the top level must be a mapping, got {type(document).__name__}"
        raise ConfigError(msg, source=source)
    return {str(key): value for key, value in document.items()}


def merge_environment(
    document: Mapping[str, object],
    environment: str,
    *,
    source: str | None = None,
) -> Mapping[str, object]:
    """Deep-merge ``environments.<environment>`` into the root document.

    Overlays are applied to the *raw* document, before validation, so an overlay
    can change any field and the result is validated exactly once.  The
    ``environments`` block is removed from the result.
    """
    environments = document.get("environments")
    merged: dict[str, object] = {k: v for k, v in document.items() if k != "environments"}
    if environments is None:
        if environment != DEFAULT_ENVIRONMENT:
            msg = f"unknown environment {environment!r}: the file declares no 'environments' block"
            raise ConfigError(msg, source=source)
        return merged
    if not isinstance(environments, Mapping):
        msg = f"'environments' must be a mapping, got {type(environments).__name__}"
        raise ConfigError(msg, source=source)

    overlay = environments.get(environment)
    if overlay is None:
        if environment == DEFAULT_ENVIRONMENT:
            return merged
        known = ", ".join(sorted(str(k) for k in environments)) or "<none>"
        msg = f"unknown environment {environment!r}"
        raise ConfigError(msg, source=source, hint=f"Declared environments: {known}")
    if not isinstance(overlay, Mapping):
        msg = f"environments.{environment} must be a mapping, got {type(overlay).__name__}"
        raise ConfigError(msg, source=source)
    return _deep_merge(merged, overlay)


def _deep_merge(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    """Recursive mapping merge; lists and scalars are replaced, not concatenated."""
    result: dict[str, object] = dict(base)
    for key, value in overlay.items():
        name = str(key)
        current = result.get(name)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[name] = _deep_merge(
                {str(k): v for k, v in current.items()},
                {str(k): v for k, v in value.items()},
            )
        else:
            result[name] = value
    return result


def parse_config(
    document: Mapping[str, object],
    *,
    environment: str = DEFAULT_ENVIRONMENT,
    source: str | None = None,
    project_name: str = "project",
) -> ProjectConfig:
    """Validate an already-parsed document into a :class:`ProjectConfig`."""
    merged = merge_environment(document, environment, source=source)
    reader = Reader(source=source)
    root = reader.root(merged)
    root.reject_unknown_keys(_ROOT_KEYS)

    version = _read_version(root)
    project = root.child("project").optional_str(project_name) or project_name

    infrastructure = _read_infrastructure(root.child("infrastructure"))
    databases = _read_databases(root.child("databases"))
    services = _read_services(root.child("services"))

    reader.raise_if_invalid(
        "invalid configuration",
        hint="See docs/devbox-yml.md for the full schema.",
    )

    _cross_validate(
        reader,
        services=services,
        databases=databases,
        infrastructure=infrastructure,
        environment=environment,
    )
    reader.raise_if_invalid("invalid configuration")

    return ProjectConfig(
        version=version,
        project=project,
        environment=environment,
        services=services,
        infrastructure=infrastructure,
        databases=databases,
        source_path=source,
    )


# ---------------------------------------------------------------------------
# section readers
# ---------------------------------------------------------------------------
def _read_version(root: Node) -> int:
    node = root.child("version")
    if node.is_missing():
        node.error(f"is required (this build understands version {SCHEMA_VERSION})")
        return SCHEMA_VERSION
    version = node.require_int()
    if version != SCHEMA_VERSION:
        node.error(
            f"unsupported schema version {version}; this build of Devbox understands "
            f"version {SCHEMA_VERSION}"
        )
        return SCHEMA_VERSION
    return version


def _read_runtime(node: Node) -> tuple[RuntimeRequirement, ...]:
    """Read ``runtime: {node: "18.19.0"}``."""
    if node.is_missing():
        return ()
    kinds = known_runtime_kinds()
    requirements: list[RuntimeRequirement] = []
    for name, child in node.children():
        if name not in kinds:
            child.error(f"unknown runtime {name!r} (available: {', '.join(sorted(kinds))})")
            continue
        version = child.scalar_str("")
        if not version:
            child.error("a runtime version is required, e.g. '18.19.0'")
            continue
        requirements.append(RuntimeRequirement(name=name, version=version))
    return tuple(sorted(requirements))


def _read_timing(node: Node) -> dict[str, float | int]:
    return {
        "interval": node.child("interval").positive_number(1.0, allow_zero=False),
        "timeout": node.child("timeout").positive_number(30.0, allow_zero=False),
        "retries": _read_retries(node.child("retries")),
        "start_period": node.child("start_period").positive_number(0.0),
    }


def _read_retries(node: Node) -> int:
    retries = node.optional_int(10) or 0
    if retries < 1:
        node.error(f"retries must be at least 1, got {retries}")
        return 1
    return retries


def _read_healthcheck(node: Node) -> Healthcheck | None:
    if node.is_missing():
        return None
    node.reject_unknown_keys(_HEALTHCHECK_KINDS)
    kind = node.exactly_one_of(_HEALTHCHECK_KINDS)
    if kind is None:
        return None
    body = node.child(kind)
    if kind == "http":
        body.reject_unknown_keys(("url", "method", "expect_status", *_TIMING_KEYS))
        url = body.child("url").require_str()
        if url and not url.startswith(("http://", "https://")):
            body.error_at("url", f"must start with http:// or https://, got {url!r}")
        timing = _read_timing(body)
        return HttpHealthcheck(
            url=url,
            method=body.child("method").enum(("GET", "HEAD", "POST"), "GET"),
            expect_status=body.child("expect_status").int_list(),
            interval=float(timing["interval"]),
            timeout=float(timing["timeout"]),
            retries=int(timing["retries"]),
            start_period=float(timing["start_period"]),
        )
    body.reject_unknown_keys(("host", "port", *_TIMING_KEYS))
    timing = _read_timing(body)
    return TcpHealthcheck(
        host=body.child("host").optional_str(DEFAULT_HOST) or DEFAULT_HOST,
        port=body.child("port").port(required=True) or 0,
        interval=float(timing["interval"]),
        timeout=float(timing["timeout"]),
        retries=int(timing["retries"]),
        start_period=float(timing["start_period"]),
    )


def _read_infrastructure(section: Node) -> dict[str, InfrastructureSpec]:
    providers = known_providers()
    result: dict[str, InfrastructureSpec] = {}
    for name, node in section.children():
        _validate_identifier(node, name, "infrastructure name")
        node.reject_unknown_keys(_INFRA_KEYS)
        provider = node.child("provider").require_str()
        if provider and provider not in providers:
            node.error_at(
                "provider",
                f"unknown provider {provider!r} (available: {', '.join(sorted(providers))})",
            )
        mode = Mode(node.child("mode").enum(tuple(Mode), Mode.MANAGED.value))
        descriptor = provider_descriptor(provider)
        descriptor_port = descriptor.default_port if descriptor is not None else None
        if descriptor is not None and mode is Mode.MANAGED and not descriptor.supports_managed:
            node.error_at(
                "mode", f"provider {provider!r} cannot be managed by Devbox; use mode: external"
            )
        url_env = node.child("url_env").optional_str()
        if mode is Mode.EXTERNAL and not url_env:
            node.error(
                "mode 'external' requires 'url_env': the name of the environment variable "
                "holding the connection URL (secrets never live in devbox.yml)"
            )
        if mode is Mode.MANAGED and url_env:
            node.error_at("url_env", "'url_env' only applies to mode 'external'")
        result[name] = InfrastructureSpec(
            name=name,
            provider=provider,
            version=node.child("version").scalar_str(
                descriptor.default_version if descriptor else ""
            ),
            mode=mode,
            host=node.child("host").optional_str(DEFAULT_HOST) or DEFAULT_HOST,
            port=node.child("port").port() or descriptor_port,
            url_env=url_env,
            username=node.child("username").optional_str(),
            password_env=node.child("password_env").optional_str(),
            healthcheck=_read_healthcheck(node.child("healthcheck")),
            options=node.child("options").string_mapping(),
        )
    return result


def _read_databases(section: Node) -> dict[str, DatabaseSpec]:
    providers = known_providers()
    result: dict[str, DatabaseSpec] = {}
    for name, node in section.children():
        _validate_identifier(node, name, "database name")
        node.reject_unknown_keys(_DATABASE_KEYS)
        provider = node.child("provider").require_str()
        if provider and provider not in providers:
            node.error_at(
                "provider",
                f"unknown provider {provider!r} (available: {', '.join(sorted(providers))})",
            )
        result[name] = DatabaseSpec(
            name=name,
            provider=provider,
            infrastructure=node.child("infrastructure").require_str(),
            database=node.child("database").optional_str(name) or name,
            username=node.child("username").optional_str(),
            password_env=node.child("password_env").optional_str(),
            init=_read_database_init(node.child("init")),
        )
    return result


def _read_database_init(node: Node) -> DatabaseInit | None:
    if node.is_missing():
        return None
    node.reject_unknown_keys(("if_missing",))
    body = node.child("if_missing")
    if body.is_missing():
        node.error("'init' requires an 'if_missing' block")
        return None
    body.reject_unknown_keys(("command", "working_dir", "runtime"))
    return DatabaseInit(
        command=body.child("command").require_str(),
        working_dir=body.child("working_dir").optional_str(),
        runtime=_read_runtime(body.child("runtime")),
    )


def _read_services(section: Node) -> dict[str, ServiceSpec]:
    result: dict[str, ServiceSpec] = {}
    for name, node in section.children():
        _validate_identifier(node, name, "service name")
        node.reject_unknown_keys(_SERVICE_KEYS)
        healthcheck = _read_healthcheck(node.child("healthcheck"))
        result[name] = ServiceSpec(
            name=name,
            command=node.child("command").require_str(),
            working_dir=node.child("working_dir").optional_str(".") or ".",
            runtime=_read_runtime(node.child("runtime")),
            env=node.child("env").string_mapping(),
            depends_on=node.child("depends_on").string_list(),
            healthcheck=healthcheck,
            port=node.child("port").port() or _infer_port(healthcheck),
            stop_timeout=node.child("stop_timeout").positive_number(10.0, allow_zero=False),
        )
    return result


def _infer_port(healthcheck: Healthcheck | None) -> int | None:
    """Best-effort port for display purposes only."""
    match healthcheck:
        case TcpHealthcheck(port=port) if port:
            return port
        case HttpHealthcheck(url=url) if url:
            from urllib.parse import urlparse

            try:
                parsed = urlparse(url)
            except ValueError:
                return None
            return parsed.port
        case _:
            return None


_IDENTIFIER_EXTRA = frozenset("-_.")


def _validate_identifier(node: Node, name: str, label: str) -> None:
    """Names become directory and log-file components, so keep them boring."""
    if not name:
        node.error(f"{label} must not be empty")
        return
    if not all(char.isalnum() or char in _IDENTIFIER_EXTRA for char in name):
        node.error(f"invalid {label} {name!r}: use letters, digits, '-', '_' and '.' only")


# ---------------------------------------------------------------------------
# cross-section validation
# ---------------------------------------------------------------------------
def _cross_validate(
    reader: Reader,
    *,
    services: Mapping[str, ServiceSpec],
    databases: Mapping[str, DatabaseSpec],
    infrastructure: Mapping[str, InfrastructureSpec],
    environment: str,
) -> None:
    """Checks that need the whole document: references, graph, ports, secrets."""
    node_names = set(services) | set(databases) | set(infrastructure)
    database_names = frozenset(databases)
    infrastructure_names = frozenset(infrastructure)

    for name, database in databases.items():
        if database.infrastructure and database.infrastructure not in infrastructure:
            known = ", ".join(sorted(infrastructure)) or "<none declared>"
            reader.error(
                f"databases.{name}.infrastructure",
                f"unknown infrastructure {database.infrastructure!r} (declared: {known})",
            )
            continue
        host = infrastructure.get(database.infrastructure)
        if host is not None and database.provider and host.provider != database.provider:
            reader.error(
                f"databases.{name}.provider",
                f"provider {database.provider!r} does not match infrastructure "
                f"{database.infrastructure!r}, which is a {host.provider!r} server",
            )

    for name, service in services.items():
        for index, dependency in enumerate(service.depends_on):
            if dependency == name:
                reader.error(
                    f"services.{name}.depends_on[{index}]", "a service cannot depend on itself"
                )
            elif dependency not in node_names:
                known = ", ".join(sorted(node_names - {name})) or "<none>"
                reader.error(
                    f"services.{name}.depends_on[{index}]",
                    f"unknown dependency {dependency!r} (known: {known})",
                )
        for key, value in service.env.items():
            reader.issues.extend(
                validate_references(
                    value,
                    path=f"services.{name}.env.{key}",
                    databases=database_names,
                    infrastructure=infrastructure_names,
                )
            )

    _validate_ports(reader, infrastructure)
    _validate_environment_completeness(reader, infrastructure, environment)


def _validate_ports(reader: Reader, infrastructure: Mapping[str, InfrastructureSpec]) -> None:
    """Two managed servers cannot share a port on the same host."""
    seen: dict[tuple[str, int], str] = {}
    for name, spec in infrastructure.items():
        if not spec.is_managed or spec.port is None:
            continue
        key = (spec.host, spec.port)
        previous = seen.get(key)
        if previous is not None:
            reader.error(
                f"infrastructure.{name}.port",
                f"port {spec.port} on {spec.host} is already used by infrastructure {previous!r}",
            )
            continue
        seen[key] = name


def _validate_environment_completeness(
    reader: Reader,
    infrastructure: Mapping[str, InfrastructureSpec],
    environment: str,
) -> None:
    """A production-shaped environment must not rely on locally managed data.

    This is advisory for every environment except that ``external`` entries are
    always required to declare where their URL comes from (checked at read time).
    Here we additionally refuse a *named non-local* environment that still has a
    managed database server, because that is the mistake that would make
    ``devbox up --environment production`` boot a local PostgreSQL.
    """
    if environment in {DEFAULT_ENVIRONMENT, "dev", "development"}:
        return
    managed = [spec.name for spec in infrastructure.values() if spec.is_managed]
    if not managed:
        return
    listed = ", ".join(sorted(managed))
    reader.error(
        f"environments.{environment}.infrastructure",
        f"environment {environment!r} still declares managed infrastructure ({listed}); "
        "non-local environments must use mode: external so Devbox never starts a local "
        "database server",
    )


def known_environments(document: Mapping[str, object]) -> Sequence[str]:
    """Environment names declared in a raw document (for ``--environment`` help)."""
    environments = document.get("environments")
    if not isinstance(environments, Mapping):
        return (DEFAULT_ENVIRONMENT,)
    return tuple(sorted({DEFAULT_ENVIRONMENT, *(str(key) for key in environments)}))
