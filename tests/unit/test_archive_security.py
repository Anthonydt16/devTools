"""Archive extraction as a security boundary.

A runtime archive is executable code from somewhere else.  Each test here is one
attack the extractor must refuse, and the assertion is always the same shape:
the error names the entry, and **nothing was written**.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from devbox.errors import SecurityError
from devbox.security.archive import ExtractionLimits, extract_archive, is_supported_archive

pytestmark = pytest.mark.unit


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def make_tar(path: Path, members: dict[str, bytes], *, suffix: str = "") -> Path:
    mode = "w:gz" if suffix == "gz" else "w"
    with tarfile.open(path, mode) as archive:  # type: ignore[call-overload]
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


class TestHappyPath:
    def test_a_normal_zip_extracts(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"bin/node": b"#!/bin/sh\n", "README": b"hi"})
        result = extract_archive(archive, tmp_path / "out")
        assert (tmp_path / "out" / "bin" / "node").read_bytes() == b"#!/bin/sh\n"
        assert result.entries == 2

    def test_a_normal_tar_gz_extracts(self, tmp_path: Path) -> None:
        archive = make_tar(tmp_path / "a.tar.gz", {"bin/node": b"x"}, suffix="gz")
        extract_archive(archive, tmp_path / "out")
        assert (tmp_path / "out" / "bin" / "node").read_bytes() == b"x"

    def test_a_single_top_level_directory_is_stripped(self, tmp_path: Path) -> None:
        """Vendors ship node-v18.19.0-win-x64/...; the install root must be flat."""
        archive = make_zip(
            tmp_path / "a.zip",
            {"node-v18.19.0-win-x64/node.exe": b"MZ", "node-v18.19.0-win-x64/npm.cmd": b"@echo"},
        )
        result = extract_archive(archive, tmp_path / "out")
        assert (tmp_path / "out" / "node.exe").is_file()
        assert result.stripped_prefix == "node-v18.19.0-win-x64"

    def test_stripping_can_be_disabled(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"root/file": b"x"})
        extract_archive(archive, tmp_path / "out", strip_single_root=False)
        assert (tmp_path / "out" / "root" / "file").is_file()

    def test_extraction_replaces_a_previous_install(self, tmp_path: Path) -> None:
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "stale.txt").write_text("old", encoding="utf-8")
        extract_archive(make_zip(tmp_path / "a.zip", {"new.txt": b"x"}), destination)
        assert not (destination / "stale.txt").exists()
        assert (destination / "new.txt").is_file()

    def test_the_executable_bit_survives_on_posix(self, tmp_path: Path) -> None:
        import os
        import stat

        archive_path = tmp_path / "a.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            info = zipfile.ZipInfo("node")
            info.external_attr = (0o755 & 0xFFFF) << 16
            archive.writestr(info, b"#!/bin/sh\n")
        extract_archive(archive_path, tmp_path / "out")
        if os.name != "nt":
            assert (tmp_path / "out" / "node").stat().st_mode & stat.S_IXUSR


class TestPathTraversal:
    """Non-regression #5."""

    @pytest.mark.parametrize(
        "member",
        [
            "../../evil.exe",
            "../evil.exe",
            "a/../../evil.exe",
            "/etc/passwd",
            "C:\\Windows\\System32\\evil.exe",
            "..\\..\\evil.exe",
            "\\\\server\\share\\evil.exe",
        ],
    )
    def test_a_dangerous_entry_is_refused(self, tmp_path: Path, member: str) -> None:
        archive = make_zip(tmp_path / "a.zip", {member: b"pwned"})
        with pytest.raises(SecurityError) as caught:
            extract_archive(archive, tmp_path / "out")
        assert "refusing archive entry" in str(caught.value)

    def test_nothing_is_written_when_an_entry_is_refused(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"safe.txt": b"ok", "../../evil.exe": b"pwned"})
        with pytest.raises(SecurityError):
            extract_archive(archive, tmp_path / "out")
        assert not (tmp_path / "out").exists(), "a refused archive must leave nothing behind"
        assert not (tmp_path.parent / "evil.exe").exists()

    def test_no_staging_directory_is_left_behind(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"../../evil.exe": b"pwned"})
        with pytest.raises(SecurityError):
            extract_archive(archive, tmp_path / "out")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out.extracting")]
        assert leftovers == []

    def test_a_reserved_windows_device_name_is_refused(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"CON": b"x"})
        with pytest.raises(SecurityError, match="reserved Windows device name"):
            extract_archive(archive, tmp_path / "out")

    def test_a_tar_entry_can_traverse_too(self, tmp_path: Path) -> None:
        archive = make_tar(tmp_path / "a.tar", {"../../evil.exe": b"pwned"})
        with pytest.raises(SecurityError, match="refusing archive entry"):
            extract_archive(archive, tmp_path / "out")


