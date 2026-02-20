"""
Unit tests for _tail_offset() — the hand-rolled binary seek algorithm
used by the log streaming endpoint.

This function is the trickiest piece of pure logic in the codebase.
It finds the byte offset to start reading the last N lines of a log file.
"""
from routes.training import _tail_offset


def test_nonexistent_file(tmp_path):
    assert _tail_offset(str(tmp_path / "nope.log"), 100) == 0


def test_empty_file(tmp_path):
    log = tmp_path / "train.log"
    log.write_bytes(b"")
    assert _tail_offset(str(log), 100) == 0


def test_fewer_lines_than_requested(tmp_path):
    """Should return 0 (start of file) when file has fewer lines than requested."""
    log = tmp_path / "train.log"
    log.write_bytes(b"line1\nline2\nline3\n")
    assert _tail_offset(str(log), 100) == 0


def test_exactly_n_lines(tmp_path):
    """Requesting exactly the number of lines in the file should return 0."""
    log = tmp_path / "train.log"
    log.write_bytes(b"line1\nline2\nline3\n")
    assert _tail_offset(str(log), 3) == 0


def test_tail_returns_correct_lines(tmp_path):
    """Offset should point to the start of line (N-tail+1)."""
    log = tmp_path / "train.log"
    lines = [f"line{i:02d}\n".encode() for i in range(20)]
    log.write_bytes(b"".join(lines))

    offset = _tail_offset(str(log), 10)
    content = log.read_bytes()[offset:]
    result_lines = content.splitlines()

    assert len(result_lines) == 10
    assert result_lines[0] == b"line10"
    assert result_lines[-1] == b"line19"


def test_tail_one_line(tmp_path):
    """Requesting 1 line should return only the last line."""
    log = tmp_path / "train.log"
    log.write_bytes(b"alpha\nbeta\ngamma\n")

    offset = _tail_offset(str(log), 1)
    content = log.read_bytes()[offset:]
    assert content.strip() == b"gamma"


def test_large_file_multiple_buffer_reads(tmp_path):
    """
    File larger than the 8192-byte buffer forces multiple backward reads.
    Correct result still required.
    """
    log = tmp_path / "train.log"
    # ~100 bytes per line × 200 lines = ~20 KB (> 8192 buffer)
    lines = [f"{'x' * 94} {i:03d}\n".encode() for i in range(200)]
    log.write_bytes(b"".join(lines))

    offset = _tail_offset(str(log), 10)
    content = log.read_bytes()[offset:]
    result_lines = content.splitlines()

    assert len(result_lines) == 10
    assert b"190" in result_lines[0]
    assert b"199" in result_lines[-1]


def test_offset_is_valid_byte_position(tmp_path):
    """The returned offset should always be a valid position within the file."""
    log = tmp_path / "train.log"
    log.write_bytes(b"a\nb\nc\nd\ne\n")
    size = log.stat().st_size

    for n in (1, 2, 3, 10):
        offset = _tail_offset(str(log), n)
        assert 0 <= offset <= size
