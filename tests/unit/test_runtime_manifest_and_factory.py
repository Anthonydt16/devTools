"""Registry manifests, and choosing a supervisor for the running platform."""

from __future__ import annotations

import pytest

from devbox.errors import RegistryError
from devbox.host.platform_info import (
    PLATFORM_LINUX_X64,
    PLATFORM_WINDOWS_X64,
    Platform,
    current_platform,
    is_windows,
)
from devbox.process.factory import create_supervisor
from devbox.runtime.manifest import RuntimeManifest, manifest_from_document

pytestmark = pytest.mark.unit

DIGEST = "a" * 64


class TestManifest:
    def test_a_valid_manifest_is_accepted(self) -> None:
        manifest = RuntimeManifest(
            name="node",
            version="24.6.0",
            platform="windows-x64",
            url="node/24.6.0/windows-x64.zip",
            sha256=DIGEST,
        )
        assert manifest.matches(PLATFORM_WINDOWS_X64)
        assert not manifest.matches(PLATFORM_LINUX_X64)

    def test_a_manifest_without_a_usable_digest_is_refused(self) -> None:
        with pytest.raises(RegistryError) as caught:
            RuntimeManifest(
                name="node", version="1", platform="windows-x64", url="u", sha256="nope"
            )
        assert "invalid sha256" in str(caught.value)
        assert caught.value.hint is not None
        assert "never installs an artifact it cannot verify" in caught.value.hint

    def test_the_archive_name_is_taken_from_the_manifest(self) -> None:
        manifest = RuntimeManifest(
            name="node",
            version="1",
            platform="windows-x64",
            url="u",
            sha256=DIGEST,
            archive="node-1-win.zip",
        )
        assert manifest.archive_name == "node-1-win.zip"

    def test_the_archive_name_falls_back_to_the_url(self) -> None:
        manifest = RuntimeManifest(
            name="node",
            version="1",
            platform="windows-x64",
            url="https://x/y/node-1.tar.gz?token=abc",
            sha256=DIGEST,
        )
        assert manifest.archive_name == "node-1.tar.gz"

    def test_an_unrecognisable_url_yields_a_synthesised_name(self) -> None:
        manifest = RuntimeManifest(
            name="node",
            version="1",
            platform="windows-x64",
            url="https://x/download",
            sha256=DIGEST,
        )
        assert manifest.archive_name == "node-1-windows-x64.zip"

    def test_a_manifest_becomes_a_lockfile_entry(self) -> None:
        locked = RuntimeManifest(
            name="node",
            version="24.6.0",
            platform="windows-x64",
            url="u",
            sha256=DIGEST,
            size=42,
            source="registry/node/24.6.0",
        ).to_locked()
        assert locked.name == "node"
        assert locked.sha256 == DIGEST
        assert locked.source == "registry/node/24.6.0"
        assert locked.size == 42


class TestManifestParsing:
    def build(self, document: dict[str, object]) -> RuntimeManifest:
        return manifest_from_document(document, name="node", version="1", platform="windows-x64")

    def test_a_complete_entry_parses(self) -> None:
        manifest = self.build({"url": "u", "sha256": DIGEST.upper(), "size": 10})
        assert manifest.sha256 == DIGEST
        assert manifest.size == 10

    def test_a_missing_url_is_reported(self) -> None:
        with pytest.raises(RegistryError, match="missing 'url'"):
            self.build({"sha256": DIGEST})

    def test_a_missing_digest_is_reported(self) -> None:
        with pytest.raises(RegistryError, match="missing 'sha256'"):
            self.build({"url": "u"})

    def test_a_non_string_url_is_reported(self) -> None:
        with pytest.raises(RegistryError, match="'url' must be a string"):
            self.build({"url": 3, "sha256": DIGEST})

    def test_a_non_integer_size_is_reported(self) -> None:
        with pytest.raises(RegistryError, match="'size' must be an integer"):
            self.build({"url": "u", "sha256": DIGEST, "size": "big"})


class TestPlatform:
    def test_a_tag_round_trips(self) -> None:
        assert Platform.parse("windows-x64") == PLATFORM_WINDOWS_X64
        assert str(PLATFORM_WINDOWS_X64) == "windows-x64"

    def test_a_malformed_tag_is_refused(self) -> None:
        for bad in ("windows", "windows-x64-extra", "", "-x64"):
            with pytest.raises(ValueError, match="invalid platform tag"):
                Platform.parse(bad)

    def test_platform_properties(self) -> None:
        assert PLATFORM_WINDOWS_X64.executable_suffix == ".exe"
        assert PLATFORM_WINDOWS_X64.path_separator == ";"
        assert PLATFORM_LINUX_X64.executable_suffix == ""
        assert PLATFORM_LINUX_X64.path_separator == ":"

    def test_the_current_platform_is_recognised(self) -> None:
        platform = current_platform()
        assert platform.os
        assert platform.arch
        assert platform.is_windows == is_windows()

    def test_platforms_sort_deterministically(self) -> None:
        assert sorted([PLATFORM_WINDOWS_X64, PLATFORM_LINUX_X64])[0] == PLATFORM_LINUX_X64


class TestSupervisorFactory:
    def test_the_default_matches_the_platform(self) -> None:
        supervisor = create_supervisor()
        expected = "windows" if is_windows() else "posix"
        assert supervisor.name == expected

    def test_an_implementation_can_be_forced(self) -> None:
        assert create_supervisor(prefer="fake").name == "fake"
        assert create_supervisor(prefer="posix").name == "posix"

    def test_forcing_windows_selects_the_windows_supervisor(self) -> None:
        """Constructing it is safe anywhere; only its calls need Windows."""
        assert create_supervisor(prefer="windows").name == "windows"

    def test_an_unknown_preference_falls_back_to_posix(self) -> None:
        assert create_supervisor(prefer="nonsense").name == "posix"
