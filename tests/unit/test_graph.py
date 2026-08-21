"""Dependency graph: ordering, cycles, unknown nodes, subsets."""

from __future__ import annotations

import pytest

from devbox.config import parse_config
from devbox.config.loader import parse_yaml_document
from devbox.config.models import ProjectConfig
from devbox.errors import DependencyGraphError
from devbox.graph import DependencyGraph

pytestmark = pytest.mark.unit


def graph_of(text: str) -> DependencyGraph:
    config: ProjectConfig = parse_config(parse_yaml_document(text, source="devbox.yml"))
    return DependencyGraph(config.all_dependencies())


class TestOrdering:
    def test_topological_order_respects_dependencies(self) -> None:
        graph = DependencyGraph(
            {
                "pg": [],
                "my": [],
                "clients": ["pg"],
                "backend": ["pg", "my", "clients"],
                "frontend": ["backend"],
            }
        )
        order = graph.startup_plan().order
        assert order.index("pg") < order.index("clients") < order.index("backend")
        assert order.index("backend") < order.index("frontend")

    def test_order_is_deterministic(self) -> None:
        edges = {"c": [], "a": [], "b": [], "d": ["a", "b", "c"]}
        first = DependencyGraph(edges).startup_plan().order
        second = DependencyGraph(dict(reversed(list(edges.items())))).startup_plan().order
        assert first == second == ("a", "b", "c", "d")

    def test_levels_group_what_can_start_together(self) -> None:
        plan = DependencyGraph({"a": [], "b": [], "c": ["a", "b"]}).startup_plan()
        assert plan.levels == (("a", "b"), ("c",))

    def test_shutdown_is_the_exact_reverse(self) -> None:
        plan = DependencyGraph({"a": [], "b": ["a"], "c": ["b"]}).startup_plan()
        assert plan.shutdown_order == tuple(reversed(plan.order))

    def test_isolated_nodes_are_included(self) -> None:
        plan = DependencyGraph({"a": [], "b": [], "c": []}).startup_plan()
        assert set(plan.order) == {"a", "b", "c"}


class TestSubsets:
    def test_a_target_pulls_in_its_dependencies(self) -> None:
        graph = DependencyGraph({"pg": [], "my": [], "backend": ["pg"], "frontend": ["backend"]})
        assert graph.startup_plan(["frontend"]).order == ("pg", "backend", "frontend")

    def test_unrelated_nodes_are_excluded(self) -> None:
        graph = DependencyGraph({"pg": [], "my": [], "backend": ["pg"]})
        assert "my" not in graph.startup_plan(["backend"]).order

    def test_unknown_target_is_rejected(self) -> None:
        graph = DependencyGraph({"a": []})
        with pytest.raises(DependencyGraphError, match="unknown target: ghost"):
            graph.startup_plan(["ghost"])

    def test_dependents_are_found_transitively(self) -> None:
        graph = DependencyGraph({"pg": [], "backend": ["pg"], "frontend": ["backend"]})
        assert graph.dependents_of("pg") == frozenset({"backend", "frontend"})


class TestCycles:
    def test_simple_cycle(self) -> None:
        with pytest.raises(DependencyGraphError) as caught:
            DependencyGraph({"a": ["b"], "b": ["a"]}).validate()
        assert "dependency cycle detected: a -> b -> a" in str(caught.value)

    def test_three_node_cycle_names_every_member(self) -> None:
        with pytest.raises(DependencyGraphError) as caught:
            DependencyGraph({"a": ["b"], "b": ["c"], "c": ["a"]}).validate()
        message = str(caught.value)
        assert "a -> b -> c -> a" in message

    def test_self_cycle(self) -> None:
        with pytest.raises(DependencyGraphError, match="a -> a"):
            DependencyGraph({"a": ["a"]}).validate()

    def test_complex_cycle_behind_a_valid_prefix(self) -> None:
        edges = {"root": ["a"], "a": ["b"], "b": ["c"], "c": ["d"], "d": ["b"]}
        with pytest.raises(DependencyGraphError) as caught:
            DependencyGraph(edges).validate()
        assert "cycle" in str(caught.value)

    def test_a_deep_chain_does_not_hit_the_recursion_limit(self) -> None:
        """5000 chained services must be an ordering problem, not a RecursionError."""
        depth = 5000
        edges = {f"s{i}": ([f"s{i - 1}"] if i else []) for i in range(depth)}
        plan = DependencyGraph(edges).startup_plan()
        assert plan.order[0] == "s0"
        assert plan.order[-1] == f"s{depth - 1}"

    def test_acyclic_graph_reports_no_cycle(self) -> None:
        assert DependencyGraph({"a": [], "b": ["a"]}).find_cycle() is None


class TestUnknownNodes:
    def test_unknown_dependency_is_reported_with_the_alternatives(self) -> None:
        with pytest.raises(DependencyGraphError) as caught:
            DependencyGraph({"a": ["ghost"], "b": []}).validate()
        message = str(caught.value)
        assert "unknown dependency 'ghost'" in message
        assert "known: b" in message


class TestFromConfiguration:
    def test_databases_implicitly_depend_on_their_server(self) -> None:
        graph = graph_of(
            "version: 1\n"
            "infrastructure:\n  pg: { provider: postgres }\n"
            "databases:\n  clients: { provider: postgres, infrastructure: pg }\n"
            "services:\n  api:\n    command: x\n    depends_on: [clients]\n"
        )
        order = graph.startup_plan().order
        assert order == ("pg", "clients", "api")

    def test_the_reference_project_orders_infrastructure_first(self) -> None:
        graph = graph_of(
            "version: 1\n"
            "infrastructure:\n"
            "  company-postgres: { provider: postgres }\n"
            "  company-mysql: { provider: mysql }\n"
            "services:\n"
            "  backend:\n    command: x\n    depends_on: [company-postgres, company-mysql]\n"
            "  frontend:\n    command: y\n    depends_on: [backend]\n"
        )
        order = graph.startup_plan().order
        assert order[-2:] == ("backend", "frontend")
        assert set(order[:2]) == {"company-postgres", "company-mysql"}
