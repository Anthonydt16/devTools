"""Progress events.

The orchestrator does real work that takes seconds — downloading a runtime,
waiting for PostgreSQL to accept connections — and a user staring at a silent
terminal assumes it hung.  It therefore emits structured events as it goes, and
the CLI decides how to render them.

Events are *data*, never pre-formatted terminal output: the same stream feeds the
human renderer, the ``--quiet`` mode and the test assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Event", "EventCollector", "EventKind", "EventScope", "EventSink"]


class EventKind(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    FAILURE = "failure"
    WARNING = "warning"
    PROGRESS = "progress"


class EventScope(StrEnum):
    PROJECT = "project"
    RUNTIME = "runtime"
    INFRASTRUCTURE = "infrastructure"
    DATABASE = "database"
    SERVICE = "service"
    STATE = "state"


@dataclass(frozen=True, slots=True)
class Event:
    """One step of a long-running operation."""

    kind: EventKind
    scope: EventScope
    name: str
    message: str
    detail: str = ""

    def render(self) -> str:
        prefix = {
            EventKind.SUCCESS: "OK",
            EventKind.FAILURE: "FAIL",
            EventKind.WARNING: "WARN",
        }.get(self.kind, "..")
        return f"[{prefix}] {self.scope}/{self.name}: {self.message}"


EventSink = Callable[[Event], None]
"""Where events go.  ``lambda event: None`` is a perfectly good sink."""


@dataclass
class EventCollector:
    """An :data:`EventSink` that keeps everything, for assertions."""

    events: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def messages(self) -> list[str]:
        return [event.message for event in self.events]

    def for_scope(self, scope: EventScope) -> list[Event]:
        return [event for event in self.events if event.scope is scope]

    def failures(self) -> list[Event]:
        return [event for event in self.events if event.kind is EventKind.FAILURE]
