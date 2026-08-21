"""State persistence: round-trips, atomicity, corruption recovery, ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devbox.host.paths import DevboxPaths
from devbox.process.supervisor import ProcessRecord
from devbox.state import InfraOwnership, InfraRecord, ProjectState, StateStore, project_id_for
from devbox.state.models import InfraState

pytestmark = pytest.mark.unit


@pytest.fixture
def store(devbox_home: DevboxPaths) -> StateStore:
    return StateStore(paths=devbox_home, clock=lambda: 1_700_000_000.0).ensure()


def a_record(service: str = "backend", pid: int = 4242) -> ProcessRecord:
    return ProcessRecord(
        project_id="demo-abc",
        service_id=service,
        pid=pid,
        command=("node", "server.js"),
        working_dir="C:\\projects\\demo\\backend",
        started_at=1_700_000_000.0,
        creation_time=98765.25,
        stdout_log="C:\\projects\\demo\\.devbox\\logs\\backend.out.log",
        stderr_log="C:\\projects\\demo\\.devbox\\logs\\backend.err.log",
        supervisor="windows",
        job_name="Local\\Devbox-abc-backend",
        process_group=4242,
    )


class TestProjectIdentity:
    def test_the_same_directory_always_gets_the_same_id(self, tmp_path: Path) -> None:
        assert project_id_for(tmp_path) == project_id_for(tmp_path)

    def test_two_clones_are_two_projects(self, tmp_path: Path) -> None:
        first = tmp_path / "a" / "demo"
        second = tmp_path / "b" / "demo"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        assert project_id_for(first) != project_id_for(second)

    def test_the_id_is_a_safe_filename(self, tmp_path: Path) -> None:
        directory = tmp_path / "My Project (café)"
        directory.mkdir()
        identifier = project_id_for(directory)
        assert all(char.isalnum() or char in "-" for char in identifier)


class TestProjectState:
    def test_round_trip(self, store: StateStore) -> None:
        state = ProjectState(project_id="demo-abc", project_name="demo").with_process(a_record())
        store.save_project(state)
        loaded = store.load_project("demo-abc")
        assert loaded.record("backend") == a_record()

    def test_missing_state_is_empty_not_an_error(self, store: StateStore) -> None:
        assert store.load_project("never-seen").processes == {}

    def test_creation_time_survives_the_round_trip(self, store: StateStore) -> None:
        """Without it, ownership cannot be proven after a restart."""
        store.save_project(ProjectState(project_id="demo-abc").with_process(a_record()))
        loaded = store.load_project("demo-abc")
        assert loaded.record("backend") is not None
        assert loaded.record("backend").creation_time == 98765.25  # type: ignore[union-attr]

    def test_forgetting_a_process_persists(self, store: StateStore) -> None:
        state = ProjectState(project_id="demo-abc").with_process(a_record())
        store.save_project(state)
        store.forget_process(state, "backend")
        assert store.load_project("demo-abc").processes == {}

    def test_writing_leaves_no_temporary_file(self, store: StateStore) -> None:
        store.save_project(ProjectState(project_id="demo-abc").with_process(a_record()))
        assert list(store.projects_dir.glob("*.tmp")) == []

    def test_projects_are_listed(self, store: StateStore) -> None:
        store.save_project(ProjectState(project_id="one"))
        store.save_project(ProjectState(project_id="two"))
        assert sorted(s.project_id for s in store.all_projects()) == ["one", "two"]


class TestCorruption:
    def test_invalid_json_is_quarantined_and_reported(self, store: StateStore) -> None:
        path = store.project_file("demo-abc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        state = store.load_project("demo-abc")

        assert state.processes == {}
        warnings = store.take_warnings()
        assert len(warnings) == 1
        assert "not valid JSON" in warnings[0].message
        assert not path.exists(), "the damaged file must be moved aside"
        assert list(store.projects_dir.glob("*.corrupt-*"))

    def test_an_empty_state_file_is_quarantined(self, store: StateStore) -> None:
        path = store.project_file("demo-abc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert store.load_project("demo-abc").processes == {}
        assert "empty" in store.take_warnings()[0].message

    def test_a_future_state_version_is_quarantined(self, store: StateStore) -> None:
        path = store.project_file("demo-abc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state_version": 999}), encoding="utf-8")
        assert store.load_project("demo-abc").processes == {}
        assert "unsupported version" in store.take_warnings()[0].message

    def test_a_malformed_process_entry_is_quarantined(self, store: StateStore) -> None:
        path = store.project_file("demo-abc")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"state_version": 1, "processes": {"backend": {"pid": "not-a-pid"}}}),
            encoding="utf-8",
        )
        assert store.load_project("demo-abc").processes == {}
        assert "not usable" in store.take_warnings()[0].message

    def test_interrupted_writes_are_cleaned_up(self, store: StateStore) -> None:
        leftover = store.projects_dir / "demo.json.tmp"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_text("half written", encoding="utf-8")
        assert store.cleanup_temporary_files() == 1
        assert not leftover.exists()

    def test_a_state_id_cannot_escape_the_state_directory(self, store: StateStore) -> None:
        from devbox.errors import SecurityError

        with pytest.raises(SecurityError):
            store.project_file("../../evil")


class TestInfrastructureOwnership:
    """Non-regression #2 lives here: shared infrastructure outlives a project."""

    def test_round_trip(self, store: StateStore) -> None:
        record = InfraRecord(
            instance="company-postgres",
            provider="postgres",
            pid=999,
            port=5432,
            ownership=InfraOwnership.GLOBAL,
            claims=("demo-abc",),
        )
        store.save_infra(InfraState().with_instance(record))
        assert store.load_infra().get("company-postgres") == record

    def test_a_globally_started_instance_is_never_stopped_by_a_project(self) -> None:
        record = InfraRecord(
            instance="company-postgres",
            provider="postgres",
            pid=1,
            port=5432,
            ownership=InfraOwnership.GLOBAL,
            claims=("demo-abc",),
        )
        allowed, reason = record.may_be_stopped_by_project("demo-abc")
        assert not allowed
        assert "devbox infra down" in reason

    def test_an_instance_shared_by_two_projects_survives_one_leaving(self) -> None:
        record = (
            InfraRecord(instance="company-postgres", provider="postgres", pid=1, port=5432)
            .with_claim("app-a")
            .with_claim("app-b")
        )
        allowed, reason = record.may_be_stopped_by_project("app-a")
        assert not allowed
        assert "still used by 1 other project" in reason

    def test_the_last_project_may_stop_what_it_started(self) -> None:
        record = InfraRecord(
            instance="company-postgres", provider="postgres", pid=1, port=5432
        ).with_claim("app-a")
        allowed, reason = record.may_be_stopped_by_project("app-a")
        assert allowed
        assert reason == ""

    def test_claims_are_idempotent_and_sorted(self) -> None:
        record = InfraRecord(instance="pg", provider="postgres", pid=1, port=5432)
        twice = record.with_claim("b").with_claim("a").with_claim("b")
        assert twice.claims == ("a", "b")

    def test_releasing_a_claim_removes_only_that_one(self) -> None:
        record = InfraRecord(instance="pg", provider="postgres", pid=1, port=5432)
        both = record.with_claim("a").with_claim("b")
        assert both.without_claim("a").claims == ("b",)
