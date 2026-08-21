"""``devbox build``: reproducibility, manifest, inclusion and failure modes."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from devbox.build.artifact import (
    MANIFEST_NAME,
    Builder,
    BuildSpec,
    verify_artifact,
)
from devbox.errors import BuildError
from devbox.lockfile.models import LockedRuntime, Lockfile

pytestmark = pytest.mark.unit

EPOCH = {"SOURCE_DATE_EPOCH": "1700000000"}


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "my-api"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".devbox" / "logs").mkdir(parents=True)
    (root / "src" / "index.js").write_text("console.log('hi')\n", encoding="utf-8")
    (root / "package.json").write_text('{"name":"my-api"}\n', encoding="utf-8")
    (root / "README.md").write_text("# my-api\n", encoding="utf-8")
    (root / "node_modules" / "left-pad" / "index.js").write_text("//\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / ".devbox" / "logs" / "api.out.log").write_text("noise\n", encoding="utf-8")
    return root


@pytest.fixture
def lockfile() -> Lockfile:
    return Lockfile().with_entry(
        LockedRuntime("node", "24.6.0", "windows-x64", "a" * 64, "registry/node/24.6.0")
    )


def spec_for(source: Path, tmp_path: Path, **kwargs: object) -> BuildSpec:
    defaults: dict[str, object] = {
        "application": "my-api",
        "version": "1.4.2",
        "project_root": source,
        "output_dir": tmp_path / "dist",
        "runtimes": {"node": "24.6.0"},
    }
    defaults.update(kwargs)
    return BuildSpec(**defaults)  # type: ignore[arg-type]


class TestArtifact:
    def test_the_artifact_is_produced_with_the_expected_name(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert result.artifact_path.name == "my-api-1.4.2.zip"
        assert result.artifact_path.is_file()

    def test_project_files_are_included(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert "src/index.js" in result.entries
        assert "package.json" in result.entries

    def test_noise_is_excluded_by_default(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert not any(entry.startswith("node_modules/") for entry in result.entries)
        assert not any(entry.startswith(".git/") for entry in result.entries)
        assert not any(entry.startswith(".devbox/") for entry in result.entries)

    def test_include_patterns_narrow_the_selection(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(
            spec_for(source, tmp_path, include=("src/*",)), lockfile=lockfile
        )
        assert result.entries == ("src/index.js",)

    def test_extra_excludes_are_honoured(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        from devbox.build.artifact import DEFAULT_EXCLUDES

        result = Builder(environ=EPOCH).build(
            spec_for(source, tmp_path, exclude=(*DEFAULT_EXCLUDES, "README.md")),
            lockfile=lockfile,
        )
        assert "README.md" not in result.entries

    def test_no_temporary_file_is_left_behind(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert list((tmp_path / "dist").glob("*.building")) == []


class TestManifest:
    def test_the_manifest_is_inside_the_archive(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        with zipfile.ZipFile(result.artifact_path) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        assert manifest["application"] == "my-api"
        assert manifest["version"] == "1.4.2"
        assert manifest["runtime"] == {"node": "24.6.0"}

    def test_the_manifest_records_the_lockfile_digest(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        """An artifact must be traceable to the exact runtimes it was built against."""
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert result.manifest["devbox_lock_hash"] == lockfile.digest()

    def test_a_different_lockfile_produces_a_different_hash(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        other = lockfile.with_entry(LockedRuntime("node", "18.19.0", "windows-x64", "b" * 64, "s"))
        first = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        second = Builder(environ=EPOCH).build(
            spec_for(source, tmp_path, version="1.4.3"), lockfile=other
        )
        assert first.manifest["devbox_lock_hash"] != second.manifest["devbox_lock_hash"]

    def test_the_manifest_is_also_written_beside_the_artifact(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert result.manifest_path.is_file()
        assert json.loads(result.manifest_path.read_text())["version"] == "1.4.2"

    def test_created_at_follows_source_date_epoch(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert str(result.manifest["created_at"]).startswith("2023-11-14")


class TestReproducibility:
    def test_two_builds_of_the_same_tree_are_byte_identical(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        first = Builder(environ=EPOCH).build(
            spec_for(source, tmp_path, output_dir=tmp_path / "a"), lockfile=lockfile
        )
        second = Builder(environ=EPOCH).build(
            spec_for(source, tmp_path, output_dir=tmp_path / "b"), lockfile=lockfile
        )
        assert first.sha256 == second.sha256
        assert first.artifact_path.read_bytes() == second.artifact_path.read_bytes()

    def test_entry_order_is_stable(self, source: Path, tmp_path: Path, lockfile: Lockfile) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert list(result.entries) == sorted(result.entries)

    def test_timestamps_are_normalised(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        with zipfile.ZipFile(result.artifact_path) as archive:
            stamps = {info.date_time for info in archive.infolist()}
        assert len(stamps) == 1, "every entry must carry the same normalised timestamp"

    def test_permissions_are_normalised(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        """A developer's umask must not end up in the artifact."""
        (source / "src" / "index.js").chmod(0o600)
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        with zipfile.ZipFile(result.artifact_path) as archive:
            modes = {info.external_attr >> 16 for info in archive.infolist()}
        assert modes <= {0o644, 0o755}

    def test_without_source_date_epoch_the_zip_epoch_is_used(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ={}).build(spec_for(source, tmp_path), lockfile=lockfile)
        with zipfile.ZipFile(result.artifact_path) as archive:
            assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


class TestVerification:
    def test_a_produced_artifact_verifies(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        result = Builder(environ=EPOCH).build(spec_for(source, tmp_path), lockfile=lockfile)
        assert verify_artifact(result.artifact_path)["application"] == "my-api"

    def test_a_missing_artifact_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(BuildError, match="no artifact at"):
            verify_artifact(tmp_path / "ghost.zip")

    def test_a_corrupt_artifact_is_reported(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.zip"
        broken.write_bytes(b"not a zip at all")
        with pytest.raises(BuildError, match="not a valid zip"):
            verify_artifact(broken)

    def test_an_archive_without_a_manifest_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a.txt", "x")
        with pytest.raises(BuildError, match=r"has no devbox-manifest\.json"):
            verify_artifact(path)


class TestFailures:
    def test_a_missing_project_directory_is_reported(
        self, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        spec = BuildSpec(
            application="x",
            version="1",
            project_root=tmp_path / "ghost",
            output_dir=tmp_path / "dist",
        )
        with pytest.raises(BuildError, match="project directory does not exist"):
            Builder(environ=EPOCH).build(spec, lockfile=lockfile)

    def test_an_empty_selection_is_reported_with_a_hint(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        with pytest.raises(BuildError) as caught:
            Builder(environ=EPOCH).build(
                spec_for(source, tmp_path, include=("nothing-matches/*",)), lockfile=lockfile
            )
        assert "nothing to package" in str(caught.value)
        assert caught.value.hint is not None

    def test_a_missing_version_is_reported(
        self, source: Path, tmp_path: Path, lockfile: Lockfile
    ) -> None:
        with pytest.raises(BuildError) as caught:
            Builder(environ=EPOCH).build(spec_for(source, tmp_path, version=""), lockfile=lockfile)
        assert caught.value.hint is not None
        assert "--version" in caught.value.hint
