"""``devbox.lock``: parsing, generation, integrity, reproducibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.config import ProjectConfig, parse_config
from devbox.config.loader import parse_yaml_document
from devbox.errors import LockfileError, RegistryError
from devbox.host.platform_info import PLATFORM_LINUX_X64, PLATFORM_WINDOWS_X64, Platform
from devbox.lockfile import (
    LockedRuntime,
    Lockfile,
    dump_lockfile,
    parse_lockfile,
    read_lockfile,
    write_lockfile,
)
from devbox.runtime.registry import FilesystemRegistry
from devbox.runtime.resolver import resolve_lockfile, verify_lockfile_covers
from devbox.testing.fake_runtime import FakeRegistry

pytestmark = pytest.mark.unit

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

SAMPLE = f"""
lock_version: 1
runtimes:
  node:
    "18.19.0":
      platform: windows-x64
      sha256: "{DIGEST_A}"
      source: "registry/company/node/18.19.0"
    "24.6.0":
      platform: windows-x64
      sha256: "{DIGEST_B}"
      source: "registry/company/node/24.6.0"
"""


class TestReading:
    def test_reads_the_documented_shape(self) -> None:
        lock = parse_lockfile(SAMPLE)
        entry = lock.require("node", "18.19.0")
        assert entry.platform == "windows-x64"
        assert entry.sha256 == DIGEST_A
        assert entry.source == "registry/company/node/18.19.0"

    def test_missing_file_is_an_empty_lockfile(self, tmp_path: Path) -> None:
        assert len(read_lockfile(tmp_path / "devbox.lock")) == 0

    def test_empty_file_is_an_empty_lockfile(self) -> None:
        assert len(parse_lockfile("")) == 0

    def test_unsupported_lock_version(self) -> None:
        with pytest.raises(LockfileError, match="unsupported lock_version 99"):
            parse_lockfile("lock_version: 99\nruntimes: {}\n")

    def test_missing_sha256_is_refused(self) -> None:
        text = "lock_version: 1\nruntimes:\n  node:\n    '1.0':\n      platform: windows-x64\n"
        with pytest.raises(LockfileError, match="sha256 is required"):
            parse_lockfile(text)

    def test_malformed_sha256_is_refused(self) -> None:
        text = (
            "lock_version: 1\nruntimes:\n  node:\n    '1.0':\n"
            "      platform: windows-x64\n      sha256: nope\n"
        )
        with pytest.raises(LockfileError, match="invalid sha256"):
            parse_lockfile(text)

    def test_missing_platform_is_refused(self) -> None:
        text = f"lock_version: 1\nruntimes:\n  node:\n    '1.0':\n      sha256: '{DIGEST_A}'\n"
        with pytest.raises(LockfileError, match="platform is required"):
            parse_lockfile(text)

    def test_wrong_structure_is_refused(self) -> None:
        with pytest.raises(LockfileError, match="must be a mapping"):
            parse_lockfile("lock_version: 1\nruntimes:\n  node: [1, 2]\n")

    def test_invalid_yaml_is_refused_with_a_hint(self) -> None:
        with pytest.raises(LockfileError) as caught:
            parse_lockfile("runtimes: [\n")
        assert caught.value.hint is not None
        assert "devbox install" in caught.value.hint


class TestWriting:
    def test_round_trip_is_lossless(self, tmp_path: Path) -> None:
        original = parse_lockfile(SAMPLE)
        path = tmp_path / "devbox.lock"
        write_lockfile(path, original)
        assert read_lockfile(path).to_document() == original.to_document()

    def test_output_is_byte_stable(self) -> None:
        lock = parse_lockfile(SAMPLE)
        assert dump_lockfile(lock) == dump_lockfile(parse_lockfile(dump_lockfile(lock)))

    def test_ambiguous_versions_survive_a_round_trip(self) -> None:
        """'8.4' and '24.60' must not come back as floats."""
        lock = Lockfile()
        for version in ("8.4", "24.60", "17", "1.0.0"):
            lock = lock.with_entry(LockedRuntime("db", version, "windows-x64", DIGEST_A, "src"))
        reloaded = parse_lockfile(dump_lockfile(lock))
        assert sorted(e.version for e in reloaded.entries()) == ["1.0.0", "17", "24.60", "8.4"]

    def test_writing_is_atomic_and_leaves_no_temporary(self, tmp_path: Path) -> None:
        path = tmp_path / "devbox.lock"
        write_lockfile(path, parse_lockfile(SAMPLE))
        assert [p.name for p in tmp_path.iterdir()] == ["devbox.lock"]

    def test_lf_line_endings_on_every_platform(self, tmp_path: Path) -> None:
        path = tmp_path / "devbox.lock"
        write_lockfile(path, parse_lockfile(SAMPLE))
        assert b"\r\n" not in path.read_bytes()


class TestDigest:
    def test_digest_is_stable_across_formatting(self) -> None:
        compact = parse_lockfile(SAMPLE)
        expanded = parse_lockfile(dump_lockfile(compact))
        assert compact.digest() == expanded.digest()

    def test_digest_changes_when_an_artifact_changes(self) -> None:
        base = parse_lockfile(SAMPLE)
        changed = base.with_entry(
            LockedRuntime("node", "18.19.0", "windows-x64", DIGEST_B, "elsewhere")
        )
        assert base.digest() != changed.digest()

    def test_digest_ignores_entry_insertion_order(self) -> None:
        one = (
            Lockfile()
            .with_entry(LockedRuntime("node", "1", "windows-x64", DIGEST_A, "s"))
            .with_entry(LockedRuntime("node", "2", "windows-x64", DIGEST_B, "s"))
        )
        other = (
            Lockfile()
            .with_entry(LockedRuntime("node", "2", "windows-x64", DIGEST_B, "s"))
            .with_entry(LockedRuntime("node", "1", "windows-x64", DIGEST_A, "s"))
        )
        assert one.digest() == other.digest()


class TestResolution:
    def config(self, *versions: str) -> ProjectConfig:
        services = "".join(
            f'  s{i}:\n    command: x\n    runtime: {{ node: "{v}" }}\n'
            for i, v in enumerate(versions)
        )
        document = parse_yaml_document(f"version: 1\nservices:\n{services}", source="devbox.yml")
        return parse_config(document, source="devbox.yml")

    def test_resolution_writes_every_required_runtime(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        result = resolve_lockfile(
            self.config("18.19.0", "24.6.0"),
            registry=registry,
            platform=host_platform,
        )
        assert len(result.lockfile) == 2
        assert len(result.added) == 2
        assert result.changed

    def test_resolution_records_the_registry_digest(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        result = resolve_lockfile(
            self.config("18.19.0"),
            registry=registry,
            platform=host_platform,
        )
        entry = result.lockfile.require("node", "18.19.0")
        assert entry.sha256 == fake_registry.artifact("node", "18.19.0").sha256

    def test_resolution_is_reproducible(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        config = self.config("18.19.0", "24.6.0")
        first = resolve_lockfile(config, registry=registry, platform=host_platform)
        second = resolve_lockfile(
            config,
            registry=registry,
            platform=host_platform,
            existing=first.lockfile,
        )
        assert dump_lockfile(first.lockfile) == dump_lockfile(second.lockfile)
        assert not second.changed
        assert len(second.kept) == 2

    def test_a_removed_runtime_is_pruned_and_reported(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        full = resolve_lockfile(
            self.config("18.19.0", "24.6.0"),
            registry=registry,
            platform=host_platform,
        )
        trimmed = resolve_lockfile(
            self.config("18.19.0"),
            registry=registry,
            platform=host_platform,
            existing=full.lockfile,
        )
        assert len(trimmed.lockfile) == 1
        assert [e.version for e in trimmed.removed] == ["24.6.0"]

    def test_unpublished_version_is_reported_with_the_alternatives(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        with pytest.raises(RegistryError) as caught:
            resolve_lockfile(
                self.config("99.0.0"),
                registry=registry,
                platform=host_platform,
            )
        assert "does not publish node 99.0.0" in str(caught.value)
        assert caught.value.hint is not None
        assert "18.19.0" in caught.value.hint

    def test_unsupported_platform_is_refused(
        self, fake_registry: FakeRegistry, host_platform: Platform
    ) -> None:
        registry = FilesystemRegistry(fake_registry.root)
        other = (
            PLATFORM_WINDOWS_X64 if host_platform != PLATFORM_WINDOWS_X64 else PLATFORM_LINUX_X64
        )
        with pytest.raises(RegistryError) as caught:
            resolve_lockfile(self.config("18.19.0"), registry=registry, platform=other)
        assert f"not published for platform '{other.tag}'" in str(caught.value)


class TestCoverageVerification:
    def test_a_covered_project_passes(self) -> None:
        from devbox.config.models import RuntimeRequirement

        lock = parse_lockfile(SAMPLE)
        verify_lockfile_covers(
            lock,
            [RuntimeRequirement("node", "18.19.0")],
            platform=PLATFORM_WINDOWS_X64,
        )

    def test_a_missing_runtime_is_reported(self) -> None:
        from devbox.config.models import RuntimeRequirement

        with pytest.raises(LockfileError) as caught:
            verify_lockfile_covers(
                parse_lockfile(SAMPLE),
                [RuntimeRequirement("node", "20.0.0")],
                platform=PLATFORM_WINDOWS_X64,
            )
        assert "missing: node 20.0.0" in str(caught.value)
        assert caught.value.hint is not None
        assert "devbox install" in caught.value.hint

    def test_a_lockfile_for_another_platform_is_reported(self) -> None:
        from devbox.config.models import RuntimeRequirement

        with pytest.raises(LockfileError) as caught:
            verify_lockfile_covers(
                parse_lockfile(SAMPLE),
                [RuntimeRequirement("node", "18.19.0")],
                platform=PLATFORM_LINUX_X64,
            )
        assert "resolved for another platform" in str(caught.value)
