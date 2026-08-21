"""Configuration validation.

Every failure mode listed in the specification has a case here, and each asserts
on the *message*, not just on the exception type: a validation error nobody can
act on is a bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.config import Mode, ProjectConfig, load_config, parse_config
from devbox.config.loader import merge_environment, parse_yaml_document
from devbox.config.models import HttpHealthcheck, TcpHealthcheck
from devbox.errors import ConfigError

pytestmark = pytest.mark.unit

VALID = """
version: 1
project: customer-portal

infrastructure:
  company-postgres:
    provider: postgres
    version: "17"
    mode: managed
    port: 5432
  company-mysql:
    provider: mysql
    version: "8.4"
    mode: managed
    port: 3306

databases:
  clients:
    provider: postgres
    infrastructure: company-postgres
    database: clients
    init:
      if_missing:
        command: npm run db:init
  legacy:
    provider: mysql
    infrastructure: company-mysql
    database: legacy

services:
  backend:
    runtime: { node: "18.19.0" }
    working_dir: ./backend
    command: npm run dev
    env:
      DATABASE_URL: ${database.clients.url}
      MYSQL_URL: ${database.legacy.url}
    depends_on: [company-postgres, company-mysql]
    healthcheck:
      http: { url: "http://127.0.0.1:3000/health", timeout: 30, retries: 10, start_period: 5 }
  frontend:
    runtime: { node: "24.6.0" }
    working_dir: ./frontend
    command: npm run dev
    env: { API_URL: "http://127.0.0.1:3000" }
    healthcheck:
      tcp: { host: 127.0.0.1, port: 5173 }

environments:
  local:
    infrastructure:
      company-postgres: { mode: managed }
  production:
    infrastructure:
      company-postgres: { mode: external, url_env: PROD_DATABASE_URL }
      company-mysql: { mode: external, url_env: PROD_MYSQL_URL }