class TestLinks:
    def test_a_tar_symlink_is_refused(self, tmp_path: Path) -> None:
        """A symlink is how an archive escapes its directory on extraction."""
        archive_path = tmp_path / "a.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with pytest.raises(SecurityError, match="symbolic link"):
            extract_archive(archive_path, tmp_path / "out")

    def test_a_tar_hardlink_is_refused(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "a.tar"
        with tarfile.open(archive_path, "w") as archive:
            data = b"real"
            real = tarfile.TarInfo("real")
            real.size = len(data)
            archive.addfile(real, io.BytesIO(data))
            link = tarfile.TarInfo("link")
            link.type = tarfile.LNKTYPE
            link.linkname = "real"
            archive.addfile(link)
        with pytest.raises(SecurityError, match="hard link"):
            extract_archive(archive_path, tmp_path / "out")

    def test_a_device_entry_is_refused(self, tmp_path: Path) -> None:
        archive_path = tmp_path / "a.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("dev")
            info.type = tarfile.CHRTYPE
            archive.addfile(info)
        with pytest.raises(SecurityError, match="device or FIFO"):
            extract_archive(archive_path, tmp_path / "out")

    def test_a_zip_symlink_is_refused(self, tmp_path: Path) -> None:
        import stat

        archive_path = tmp_path / "a.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
        with pytest.raises(SecurityError, match="symbolic link"):
            extract_archive(archive_path, tmp_path / "out")


class TestArchiveBombs:
    def test_too_many_entries_is_refused(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {f"f{i}": b"x" for i in range(50)})
        limits = ExtractionLimits(max_entries=10)
        with pytest.raises(SecurityError, match="more than 10 entries"):
            extract_archive(archive, tmp_path / "out", limits=limits)

    def test_an_oversized_entry_is_refused(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"big": b"x" * 5000})
        limits = ExtractionLimits(max_entry_bytes=1000)
        with pytest.raises(SecurityError, match=r"per-entry limit|byte limit"):
            extract_archive(archive, tmp_path / "out", limits=limits)

    def test_an_oversized_total_is_refused(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {f"f{i}": b"x" * 1000 for i in range(10)})
        limits = ExtractionLimits(max_total_bytes=2500)
        with pytest.raises(SecurityError, match=r"total limit|byte limit"):
            extract_archive(archive, tmp_path / "out", limits=limits)

    def test_a_lying_tar_header_is_caught_while_writing(self, tmp_path: Path) -> None:
        """A header claiming 1 byte must not be trusted to deliver 1 byte."""
        archive_path = tmp_path / "a.tar"
        payload = b"y" * 5000
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("big")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        limits = ExtractionLimits(max_entry_bytes=1000)
        with pytest.raises(SecurityError):
            extract_archive(archive_path, tmp_path / "out", limits=limits)


class TestCorruptAndUnsupported:
    def test_a_corrupt_zip_is_reported_with_a_hint(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.zip"
        archive.write_bytes(b"PK\x03\x04 this is not a zip file at all")
        with pytest.raises(SecurityError) as caught:
            extract_archive(archive, tmp_path / "out")
        assert "corrupt archive" in str(caught.value)
        assert caught.value.hint is not None

    def test_a_truncated_gzip_is_reported(self, tmp_path: Path) -> None:
        good = make_tar(tmp_path / "a.tar.gz", {"f": b"x" * 500}, suffix="gz")
        data = good.read_bytes()
        good.write_bytes(data[: len(data) // 2])
        with pytest.raises(SecurityError):
            extract_archive(good, tmp_path / "out")

    def test_an_unsupported_format_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.rar"
        archive.write_bytes(b"Rar!")
        with pytest.raises(SecurityError, match="unsupported archive format"):
            extract_archive(archive, tmp_path / "out")

    def test_a_missing_archive_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(SecurityError, match="archive not found"):
            extract_archive(tmp_path / "ghost.zip", tmp_path / "out")

    def test_supported_formats_are_recognised(self) -> None:
        assert is_supported_archive("node.zip")
        assert is_supported_archive("node.tar.gz")
        assert not is_supported_archive("node.7z")
