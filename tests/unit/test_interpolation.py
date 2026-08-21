"""``${...}`` interpolation: validation at load time, resolution at start-up."""

from __future__ import annotations

import pytest

from devbox.config.interpolation import (
    InterpolationContext,
    collect_references,
    interpolate,
    interpolate_mapping,
    validate_references,
)
from devbox.errors import ConfigError

pytestmark = pytest.mark.unit

DATABASES = frozenset({"clients", "legacy"})
INFRASTRUCTURE = frozenset({"company-postgres"})


def issues(text: str) -> list[str]:
    return [
        issue.message
        for issue in validate_references(
            text, path="services.a.env.X", databases=DATABASES, infrastructure=INFRASTRUCTURE
        )
    ]


@pytest.fixture
def context() -> InterpolationContext:
    return InterpolationContext(
        databases={
            "clients": {
                "url": "postgresql://devbox@127.0.0.1:5432/clients",
                "name": "clients",
                "host": "127.0.0.1",
                "port": "5432",
                "user": "devbox",
                "password": "",
            }
        },
        infrastructure={
            "company-postgres": {
                "host": "127.0.0.1",
                "port": "5432",
                "url": "postgresql://devbox@127.0.0.1:5432/postgres",
            }
        },
        environ={"CI_TOKEN": "abc123", "EMPTY": ""},
        project={"name": "customer-portal", "root": "C:\\projects\\portal"},
    )


class TestParsing:
    def test_references_are_found(self) -> None:
        found = collect_references("a=${database.clients.url} b=${env.TOKEN}")
        assert len(found) == 2

    def test_a_literal_dollar_is_not_a_reference(self) -> None:
        assert collect_references("cost is $$5") == ()

    def test_a_malformed_reference_comes_back_raw(self) -> None:
        found = collect_references("${nonsense}")
        assert found == ("nonsense",)


class TestValidation:
    def test_a_valid_database_reference_passes(self) -> None:
        assert issues("${database.clients.url}") == []

    def test_an_unknown_database_is_reported_with_the_alternatives(self) -> None:
        message = issues("${database.ghost.url}")[0]
        assert "unknown database 'ghost'" in message
        assert "clients, legacy" in message

    def test_an_unknown_database_field_is_reported(self) -> None:
        assert "unknown database field 'secret'" in issues("${database.clients.secret}")[0]

    def test_an_unknown_infrastructure_is_reported(self) -> None:
        assert "unknown infrastructure 'ghost'" in issues("${infrastructure.ghost.host}")[0]

    def test_an_unknown_infrastructure_field_is_reported(self) -> None:
        message = issues("${infrastructure.company-postgres.password}")[0]
        assert "unknown infrastructure field 'password'" in message

    def test_an_unknown_namespace_is_reported(self) -> None:
        assert "unknown interpolation namespace 'secrets'" in issues("${secrets.a.b}")[0]

    def test_a_malformed_reference_is_reported_with_the_accepted_forms(self) -> None:
        message = issues("${nonsense}")[0]
        assert "invalid interpolation" in message
        assert "${env.<VAR>}" in message

    def test_an_env_reference_needs_a_name(self) -> None:
        assert issues("${env.}") or issues("${env}")

    def test_a_project_field_is_checked(self) -> None:
        assert issues("${project.name}") == []
        assert "unknown project field 'secret'" in issues("${project.secret}")[0]

    def test_several_problems_in_one_value_are_all_reported(self) -> None:
        assert len(issues("${database.ghost.url}${secrets.a.b}")) == 2


class TestResolution:
    def test_a_database_url_is_substituted(self, context: InterpolationContext) -> None:
        assert (
            interpolate("${database.clients.url}", context)
            == "postgresql://devbox@127.0.0.1:5432/clients"
        )

    def test_parts_of_a_database_can_be_used_separately(
        self, context: InterpolationContext
    ) -> None:
        result = interpolate("${database.clients.host}:${database.clients.port}", context)
        assert result == "127.0.0.1:5432"

    def test_an_environment_variable_is_substituted(self, context: InterpolationContext) -> None:
        assert interpolate("Bearer ${env.CI_TOKEN}", context) == "Bearer abc123"

    def test_an_empty_environment_variable_is_still_a_value(
        self, context: InterpolationContext
    ) -> None:
        assert interpolate("[${env.EMPTY}]", context) == "[]"

    def test_an_unset_environment_variable_is_an_actionable_error(
        self, context: InterpolationContext
    ) -> None:
        with pytest.raises(ConfigError) as caught:
            interpolate("${env.MISSING}", context)
        assert "MISSING" in str(caught.value)
        assert caught.value.hint is not None
        assert "$env:MISSING" in caught.value.hint

    def test_project_values_are_available(self, context: InterpolationContext) -> None:
        assert interpolate("${project.name}", context) == "customer-portal"
        assert "projects" in interpolate("${project.root}", context)

    def test_a_literal_dollar_survives(self, context: InterpolationContext) -> None:
        assert interpolate("cost: $$5", context) == "cost: $5"

    def test_text_without_references_is_returned_unchanged(
        self, context: InterpolationContext
    ) -> None:
        assert interpolate("plain text", context) == "plain text"

    def test_substitution_is_not_recursive(self, context: InterpolationContext) -> None:
        """A substituted value containing ${...} is left alone, deliberately."""
        nested = InterpolationContext(environ={"A": "${env.B}", "B": "final"})
        assert interpolate("${env.A}", nested) == "${env.B}"

    def test_an_unknown_database_at_resolution_time_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="unknown database 'ghost'"):
            interpolate("${database.ghost.url}", InterpolationContext())

    def test_a_field_with_no_value_explains_why(self) -> None:
        context = InterpolationContext(databases={"clients": {"url": "x"}})
        with pytest.raises(ConfigError) as caught:
            interpolate("${database.clients.password}", context)
        assert "which has no value in this environment" in str(caught.value)
        assert caught.value.hint is not None

    def test_a_malformed_reference_at_resolution_time_names_the_path(
        self, context: InterpolationContext
    ) -> None:
        with pytest.raises(ConfigError, match=r"services\.a\.env\.X"):
            interpolate("${nonsense}", context, path="services.a.env.X")


class TestMappings:
    def test_a_whole_env_block_is_resolved(self, context: InterpolationContext) -> None:
        resolved = interpolate_mapping(
            {"DATABASE_URL": "${database.clients.url}", "PORT": "3000"}, context
        )
        assert resolved == {
            "DATABASE_URL": "postgresql://devbox@127.0.0.1:5432/clients",
            "PORT": "3000",
        }

    def test_the_failing_key_is_named(self, context: InterpolationContext) -> None:
        with pytest.raises(ConfigError, match=r"services\.a\.env\.BROKEN"):
            interpolate_mapping({"BROKEN": "${nonsense}"}, context, path="services.a.env")
