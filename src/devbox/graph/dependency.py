"""Dependency graph: ordering, cycle detection, reachability.

``depends_on`` forms a directed acyclic graph over services, databases and
infrastructure.  Devbox starts nodes in topological order and stops them in the
exact reverse of the order it actually started, so a service is never left
talking to a database that has already gone away.

Ordering is **deterministic**: within one topological "level" nodes are sorted by
name.  Two runs of ``devbox up`` on the same file therefore start services in the
same order, which makes failures reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from devbox.errors import DependencyGraphError, ValidationIssue

__all__ = ["DependencyGraph", "StartupPlan"]


@dataclass(frozen=True, slots=True)
class StartupPlan:
    """The order to start in, grouped into levels that could run concurrently."""

    order: tuple[str, ...]
    levels: tuple[tuple[str, ...], ...]

    @property
    def shutdown_order(self) -> tuple[str, ...]:
        return tuple(reversed(self.order))


class DependencyGraph:
    """A validated ``node -> dependencies`` graph."""

    __slots__ = ("_edges",)

    def __init__(self, edges: Mapping[str, Sequence[str]]) -> None:
        self._edges: dict[str, tuple[str, ...]] = {
            node: tuple(dependencies) for node, dependencies in edges.items()
        }

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._edges)

    def dependencies_of(self, node: str) -> tuple[str, ...]:
        return self._edges.get(node, ())

    # -- validation -----------------------------------------------------------
    def validate(self) -> None:
        """Raise on an unknown dependency or a cycle, naming exactly what is wrong."""
        issues = list(self._unknown_dependency_issues())
        if issues:
            raise DependencyGraphError(
                "invalid dependencies",
                issues=issues,
                hint="Every name in depends_on must be a declared service, database "
                "or infrastructure entry.",
            )
        cycle = self.find_cycle()
        if cycle is not None:
            rendered = " -> ".join((*cycle, cycle[0]))
            msg = f"dependency cycle detected: {rendered}"
            raise DependencyGraphError(
                msg,
                issues=[ValidationIssue(path=f"services.{cycle[0]}.depends_on", message=rendered)],
                hint="Break the cycle by removing one of these depends_on entries.",
            )

    def _unknown_dependency_issues(self) -> Iterable[ValidationIssue]:
        known = self.nodes
        for node in sorted(self._edges):
            for dependency in self._edges[node]:
                if dependency not in known:
                    others = ", ".join(sorted(known - {node})) or "<none>"
                    yield ValidationIssue(
                        path=f"{node}.depends_on",
                        message=f"unknown dependency {dependency!r} (known: {others})",
                    )

    def find_cycle(self) -> tuple[str, ...] | None:
        """Return one cycle as an ordered tuple, or ``None`` if the graph is acyclic.

        Iterative depth-first search with an explicit stack: a deeply nested
        ``depends_on`` chain must not blow the Python recursion limit.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        colour: dict[str, int] = dict.fromkeys(self._edges, WHITE)
        parent: dict[str, str | None] = {}

        for root in sorted(self._edges):
            if colour[root] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(root, 0)]
            parent[root] = None
            colour[root] = GREY
            while stack:
                node, index = stack.pop()
                neighbours = sorted(d for d in self._edges.get(node, ()) if d in colour)
                if index >= len(neighbours):
                    colour[node] = BLACK
                    continue
                stack.append((node, index + 1))
                neighbour = neighbours[index]
                if colour[neighbour] == GREY:
                    return _extract_cycle(parent, node, neighbour)
                if colour[neighbour] == WHITE:
                    colour[neighbour] = GREY
                    parent[neighbour] = node
                    stack.append((neighbour, 0))
        return None

    # -- ordering -------------------------------------------------------------
    def startup_plan(self, targets: Iterable[str] | None = None) -> StartupPlan:
        """Topological order, restricted to *targets* and everything they need."""
        self.validate()
        selected = self._closure(targets) if targets is not None else set(self._edges)

        remaining = {node: {d for d in self._edges[node] if d in selected} for node in selected}
        order: list[str] = []
        levels: list[tuple[str, ...]] = []
        while remaining:
            ready = sorted(node for node, deps in remaining.items() if not deps)
            if not ready:  # pragma: no cover - validate() already rejected cycles
                cycle = self.find_cycle() or tuple(sorted(remaining))
                msg = f"dependency cycle detected: {' -> '.join(cycle)}"
                raise DependencyGraphError(msg)
            levels.append(tuple(ready))
            order.extend(ready)
            for node in ready:
                del remaining[node]
            for deps in remaining.values():
                deps.difference_update(ready)
        return StartupPlan(order=tuple(order), levels=tuple(levels))

    def _closure(self, targets: Iterable[str]) -> set[str]:
        """*targets* plus every node they transitively depend on."""
        wanted = list(targets)
        unknown = [name for name in wanted if name not in self._edges]
        if unknown:
            known = ", ".join(sorted(self._edges)) or "<none>"
            msg = f"unknown target{'s' if len(unknown) > 1 else ''}: {', '.join(sorted(unknown))}"
            raise DependencyGraphError(msg, hint=f"Known names: {known}")
        seen: set[str] = set()
        stack = list(wanted)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(d for d in self._edges.get(node, ()) if d not in seen)
        return seen

    def dependents_of(self, node: str) -> frozenset[str]:
        """Every node that transitively depends on *node*."""
        reverse: dict[str, list[str]] = {name: [] for name in self._edges}
        for name, deps in self._edges.items():
            for dependency in deps:
                if dependency in reverse:
                    reverse[dependency].append(name)
        seen: set[str] = set()
        stack = list(reverse.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(reverse.get(current, ()))
        return frozenset(seen)


def _extract_cycle(
    parent: Mapping[str, str | None],
    node: str,
    target: str,
) -> tuple[str, ...]:
    """Walk parents from *node* back to *target* to render the cycle in order."""
    path = [node]
    current: str | None = node
    while current is not None and current != target:
        current = parent.get(current)
        if current is None:
            break
        path.append(current)
    return tuple(reversed(path))
