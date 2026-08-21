"""The runtime install pipeline, end to end, against a real filesystem registry.

Real archives, real SHA256 digests, real extraction, real executables — no
network and nothing installed on the machine.  What is *not* covered here is
Windows-specific behaviour of the installed binaries; that is in
``tests/windows/``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from devbox.config.models import RuntimeRequirement
from devbox.errors import IntegrityError, RegistryError, RuntimeInstallError
from devbox.host.paths import DevboxPaths
from devbox.host.platform_info import PLATFORM_WINDOWS_X64, Platform
from devbox.lockfile import LockedRuntime, Lockfile
from devbox.runtime import (
    Downloader,
    FilesystemRegistry,
    HttpRegistry,
    RuntimeManager,
    resolve_lockfile,
)
from devbox.testing.fake_runtime import FakeRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def manager(devbox_home: DevboxPaths, host_platform: Platform) -> RuntimeManager:
    return RuntimeManager(devbox_home, platform=host_platform)


@pytest.fixture
def registry(fake_registry: FakeRegistry) -> FilesystemRegistry:
    return FilesystemRegistry(fake_registry.root)


def locked_for(
    fake_registry: FakeRegistry, name: str, version: str, platform: Platform
) -> LockedRuntime:
    """The lockfile entry a resolution against *fake_registry* would produce."""
    artifact = fake_registry.artifact(name, version)
    return LockedRuntime(
        name=name,
        version=version,
        platform=platform.tag,
        sha256=artifact.sha256,
        source=str(fake_registry.root),
        url=artifact.relative_url,
        size=artifact.size,
        archive=f"{name}-{version}-{platform.tag}.zip",
    )


class TestInstall:
    def test_a_runtime_is_installed_and_usable(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        outcome = manager.install_locked(
            locked_for(fake_registry, "node", "18.19.0", host_platform), registry
        )
        assert outcome.action == "installed"
        assert manager.is_installed("node", "18.19.0")

        ok, reason = manager.verify("node", "18.19.0")
        assert ok, reason

    def test_the_installed_runtime_actually_runs(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        """Not "a file exists" — the binary runs and reports its version."""
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)
        root = manager.install_root("node", "18.19.0")
        result = subprocess.run(
            [sys.executable, str(root / "runtime_main.py"), "--version"],
            capture_output=True,
            text=True,
            env={"DEVBOX_FAKE_RUNTIME_VERSION": "18.19.0", "PATH": "/usr/bin:/bin"},
            check=False,
            timeout=30,
        )
        assert result.stdout.strip() == "v18.19.0"

    def test_installing_twice_is_a_no_op(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)
        second = manager.install_locked(locked, registry)
        assert second.action == "already-present"
        assert not second.changed

    def test_the_second_install_of_a_different_version_uses_the_cache(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        first = manager.install_locked(locked, registry)
        assert not first.from_cache

        manager.remove("node", "18.19.0")
        second = manager.install_locked(locked, registry)
        assert second.from_cache, "the verified artifact should have been reused"

    def test_two_versions_coexist(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        for version in ("18.19.0", "24.6.0"):
            locked = locked_for(fake_registry, "node", version, host_platform)
            manager.install_locked(locked, registry)
        installed = {(r.name, r.version) for r in manager.list_installed()}
        assert installed == {("node", "18.19.0"), ("node", "24.6.0")}

    def test_a_completion_marker_is_written(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)
        marker = manager.install_root("node", "18.19.0") / ".devbox-runtime.json"
        assert json.loads(marker.read_text())["sha256"] == locked.sha256

    def test_nothing_is_written_outside_the_devbox_home(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
        tmp_path: Path,
    ) -> None:
        """Zero-privilege guarantee: everything lives under %LOCALAPPDATA%\\Devbox."""
        before = {p for p in tmp_path.rglob("*") if devbox_home.root not in p.parents}
        manager = RuntimeManager(devbox_home, platform=host_platform)
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)
        after = {p for p in tmp_path.rglob("*") if devbox_home.root not in p.parents}
        assert after == before


class TestIntegrity:
    """Non-regression #4: a bad digest refuses the install."""

    def test_a_sha256_mismatch_refuses_the_install(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        good = locked_for(fake_registry, "node", "18.19.0", host_platform)
        tampered = dataclasses.replace(good, sha256="b" * 64)
        with pytest.raises(IntegrityError) as caught:
            manager.install_locked(tampered, registry)

        assert "b" * 64 in str(caught.value)
        assert not manager.is_installed("node", "18.19.0")
        assert not manager.install_root("node", "18.19.0").exists(), (
            "nothing may be extracted from an unverified artifact"
        )

    def test_a_refused_artifact_is_not_left_in_the_cache(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        good = locked_for(fake_registry, "node", "18.19.0", host_platform)
        tampered = dataclasses.replace(good, sha256="c" * 64)
        with pytest.raises(IntegrityError):
            manager.install_locked(tampered, registry)
        assert Downloader(devbox_home).cache_entries() == ()
        assert list(devbox_home.downloads.glob("*.part")) == []

    def test_a_registry_that_changed_its_digest_is_refused(
        self,
        manager: RuntimeManager,
        registry_factory: Callable[..., FakeRegistry],
        host_platform: Platform,
    ) -> None:
        """The lockfile wins: a republished artifact is an error, not an upgrade."""
        lying = registry_factory(
            [("node", "18.19.0")], platform=host_platform.tag, corrupt=[("node", "18.19.0")]
        )
        locked = LockedRuntime(
            name="node",
            version="18.19.0",
            platform=host_platform.tag,
            sha256=lying.artifact("node", "18.19.0").sha256,
            source="registry",
        )
        with pytest.raises(IntegrityError) as caught:
            manager.install_locked(locked, FilesystemRegistry(lying.root))
        assert "devbox.lock pins" in str(caught.value)

    def test_a_corrupt_cache_entry_is_replaced_rather_than_trusted(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)

        cached = Downloader(devbox_home).cache_entries()[0]
        cached.write_bytes(b"this is not the artifact you are looking for")

        manager.remove("node", "18.19.0")
        outcome = manager.install_locked(locked, registry)
        assert not outcome.from_cache, "a cache entry that fails verification must be refetched"
        assert manager.is_installed("node", "18.19.0")


class TestPlatforms:
    def test_a_lockfile_for_another_platform_is_refused(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        other = "linux-x64" if host_platform.tag != "linux-x64" else "windows-x64"
        locked = LockedRuntime(
            name="node", version="18.19.0", platform=other, sha256="a" * 64, source="s"
        )
        with pytest.raises(RuntimeInstallError) as caught:
            manager.install_locked(locked, registry)
        assert "is locked for platform" in str(caught.value)
        assert caught.value.hint is not None

    def test_an_unpublished_platform_is_reported_with_alternatives(
        self, devbox_home: DevboxPaths, registry: FilesystemRegistry, host_platform: Platform
    ) -> None:
        unpublished = (
            PLATFORM_WINDOWS_X64 if host_platform.tag != "windows-x64" else Platform("linux", "x64")
        )
        manager = RuntimeManager(devbox_home, platform=unpublished)
        locked = LockedRuntime(
            name="node",
            version="18.19.0",
            platform=unpublished.tag,
            sha256="a" * 64,
            source="s",
        )
        with pytest.raises(RegistryError, match="not published for platform"):
            manager.install_locked(locked, registry)


class TestResumeAndCleanup:
    def test_an_interrupted_download_is_resumed(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        downloader = Downloader(devbox_home)
        artifact = fake_registry.artifact("node", "18.19.0")
        manifest = registry.resolve("node", "18.19.0", host_platform)

        # Simulate a download that stopped halfway.
        devbox_home.downloads.mkdir(parents=True, exist_ok=True)
        partial = downloader.partial_path(manifest)
        partial.write_bytes(artifact.path.read_bytes()[: artifact.size // 2])

        result = downloader.fetch(manifest, registry)

        assert result.resumed, "the partial download should have been continued"
        assert result.sha256 == artifact.sha256
        assert not partial.exists()

    def test_a_resumed_download_that_does_not_verify_is_refused(
        self,
        devbox_home: DevboxPaths,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        """A resumed file is only trustworthy if the *whole* digest matches."""
        downloader = Downloader(devbox_home)
        manifest = registry.resolve("node", "18.19.0", host_platform)
        devbox_home.downloads.mkdir(parents=True, exist_ok=True)
        downloader.partial_path(manifest).write_bytes(b"garbage that is not the real prefix")

        with pytest.raises(IntegrityError):
            downloader.fetch(manifest, registry)
        assert not downloader.partial_path(manifest).exists()

    def test_partial_downloads_can_be_cleaned_up(
        self, devbox_home: DevboxPaths, registry: FilesystemRegistry, host_platform: Platform
    ) -> None:
        downloader = Downloader(devbox_home)
        devbox_home.downloads.mkdir(parents=True, exist_ok=True)
        manifest = registry.resolve("node", "18.19.0", host_platform)
        downloader.partial_path(manifest).write_bytes(b"half")
        assert downloader.clean_partials() == 1
        assert list(devbox_home.downloads.glob("*.part")) == []

    def test_an_incomplete_install_is_detected_and_pruned(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        locked = locked_for(fake_registry, "node", "18.19.0", host_platform)
        manager.install_locked(locked, registry)
        (manager.install_root("node", "18.19.0") / ".devbox-runtime.json").unlink()

        ok, reason = manager.verify("node", "18.19.0")
        assert not ok
        assert "incomplete" in reason
        assert manager.prune_incomplete() == ("node 18.19.0",)
        assert not manager.install_root("node", "18.19.0").exists()

    def test_removing_a_runtime_that_is_not_installed_is_false_not_an_error(
        self, manager: RuntimeManager
    ) -> None:
        assert manager.remove("node", "99.0.0") is False


class TestHttpRegistry:
    """The same pipeline over HTTP, served from loopback by a fixture."""

    def test_install_over_http(
        self,
        devbox_home: DevboxPaths,
        http_registry_server: str,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        manager = RuntimeManager(devbox_home, platform=host_platform)
        registry = HttpRegistry(http_registry_server)
        outcome = manager.install_locked(
            locked_for(fake_registry, "node", "24.6.0", host_platform), registry
        )
        assert outcome.action == "installed"
        assert manager.is_installed("node", "24.6.0")

    def test_an_unreachable_registry_is_reported_with_a_hint(
        self, devbox_home: DevboxPaths, host_platform: Platform, free_port: Callable[[], int]
    ) -> None:
        registry = HttpRegistry(f"http://127.0.0.1:{free_port()}")
        with pytest.raises(RegistryError) as caught:
            registry.resolve("node", "18.19.0", host_platform)
        assert "unreachable" in str(caught.value)
        assert caught.value.hint is not None

    def test_plain_http_to_a_remote_host_is_refused(self) -> None:
        """Runtime artifacts are executable code: https or loopback only."""
        with pytest.raises(RegistryError) as caught:
            HttpRegistry("http://registry.example.com")
        assert "refusing to use registry URL" in str(caught.value)

    def test_https_is_accepted(self) -> None:
        assert HttpRegistry("https://registry.example.com").location


class TestEnsureInstalled:
    def test_everything_a_project_needs_is_installed_in_one_call(
        self,
        manager: RuntimeManager,
        registry: FilesystemRegistry,
        fake_registry: FakeRegistry,
        host_platform: Platform,
    ) -> None:
        from devbox.config import parse_config
        from devbox.config.loader import parse_yaml_document

        config = parse_config(
            parse_yaml_document(
                "version: 1\nservices:\n"
                '  legacy:\n    command: x\n    runtime: { node: "18.19.0" }\n'
                '  modern:\n    command: y\n    runtime: { node: "24.6.0" }\n',
                source="devbox.yml",
            )
        )
        resolution = resolve_lockfile(config, registry=registry, platform=host_platform)
        outcomes = manager.ensure_installed(
            config.required_runtimes(), resolution.lockfile, registry
        )
        assert [o.runtime.version for o in outcomes] == ["18.19.0", "24.6.0"]
        assert manager.missing(config.required_runtimes()) == ()

    def test_a_missing_lockfile_entry_is_an_actionable_error(
        self, manager: RuntimeManager, registry: FilesystemRegistry
    ) -> None:
        from devbox.errors import LockfileError

        with pytest.raises(LockfileError) as caught:
            manager.ensure_installed([RuntimeRequirement("node", "18.19.0")], Lockfile(), registry)
        assert caught.value.hint is not None
        assert "devbox install" in caught.value.hint
