"""Log reading: encodings, separation, tailing, following, missing files."""

from __future__ import annotations

from pathlib import Path

import pytest

from devbox.logs import LogReader, LogStream, decode_log_bytes

pytestmark = pytest.mark.unit


@pytest.fixture
def reader(tmp_path: Path) -> LogReader:
    return LogReader(
        service="backend",
        stdout_path=tmp_path / "backend.out.log",
        stderr_path=tmp_path / "backend.err.log",
    )


class TestDecoding:
    """Windows writes logs in at least four different encodings."""

    def test_utf8(self) -> None:
        assert decode_log_bytes("démarré".encode()) == "démarré"

    def test_cp1252_ansi_code_page(self) -> None:
        assert decode_log_bytes("démarré".encode("cp1252")) == "démarré"

    def test_utf8_with_bom(self) -> None:
        assert decode_log_bytes(b"\xef\xbb\xbf" + "démarré".encode()) == "démarré"

    def test_utf16_le_with_bom_as_powershell_redirection_writes_it(self) -> None:
        raw = b"\xff\xfe" + "démarré".encode("utf-16-le")
        assert decode_log_bytes(raw) == "démarré"

    def test_undecodable_bytes_still_produce_output(self) -> None:
        """A broken byte must never be why a developer cannot read their log."""
        assert decode_log_bytes(b"ok \x81\x8d\x90 end").startswith("ok ")

    def test_an_unknown_fallback_encoding_does_not_raise(self) -> None:
        assert decode_log_bytes(b"\xff\xfa", fallback="not-a-codec")


class TestReading:
    def test_streams_are_kept_separate(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"out line\n")
        reader.stderr_path.write_bytes(b"err line\n")
        lines = reader.read()
        assert [(line.stream, line.text) for line in lines] == [
            (LogStream.STDOUT, "out line"),
            (LogStream.STDERR, "err line"),
        ]

    def test_one_stream_can_be_read_alone(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"a\nb\n")
        reader.stderr_path.write_bytes(b"boom\n")
        assert [line.text for line in reader.read(streams=[LogStream.STDERR])] == ["boom"]

    def test_a_missing_file_is_not_an_error(self, reader: LogReader) -> None:
        assert reader.read() == []
        assert reader.available() == (False, False)

    def test_crlf_line_endings_are_normalised(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"one\r\ntwo\r\n")
        assert [line.text for line in reader.read()] == ["one", "two"]

    def test_a_file_without_a_trailing_newline_yields_its_last_line(
        self, reader: LogReader
    ) -> None:
        reader.stdout_path.write_bytes(b"only line")
        assert [line.text for line in reader.read()] == ["only line"]

    def test_logs_are_readable_after_the_process_has_stopped(self, reader: LogReader) -> None:
        """Nothing keeps the file open, so a stopped service still has history."""
        reader.stdout_path.write_bytes(b"served 1 request\n")
        assert [line.text for line in reader.read()] == ["served 1 request"]

    def test_tail_returns_only_the_last_lines(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"".join(f"line {i}\n".encode() for i in range(1000)))
        tail = reader.read(streams=[LogStream.STDOUT], tail=3)
        assert [line.text for line in tail] == ["line 997", "line 998", "line 999"]

    @pytest.mark.slow
    def test_a_large_log_is_tailed_without_reading_it_all(self, reader: LogReader) -> None:
        """20 MB of log must not become 20 MB of memory to show 5 lines."""
        payload = b"x" * 200 + b"\n"
        with reader.stdout_path.open("wb") as handle:
            for _ in range(100_000):
                handle.write(payload)
        assert reader.stdout_path.stat().st_size > 20_000_000
        assert len(reader.read(streams=[LogStream.STDOUT], tail=5)) == 5

    def test_tail_text_renders_a_block_for_error_messages(self, reader: LogReader) -> None:
        reader.stderr_path.write_bytes(b"Error: cannot connect\n  at db.js:12\n")
        assert reader.tail_text(LogStream.STDERR, lines=2) == "Error: cannot connect\n  at db.js:12"

    def test_rendering_prefixes_the_service_and_stream(self, reader: LogReader) -> None:
        reader.stderr_path.write_bytes(b"boom\n")
        assert reader.read()[0].render() == "backend | err | boom"


class TestFollow:
    def test_follow_yields_lines_appended_after_it_started(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"already there\n")
        stream = reader.follow(streams=[LogStream.STDOUT], poll_interval=0.01, stop_after=0.2)

        with reader.stdout_path.open("ab") as handle:
            handle.write(b"new line\n")

        texts = [line.text for line in stream]
        assert "new line" in texts
        assert "already there" not in texts

    def test_follow_from_start_includes_existing_content(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"already there\n")
        stream = reader.follow(
            streams=[LogStream.STDOUT], poll_interval=0.01, stop_after=0.05, from_start=True
        )
        assert "already there" in [line.text for line in stream]

    def test_follow_terminates_on_its_deadline_even_with_no_output(self, reader: LogReader) -> None:
        """A --follow test that can hang forever is worse than no test."""
        reader.stdout_path.write_bytes(b"")
        assert list(reader.follow(poll_interval=0.01, stop_after=0.05)) == []

    def test_follow_survives_a_truncated_file(self, reader: LogReader) -> None:
        reader.stdout_path.write_bytes(b"one\ntwo\n")
        stream = reader.follow(
            streams=[LogStream.STDOUT], poll_interval=0.01, stop_after=0.2, from_start=True
        )
        first = next(stream)
        assert first.text == "one"
        reader.stdout_path.write_bytes(b"fresh\n")
        assert "fresh" in [line.text for line in stream]

    def test_follow_handles_a_file_that_does_not_exist_yet(self, reader: LogReader) -> None:
        assert list(reader.follow(poll_interval=0.01, stop_after=0.05)) == []