"""


def load(text: str, *, environment: str = "local") -> ProjectConfig:
    document = parse_yaml_document(text, source="devbox.yml")
    return parse_config(document, environment=environment, source="devbox.yml")


def expect_error(text: str, *, environment: str = "local") -> str:
    """Load *text*, expect it to be rejected, and return the rendered message."""
    with pytest.raises(ConfigError) as caught:
        load(text, environment=environment)
    return str(caught.value)


class TestValidConfiguration:
    def test_reference_configuration_parses(self) -> None:
        config = load(VALID)
        assert config.project == "customer-portal"
        assert sorted(config.services) == ["backend", "frontend"]
        assert sorted(config.infrastructure) == ["company-mysql", "company-postgres"]

    def test_runtimes_are_collected_and_sorted(self) -> None:
        """Managed servers count too: their binaries have to be installed."""
        config = load(VALID)
        assert [str(r) for r in config.required_runtimes()] == [
            "mysql 8.4",
            "node 18.19.0",
            "node 24.6.0",
            "postgres 17",
        ]

    def test_an_external_server_needs_no_runtime_installed(self) -> None:
        config = load(VALID, environment="production")
        assert [str(r) for r in config.required_runtimes()] == [
            "node 18.19.0",
            "node 24.6.0",
        ]

    def test_healthchecks_are_typed(self) -> None:
        config = load(VALID)
        backend = config.services["backend"]
        frontend = config.services["frontend"]
        assert isinstance(backend.healthcheck, HttpHealthcheck)
        assert backend.healthcheck.timeout == 30.0
        assert backend.healthcheck.start_period == 5.0
        assert isinstance(frontend.healthcheck, TcpHealthcheck)
        assert frontend.healthcheck.port == 5173

    def test_port_is_inferred_from_the_healthcheck(self) -> None:
        config = load(VALID)
        assert config.services["backend"].port == 3000
        assert config.services["frontend"].port == 5173

    def test_minimal_configuration_is_valid(self) -> None:
        config = load("version: 1\n")
        assert config.services == {}
        assert config.required_runtimes() == ()

    def test_yaml_numbers_are_accepted_where_a_version_string_is_expected(self) -> None:
        config = load(
            "version: 1\ninfrastructure:\n  db:\n    provider: postgres\n    version: 17\n"
        )
        assert config.infrastructure["db"].version == "17"

    def test_env_values_are_stringified(self) -> None:
        config = load(
            "version: 1\n"
            "services:\n"
            "  a:\n"
            "    command: x\n"
            "    env:\n"
            "      PORT: 3000\n"
            "      DEBUG: true\n"
        )
        assert config.services["a"].env == {"PORT": "3000", "DEBUG": "true"}


class TestStructuralErrors:
    def test_missing_version(self) -> None:
        assert "version: is required" in expect_error("services: {}\n")

    def test_unsupported_version(self) -> None:
        assert "unsupported schema version 99" in expect_error("version: 99\n")

    def test_empty_file(self) -> None:
        with pytest.raises(ConfigError, match="the file is empty"):
            parse_yaml_document("", source="devbox.yml")

    def test_invalid_yaml(self) -> None:
        with pytest.raises(ConfigError, match="invalid YAML"):
            parse_yaml_document("a: [1, 2\n", source="devbox.yml")

    def test_top_level_must_be_a_mapping(self) -> None:
        with pytest.raises(ConfigError, match="top level must be a mapping"):
            parse_yaml_document("- a\n- b\n", source="devbox.yml")

    def test_unknown_root_key_is_rejected_with_a_suggestion(self) -> None:
        message = expect_error("version: 1\nservice:\n  a: {}\n")
        assert "unknown key 'service'" in message
        assert "did you mean 'services'" in message

    def test_unknown_service_key_is_rejected(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    depend_on: []\n")
        assert "services.a.depend_on: unknown key 'depend_on'" in message
        assert "did you mean 'depends_on'" in message


class TestFieldErrors:
    def test_missing_required_field(self) -> None:
        assert "services.a.command: is required" in expect_error("version: 1\nservices:\n  a: {}\n")

    def test_wrong_scalar_type(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: 3\n")
        assert "services.a.command: expected a string, got an integer (3)" in message

    def test_boolean_is_not_an_integer(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    port: true\n")
        assert "expected an integer, got a boolean" in message

    def test_port_range_is_checked(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    port: 70000\n")
        assert "port must be between 1 and 65535, got 70000" in message

    def test_depends_on_must_be_a_list(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    depends_on: b\n")
        assert "expected a list of strings, got a string" in message

    def test_every_problem_is_reported_at_once(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: 3\n    port: 99999\n    depends_on: nope\n"
        )
        assert message.count("  - ") == 3


class TestRuntimeErrors:
    def test_unknown_runtime(self) -> None:
        message = expect_error(
            'version: 1\nservices:\n  a:\n    command: x\n    runtime: { go: "1.22" }\n'
        )
        assert "unknown runtime 'go'" in message
        assert "node" in message

    def test_runtime_without_a_version(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    runtime: { node: }\n"
        )
        assert "a runtime version is required" in message


class TestHealthcheckErrors:
    def test_exactly_one_kind_is_required(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    healthcheck: {}\n")
        assert "exactly one of [http, tcp] is required, got none" in message

    def test_two_kinds_are_rejected(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    healthcheck:\n"
            '      http: { url: "http://127.0.0.1:1/h" }\n      tcp: { port: 1 }\n'
        )
        assert "exactly one of [http, tcp] is allowed, got 2" in message

    def test_url_scheme_is_checked(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    healthcheck:\n"
            '      http: { url: "127.0.0.1:3000/health" }\n'
        )
        assert "must start with http:// or https://" in message

    def test_tcp_port_is_required(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    healthcheck:\n      tcp: {}\n"
        )
        assert "services.a.healthcheck.tcp.port: is required" in message

    def test_retries_must_be_positive(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    healthcheck:\n"
            "      tcp: { port: 1, retries: 0 }\n"
        )
        assert "retries must be at least 1, got 0" in message

    def test_interval_must_not_be_zero(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    healthcheck:\n"
            "      tcp: { port: 1, interval: 0 }\n"
        )
        assert "must not be zero or negative" in message


class TestCrossReferenceErrors:
    def test_unknown_dependency(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    depends_on: [ghost]\n"
        )
        assert "unknown dependency 'ghost'" in message

    def test_self_dependency(self) -> None:
        message = expect_error("version: 1\nservices:\n  a:\n    command: x\n    depends_on: [a]\n")
        assert "a service cannot depend on itself" in message

    def test_unknown_infrastructure_for_a_database(self) -> None:
        message = expect_error(
            "version: 1\ndatabases:\n  d:\n    provider: postgres\n    infrastructure: ghost\n"
        )
        assert "unknown infrastructure 'ghost'" in message

    def test_provider_mismatch_between_database_and_server(self) -> None:
        message = expect_error(
            "version: 1\n"
            "infrastructure:\n  pg:\n    provider: postgres\n"
            "databases:\n  d:\n    provider: mysql\n    infrastructure: pg\n"
        )
        assert "does not match infrastructure 'pg', which is a 'postgres' server" in message

    def test_unknown_provider(self) -> None:
        message = expect_error("version: 1\ninfrastructure:\n  x:\n    provider: oracle\n")
        assert "unknown provider 'oracle'" in message
        assert "postgres" in message

    def test_duplicate_managed_port(self) -> None:
        message = expect_error(
            "version: 1\ninfrastructure:\n"
            "  a: { provider: postgres, port: 5432 }\n"
            "  b: { provider: postgres, port: 5432 }\n"
        )
        assert "port 5432 on 127.0.0.1 is already used by infrastructure" in message

    def test_invalid_identifier(self) -> None:
        message = expect_error("version: 1\nservices:\n  'bad/name':\n    command: x\n")
        assert "invalid service name 'bad/name'" in message


class TestInterpolationValidation:
    def test_unknown_database_reference(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n"
            "    env: { URL: '${database.ghost.url}' }\n"
        )
        assert "unknown database 'ghost'" in message

    def test_unknown_field(self) -> None:
        message = expect_error(
            "version: 1\n"
            "infrastructure:\n  pg: { provider: postgres }\n"
            "databases:\n  d: { provider: postgres, infrastructure: pg }\n"
            "services:\n  a:\n    command: x\n    env: { URL: '${database.d.secret}' }\n"
        )
        assert "unknown database field 'secret'" in message

    def test_malformed_reference(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    env: { URL: '${nonsense}' }\n"
        )
        assert "invalid interpolation '${nonsense}'" in message

    def test_unknown_namespace(self) -> None:
        message = expect_error(
            "version: 1\nservices:\n  a:\n    command: x\n    env: { URL: '${secrets.a.b}' }\n"
        )
        assert "unknown interpolation namespace 'secrets'" in message

    def test_env_reference_is_accepted(self) -> None:
        config = load(
            "version: 1\nservices:\n  a:\n    command: x\n    env: { TOKEN: '${env.CI_TOKEN}' }\n"
        )
        assert config.services["a"].env["TOKEN"] == "${env.CI_TOKEN}"


class TestModes:
    def test_external_requires_url_env(self) -> None:
        message = expect_error(
            "version: 1\ninfrastructure:\n  pg: { provider: postgres, mode: external }\n"
        )
        assert "mode 'external' requires 'url_env'" in message

    def test_url_env_is_rejected_in_managed_mode(self) -> None:
        message = expect_error(
            "version: 1\ninfrastructure:\n  pg: { provider: postgres, mode: managed, url_env: X }\n"
        )
        assert "'url_env' only applies to mode 'external'" in message

    def test_unknown_mode(self) -> None:
        message = expect_error(
            "version: 1\ninfrastructure:\n  pg: { provider: postgres, mode: hybrid }\n"
        )
        assert "expected one of [managed, external], got 'hybrid'" in message


class TestEnvironments:
    def test_overlay_replaces_scalars(self) -> None:
        config = load(VALID, environment="production")
        postgres = config.infrastructure["company-postgres"]
        assert postgres.mode is Mode.EXTERNAL
        assert postgres.url_env == "PROD_DATABASE_URL"

    def test_production_starts_no_local_infrastructure(self) -> None:
        config = load(VALID, environment="production")
        assert config.managed_infrastructure() == ()

    def test_local_is_the_default(self) -> None:
        config = load(VALID)
        assert config.environment == "local"
        assert len(config.managed_infrastructure()) == 2

    def test_unknown_environment(self) -> None:
        with pytest.raises(ConfigError, match="unknown environment 'staging'"):
            load(VALID, environment="staging")

    def test_a_non_local_environment_may_not_keep_managed_infrastructure(self) -> None:
        text = (
            "version: 1\n"
            "infrastructure:\n  pg: { provider: postgres }\n"
            "environments:\n  production: {}\n"
        )
        message = expect_error(text, environment="production")
        assert "still declares managed infrastructure (pg)" in message

    def test_overlay_merges_deeply_without_dropping_siblings(self) -> None:
        text = (
            "version: 1\n"
            "services:\n  a:\n    command: x\n    env: { A: '1', B: '2' }\n"
            "environments:\n  ci:\n    services:\n      a:\n        env: { B: '9' }\n"
        )
        config = load(text, environment="ci")
        assert config.services["a"].env == {"A": "1", "B": "9"}
        assert config.services["a"].command == "x"

    def test_merge_environment_removes_the_environments_block(self) -> None:
        merged = merge_environment({"version": 1, "environments": {"local": {}}}, "local")
        assert "environments" not in merged


class TestFileLevelBehaviour:
    def test_missing_file_has_an_actionable_hint(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            load_config(tmp_path / "devbox.yml")
        assert "no configuration file at" in str(caught.value)
        assert caught.value.hint is not None
        assert "devbox init" in caught.value.hint

    def test_utf8_bom_is_tolerated(self, tmp_path: Path) -> None:
        """Windows editors write a BOM; a config file must still load."""
        path = tmp_path / "devbox.yml"
        path.write_bytes(b"\xef\xbb\xbfversion: 1\n")
        assert load_config(path).version == 1

    def test_project_name_defaults_to_the_directory_name(self, tmp_path: Path) -> None:
        directory = tmp_path / "customer portal"
        directory.mkdir()
        path = directory / "devbox.yml"
        path.write_text("version: 1\n", encoding="utf-8")
        assert load_config(path).project == "customer portal"

    def test_source_path_is_recorded_for_error_messages(self, tmp_path: Path) -> None:
        path = tmp_path / "devbox.yml"
        path.write_text("version: 1\nservices:\n  a: {}\n", encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_config(path)
        assert str(path) in str(caught.value)
